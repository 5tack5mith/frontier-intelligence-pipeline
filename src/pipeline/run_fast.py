"""
Fast path: Startups + Products + Jobs + News only — no research-paper
GitHub enrichment, so this finishes in ~1-2 minutes instead of ~70.
Meant to run every 6 hours in CI (see .github/workflows/fast_pipeline.yml).

Writes/overwrites exactly 5 output files: startups.jsonl/csv,
products.jsonl/csv, jobs.jsonl/csv, news.jsonl/csv, and
entity_mapping_log.jsonl/csv (the entity mapping log is entirely a
byproduct of startup/job name resolution — research papers never touch
EntityCanonicalizer, so this file is the log's sole producer). Does
NOT touch research_papers.jsonl/csv — that's run_papers.py's job, on
its own slower schedule, and to_sheets.py --only keeps the two Sheet
pushes from clobbering each other's tabs.

Run with:  python -m src.pipeline.run_fast
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.output.writer import validate_and_write, write_plain_dicts
from src.pipeline.steps import (
    OUTPUT_DIR,
    SEED_ENTITIES_PATH,
    print_spot_check,
    run_jobs,
    run_news,
    run_startups_and_products,
)
from src.resolution.canonicalizer import EntityCanonicalizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_fast")


async def main():
    resolver = EntityCanonicalizer(seed_path=SEED_ENTITIES_PATH)
    substitution_notes: list[tuple[str, str]] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        (startups, products), (news_records, news_subs), (job_records, job_subs) = await asyncio.gather(
            run_startups_and_products(client, resolver),
            run_news(client),
            run_jobs(client, resolver),
        )
        substitution_notes.extend(news_subs)
        substitution_notes.extend(job_subs)

    entity_log = resolver.export_log()

    logger.info("=== Writing outputs (startups/products/jobs/news/entity_mapping_log only) ===")
    OUTPUT_DIR.mkdir(exist_ok=True)
    summaries = {
        "startups": validate_and_write("startups", startups, OUTPUT_DIR),
        "products": validate_and_write("products", products, OUTPUT_DIR),
        "jobs": validate_and_write("jobs", job_records, OUTPUT_DIR),
        "news": validate_and_write("news", news_records, OUTPUT_DIR),
        "entity_mapping_log": write_plain_dicts("entity_mapping_log", entity_log, OUTPUT_DIR),
    }

    print("\n" + "=" * 70)
    print("FAST PATH SUMMARY (research_papers.* untouched — see run_papers.py)")
    print("=" * 70)
    for name, summary in summaries.items():
        print(f"  {name:20s} total={summary['total']:5d}  valid={summary['valid']:5d}  rejected={summary['rejected']:4d}")

    if substitution_notes:
        print("\nSource substitutions made:")
        for name, reason in substitution_notes:
            print(f"  - {name}: {reason}")
    else:
        print("\nNo source substitutions were necessary.")

    print("\nSpot-checking source.url values:")
    print_spot_check("startups", [r.source.url for r in startups], "ycombinator.com")
    print_spot_check("products", [r.source.url for r in products], "ycombinator.com")
    print_spot_check("jobs", [r.source.url for r in job_records])
    print_spot_check("news", [r.source.url for r in news_records])

    if not (startups and products and job_records):
        print("\n** WARNING: one or more entity types produced zero records. **")

    print("\nFast path run complete.")


if __name__ == "__main__":
    asyncio.run(main())
