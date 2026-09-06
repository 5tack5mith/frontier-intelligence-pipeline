"""
One-off targeted repair: re-fetch + re-resolve only the entity-bearing
outputs (startups, products, jobs, entity_mapping_log) after the
EntityCanonicalizer's WRatio->ratio + min-length fix, WITHOUT re-running
the ~69-minute research-papers GitHub enrichment, which doesn't touch
EntityCanonicalizer at all and is therefore unaffected by that bug.

Not a permanent pipeline module — a fixed run_all.py run would do the
same thing at full cost; this exists purely to avoid re-paying the
GitHub rate-limit tax for a fix that has nothing to do with papers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.output.writer import validate_and_write, write_plain_dicts
from src.resolution.canonicalizer import EntityCanonicalizer
from src.scrapers.jobs_other import fetch_all_other_jobs
from src.scrapers.jobs_other import to_job_records as jobs_other_to_records
from src.scrapers.jobs_remoteok import fetch_remoteok_raw
from src.scrapers.jobs_remoteok import to_job_records as remoteok_to_records
from src.scrapers.yc_startups import (
    fetch_algolia_credentials,
    fetch_yc_ai_companies,
    to_startup_and_product_records,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("refix_entities")

STARTUP_TARGET = 1000
OUTPUT_DIR = Path("output")
SEED_ENTITIES_PATH = "src/resolution/seed_entities.json"


async def main():
    resolver = EntityCanonicalizer(seed_path=SEED_ENTITIES_PATH)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        logger.info("Re-fetching YC/Algolia startups (target %d)...", STARTUP_TARGET)
        creds = await fetch_algolia_credentials(client)
        companies = await fetch_yc_ai_companies(client, creds, target_count=STARTUP_TARGET)
        startups, products = to_startup_and_product_records(companies, resolver)
        logger.info("Startups: %d, Products: %d", len(startups), len(products))

        logger.info("Re-fetching jobs (RemoteOK + 4 boards)...")
        now = datetime.now(timezone.utc)
        remoteok_raw = await fetch_remoteok_raw(client)
        remoteok_records = remoteok_to_records(remoteok_raw, resolver, now=now)
        other_raw, job_subs = await fetch_all_other_jobs(client)
        other_records = jobs_other_to_records(other_raw, resolver, now=now)
        job_records = remoteok_records + other_records
        logger.info("Jobs: %d within 24h", len(job_records))

    entity_log = resolver.export_log()

    logger.info("=== Writing corrected outputs (startups/products/jobs/entity_mapping_log only) ===")
    summaries = {
        "startups": validate_and_write("startups", startups, OUTPUT_DIR),
        "products": validate_and_write("products", products, OUTPUT_DIR),
        "jobs": validate_and_write("jobs", job_records, OUTPUT_DIR),
        "entity_mapping_log": write_plain_dicts("entity_mapping_log", entity_log, OUTPUT_DIR),
    }

    print("\n" + "=" * 70)
    print("REFIX SUMMARY (research_papers.* and news.* untouched — not affected by this bug)")
    print("=" * 70)
    for name, summary in summaries.items():
        print(f"  {name:20s} total={summary['total']:5d}  valid={summary['valid']:5d}  rejected={summary['rejected']:4d}")

    # Sanity-check: verify none of the previously-observed bad merges recur.
    def _method_str(e):
        m = e["match_method"]
        return m.value if hasattr(m, "value") else str(m)

    fuzzy = [e for e in entity_log if _method_str(e) == "fuzzy"]
    print(f"\nFuzzy matches this run: {len(fuzzy)}")
    from collections import Counter
    targets = Counter(e["canonical_name"] for e in fuzzy)
    for name, count in targets.most_common(10):
        print(f"  {count:3d} -> {name}")


if __name__ == "__main__":
    asyncio.run(main())
