"""
Deterministic entity resolution: map messy raw startup/company names
to a canonical form, using an exact-match lookup first and a fuzzy
match fallback (rapidfuzz) second. Every resolution decision is logged
to feed the "Entity Mapping Log" output tab (brief deliverable #6).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

from src.pipeline.schemas import EntityMappingLogEntry, MatchMethod

FUZZY_MATCH_THRESHOLD = 82.0  # 0-100 rapidfuzz score.
# Tuning note: a single-character typo in a short name (e.g. "OpenAl" vs
# "OpenAI") scores ~83 on WRatio because the edit distance is large
# relative to the string length. 82 catches that case; anything lower
# starts risking false-positive merges of genuinely different short
# company names. Review the Entity Mapping Log's fuzzy-match entries by
# hand after each run — this threshold is a judgment call, not a solved
# problem, and the log exists specifically so a human can audit it.


def _normalize(name: str) -> str:
    """Lowercase, strip common corporate suffixes and punctuation noise
    so 'OpenAI, Inc.' and 'Open AI' land close together before fuzzy
    matching even runs."""
    name = name.lower().strip()
    name = re.sub(r"[.,]", "", name)
    name = re.sub(r"\b(inc|llc|ltd|pbc|corp|corporation|co)\b", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


class EntityCanonicalizer:
    def __init__(self, seed_path: Optional[str] = None):
        """
        seed_path: path to a JSON file shaped like:
            { "OpenAI": ["OpenAI", "OpenAI, Inc.", "Open AI"], ... }
        """
        self.canonical_to_variants: dict[str, list[str]] = {}
        self.variant_to_canonical: dict[str, str] = {}  # normalized variant -> canonical
        self.log: list[EntityMappingLogEntry] = []

        if seed_path:
            self.load_seed(seed_path)

    def load_seed(self, seed_path: str) -> None:
        data = json.loads(Path(seed_path).read_text())
        for canonical, variants in data.items():
            self.canonical_to_variants[canonical] = list(variants)
            for v in variants:
                self.variant_to_canonical[_normalize(v)] = canonical

    def resolve(self, raw_name: str, source_record_id: Optional[str] = None) -> str:
        """
        Resolve a raw name to its canonical form. Logs the decision.
        Falls back to registering the raw name as its own new canonical
        entity if no match is found above threshold — this keeps every
        record resolvable without ever inventing a "smarter" canonical
        name than what we actually observed.
        """
        normalized = _normalize(raw_name)

        # 1. Exact match against normalized seed variants
        if normalized in self.variant_to_canonical:
            canonical = self.variant_to_canonical[normalized]
            self.log.append(
                EntityMappingLogEntry(
                    raw_name=raw_name,
                    canonical_name=canonical,
                    source_record_id=source_record_id,
                    match_method=MatchMethod.EXACT,
                )
            )
            return canonical

        # 2. Fuzzy match against known canonical names + all known variants
        choices = list(self.variant_to_canonical.keys())
        if choices:
            match = process.extractOne(normalized, choices, scorer=fuzz.WRatio)
            if match is not None:
                matched_variant, score, _ = match
                if score >= FUZZY_MATCH_THRESHOLD:
                    canonical = self.variant_to_canonical[matched_variant]
                    self.log.append(
                        EntityMappingLogEntry(
                            raw_name=raw_name,
                            canonical_name=canonical,
                            source_record_id=source_record_id,
                            match_method=MatchMethod.FUZZY,
                            confidence=float(score),
                        )
                    )
                    return canonical

        # 3. No match — register raw name as its own new canonical entity.
        # We do NOT invent a "cleaner-looking" name; the raw name becomes
        # canonical going forward so future exact variants of it resolve
        # correctly.
        self.canonical_to_variants.setdefault(raw_name, []).append(raw_name)
        self.variant_to_canonical[normalized] = raw_name
        self.log.append(
            EntityMappingLogEntry(
                raw_name=raw_name,
                canonical_name=raw_name,
                source_record_id=source_record_id,
                match_method=MatchMethod.MANUAL_SEED,  # treated as a new seed of one
            )
        )
        return raw_name

    def export_log(self) -> list[dict]:
        return [entry.model_dump() for entry in self.log]


if __name__ == "__main__":
    import tempfile

    seed = {
        "OpenAI": ["OpenAI", "OpenAI, Inc.", "Open AI", "OpenAI Inc"],
        "Anthropic": ["Anthropic", "Anthropic PBC", "Anthropic, PBC"],
        "Google DeepMind": ["DeepMind", "Google DeepMind", "Alphabet - DeepMind"],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(seed, f)
        seed_path = f.name

    resolver = EntityCanonicalizer(seed_path=seed_path)

    test_cases = [
        ("OpenAI, Inc.", "OpenAI"),       # exact after normalization
        ("Open AI", "OpenAI"),             # exact seed variant
        ("OpenAl", "OpenAI"),               # typo -> should fuzzy match
        ("DeepMind", "Google DeepMind"),    # exact seed variant
        ("Some Random New Startup Co", "Some Random New Startup Co"),  # no match -> self
    ]

    print("Testing entity resolution:\n")
    all_passed = True
    for raw, expected in test_cases:
        result = resolver.resolve(raw, source_record_id=f"test-{raw}")
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  [{status}] '{raw}' -> '{result}' (expected '{expected}')")

    print("\nEntity Mapping Log:")
    for entry in resolver.export_log():
        print(f"  {entry}")

    assert all_passed, "Some entity resolution test cases failed"
    print("\nCanonicalizer smoke test PASSED.")
