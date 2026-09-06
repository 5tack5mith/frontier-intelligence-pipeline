"""
Demo/reporting utility: prints the final record-count summary from the
already-written output/*.jsonl files, instantly, without re-running any
scraper. Re-running the full pipeline takes ~70 minutes (GitHub's
search-endpoint rate limit on research-papers enrichment); this reads
the same files run_all.py already validated and wrote.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

OUTPUT_DIR = Path("output")

ENTITY_FILES = {
    "startups": "startups.jsonl",
    "products": "products.jsonl",
    "research_papers": "research_papers.jsonl",
    "jobs": "jobs.jsonl",
    "news": "news.jsonl",
    "entity_mapping_log": "entity_mapping_log.jsonl",
}

EXPECTED_DOMAIN = {
    "startups": "ycombinator.com",
    "products": "ycombinator.com",
    "research_papers": "arxiv.org",
}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    print("=" * 70)
    print("FINAL RECORD COUNTS (from output/*.jsonl — pipeline already run)")
    print("=" * 70)

    records_by_type: dict[str, list[dict]] = {}
    for name, filename in ENTITY_FILES.items():
        rows = load_jsonl(OUTPUT_DIR / filename)
        records_by_type[name] = rows
        print(f"  {name:20s} {len(rows):6d}")

    print("\nSpot-checking ~3 random source.url values per entity type:")
    for name in ("startups", "products", "research_papers", "jobs", "news"):
        rows = records_by_type[name]
        urls = [r["source"]["url"] for r in rows if "source" in r]
        sample = random.sample(urls, min(3, len(urls))) if urls else []
        print(f"\n  [{name}]")
        for url in sample:
            expected = EXPECTED_DOMAIN.get(name)
            flag = "  ** FLAG **" if expected and expected not in url else ""
            print(f"    {url}{flag}")

    print("\nDone.")


if __name__ == "__main__":
    main()
