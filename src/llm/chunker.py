"""
Payload chunking / truncation to avoid 413 (Payload Too Large) errors.

Strategy: rather than a naive head-truncate (which loses information
density from long articles), we score paragraphs and keep the highest-
density subset that fits the token budget. Density heuristics used:
  1. Paragraphs near the start of the document (leads carry most info)
  2. Paragraphs containing digits/numbers (often stats, dates, metrics)
  3. Paragraphs containing capitalized multi-word sequences (likely
     named entities: people, orgs, products)
  4. Penalize very short paragraphs (nav/boilerplate junk like "Share"
     or "Subscribe") and very long ones (likely legal/cookie text)

A real tokenizer (tiktoken) is preferred if available; falls back to
a word-count heuristic (~1.3 tokens per word for English) so the
module still works with zero extra dependencies if tiktoken isn't
installed in a given environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))

except Exception:
    # Covers both ImportError (tiktoken not installed) and the more
    # common case in sandboxed/offline environments: tiktoken IS
    # installed but its encoding file has to be fetched from
    # openaipublic.blob.core.windows.net on first use, which fails
    # behind a network allowlist. Either way, fall back to a
    # word-count heuristic (~1.3 tokens/word for English) so the
    # module still works with zero network dependency.
    def count_tokens(text: str) -> int:
        words = text.split()
        return int(len(words) * 1.3)


NUMBER_RE = re.compile(r"\d")
NAMED_ENTITY_RE = re.compile(r"(?:[A-Z][a-z]+\s){1,4}[A-Z][a-z]+")


@dataclass
class ChunkResult:
    text: str
    original_tokens: int
    final_tokens: int
    paragraphs_kept: int
    paragraphs_total: int
    truncated: bool


def _paragraph_score(paragraph: str, position_index: int, total_paragraphs: int) -> float:
    length = len(paragraph)
    if length < 20:
        return -1.0  # near-certainly boilerplate ("Share", "Advertisement", etc.)
    if length > 2000:
        return -0.5  # likely legal/cookie block, penalize but don't hard-exclude

    score = 0.0
    # Lead paragraphs carry the most information in journalism / abstracts
    position_ratio = position_index / max(total_paragraphs - 1, 1)
    score += max(0.0, 1.0 - position_ratio) * 2.0

    # Reward numeric density (stats, dates, dollar amounts, counts)
    score += min(len(NUMBER_RE.findall(paragraph)), 10) * 0.15

    # Reward apparent named entities (people/org names)
    score += min(len(NAMED_ENTITY_RE.findall(paragraph)), 10) * 0.2

    return score


def chunk_text(
    text: str,
    max_tokens: int = 3000,
    min_paragraph_chars: int = 20,
) -> ChunkResult:
    """
    Truncate `text` to fit within `max_tokens`, keeping the
    highest information-density paragraphs rather than naive
    head-truncation.
    """
    original_tokens = count_tokens(text)
    if original_tokens <= max_tokens:
        return ChunkResult(
            text=text,
            original_tokens=original_tokens,
            final_tokens=original_tokens,
            paragraphs_kept=text.count("\n\n") + 1,
            paragraphs_total=text.count("\n\n") + 1,
            truncated=False,
        )

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    total = len(paragraphs)

    scored = [
        (idx, para, _paragraph_score(para, idx, total))
        for idx, para in enumerate(paragraphs)
    ]
    # Keep original order for readability, but select by score
    scored_sorted = sorted(scored, key=lambda x: x[2], reverse=True)

    kept_indices: set[int] = set()
    running_tokens = 0
    for idx, para, score in scored_sorted:
        if score < 0:
            continue
        para_tokens = count_tokens(para)
        if running_tokens + para_tokens > max_tokens:
            continue
        kept_indices.add(idx)
        running_tokens += para_tokens

    # Always try to keep paragraph 0 (lead/title) even if scoring excluded it,
    # as long as it fits — leads are disproportionately important.
    if 0 not in kept_indices and paragraphs:
        first_tokens = count_tokens(paragraphs[0])
        if running_tokens + first_tokens <= max_tokens:
            kept_indices.add(0)
            running_tokens += first_tokens

    kept_paragraphs = [paragraphs[i] for i in sorted(kept_indices)]
    final_text = "\n\n".join(kept_paragraphs)

    return ChunkResult(
        text=final_text,
        original_tokens=original_tokens,
        final_tokens=count_tokens(final_text),
        paragraphs_kept=len(kept_paragraphs),
        paragraphs_total=total,
        truncated=True,
    )


if __name__ == "__main__":
    # Smoke test with a synthetic long article to prove truncation triggers.
    lead = "OpenAI announced GPT-6 today, a model with 40 trillion parameters, in San Francisco."
    boilerplate = "Share\n\nSubscribe to our newsletter for more updates like this one every day."
    filler = " ".join(["This is filler text without much specific information in it."] * 400)
    stat_para = "The company raised $500 million in Series C funding led by Sequoia Capital and Andreessen Horowitz in 2026."

    fake_article = "\n\n".join([lead, filler, stat_para, boilerplate])

    result = chunk_text(fake_article, max_tokens=200)
    print(f"Original tokens: {result.original_tokens}")
    print(f"Final tokens: {result.final_tokens}")
    print(f"Truncated: {result.truncated}")
    print(f"Paragraphs kept: {result.paragraphs_kept}/{result.paragraphs_total}")
    print("--- kept text ---")
    print(result.text)
    assert result.truncated
    assert "GPT-6" in result.text, "Lead paragraph should survive chunking"
    assert "$500 million" in result.text, "High-density stat paragraph should survive chunking"
    print("\nChunker smoke test PASSED.")
