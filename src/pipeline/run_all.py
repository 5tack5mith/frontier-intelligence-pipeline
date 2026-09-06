"""
Top-level orchestrator: runs every scraper (concurrently where the
sources are independent of each other), resolves every startup/company
name through EntityCanonicalizer, validates every record against its
schema, and writes all 6 output tabs as JSONL + CSV.

Run with:  python -m src.pipeline.run_all
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.output.writer import validate_and_write, write_plain_dicts
from src.resolution.canonicalizer import EntityCanonicalizer
from src.scrapers.arxiv_papers import (
    enrich_papers_with_github,
    fetch_arxiv_papers,
    to_research_paper_records,
)
from src.scrapers.jobs_other import fetch_all_other_jobs
from src.scrapers.jobs_other import to_job_records as jobs_other_to_records
from src.scrapers.jobs_remoteok import fetch_remoteok_raw
from src.scrapers.jobs_remoteok import to_job_records as remoteok_to_records
from src.scrapers.news_feeds import fetch_all_news
from src.scrapers.yc_startups import (
    fetch_algolia_credentials,
    fetch_yc_ai_companies,
    to_startup_and_product_records,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_all")

STARTUP_TARGET = 200          # within TRD's 150-300 trial target
PAPER_TARGET = 300            # within TRD's 200-400 trial target
ARXIV_CATEGORIES = ["cs.AI", "cs.LG"]

OUTPUT_DIR = Path("output")
SEED_ENTITIES_PATH = "src/resolution/seed_entities.json"


async def run_startups_and_products(client: httpx.AsyncClient, resolver: EntityCanonicalizer):
    logger.info("=== Startups + Products (YC/Algolia) ===")
    creds = await fetch_algolia_credentials(client)
    companies = await fetch_yc_ai_companies(client, creds, target_count=STARTUP_TARGET)
    startups, products = to_startup_and_product_records(companies, resolver)
    logger.info("Startups: %d, Products: %d", len(startups), len(products))
    return startups, products


async def run_research_papers(client: httpx.AsyncClient):
    logger.info("=== Research Papers (arXiv + GitHub) ===")
    papers = await fetch_arxiv_papers(ARXIV_CATEGORIES, target_count=PAPER_TARGET, client=client)
    logger.info("arXiv: %d papers fetched, starting GitHub enrichment (rate-limited, will take a while)...", len(papers))
    enriched = await enrich_papers_with_github(papers, client)
    matched = sum(1 for p in enriched if p.get("github_url"))
    logger.info("GitHub enrichment complete: %d/%d papers matched to a repo", matched, len(enriched))
    records = to_research_paper_records(enriched)
    return records


async def run_news(client: httpx.AsyncClient):
    logger.info("=== News (5 sources, 24h freshness) ===")
    records, substitutions = await fetch_all_news(client)
    logger.info("News: %d fresh records", len(records))
    if substitutions:
        logger.warning("News source substitutions needed: %s", substitutions)
    return records, substitutions


async def run_jobs(client: httpx.AsyncClient, resolver: EntityCanonicalizer):
    logger.info("=== Jobs (RemoteOK + 4 boards, 24h freshness) ===")
    now = datetime.now(timezone.utc)

    remoteok_raw = await fetch_remoteok_raw(client)
    remoteok_records = remoteok_to_records(remoteok_raw, resolver, now=now)
    logger.info("RemoteOK: %d raw, %d within 24h", len(remoteok_raw), len(remoteok_records))

    other_raw, substitutions = await fetch_all_other_jobs(client)
    other_records = jobs_other_to_records(other_raw, resolver, now=now)
    logger.info("4 other boards: %d raw, %d within 24h", len(other_raw), len(other_records))
    if substitutions:
        logger.warning("Job board substitutions needed: %s", substitutions)

    return remoteok_records + other_records, substitutions


async def main():
    resolver = EntityCanonicalizer(seed_path=SEED_ENTITIES_PATH)
    substitution_notes: list[tuple[str, str]] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Startups/Products and News/Jobs are independent of each other
        # and of the (slow) arXiv+GitHub pipeline, so run them
        # concurrently; each source internally respects its own rate
        # limit via asyncio.sleep between its own requests.
        (startups, products), (news_records, news_subs), (job_records, job_subs) = await asyncio.gather(
            run_startups_and_products(client, resolver),
            run_news(client),
            run_jobs(client, resolver),
        )
        substitution_notes.extend(news_subs)
        substitution_notes.extend(job_subs)

        # GitHub enrichment is itself rate-limited to ~30 req/min on its
        # search endpoint; running it concurrently with the above would
        # only add contention, not speed, so it runs after.
        paper_records = await run_research_papers(client)

    entity_log = resolver.export_log()

    logger.info("=== Writing outputs ===")
    OUTPUT_DIR.mkdir(exist_ok=True)
    summaries = {
        "startups": validate_and_write("startups", startups, OUTPUT_DIR),
        "products": validate_and_write("products", products, OUTPUT_DIR),
        "research_papers": validate_and_write("research_papers", paper_records, OUTPUT_DIR),
        "jobs": validate_and_write("jobs", job_records, OUTPUT_DIR),
        "news": validate_and_write("news", news_records, OUTPUT_DIR),
        "entity_mapping_log": write_plain_dicts("entity_mapping_log", entity_log, OUTPUT_DIR),
    }

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    for name, summary in summaries.items():
        print(f"  {name:20s} total={summary['total']:5d}  valid={summary['valid']:5d}  rejected={summary['rejected']:4d}")

    if substitution_notes:
        print("\nSource substitutions made:")
        for name, reason in substitution_notes:
            print(f"  - {name}: {reason}")
    else:
        print("\nNo source substitutions were necessary — all TRD-listed sources verified live.")

    print("\nSpot-checking ~5 random source.url values per entity type:")
    spot_check_groups = {
        "startups": [r.source.url for r in startups],
        "products": [r.source.url for r in products],
        "research_papers": [r.source.url for r in paper_records],
        "jobs": [r.source.url for r in job_records],
        "news": [r.source.url for r in news_records],
    }
    expected_domain = {
        "startups": "ycombinator.com",
        "products": "ycombinator.com",
        "research_papers": "arxiv.org",
    }
    for name, urls in spot_check_groups.items():
        sample = random.sample(urls, min(5, len(urls))) if urls else []
        print(f"\n  [{name}] {len(sample)} sampled:")
        for url in sample:
            flag = ""
            expected = expected_domain.get(name)
            if expected and expected not in url:
                flag = f"  ** FLAG: expected domain containing {expected!r} **"
            print(f"    {url}{flag}")

    if not (startups and products and paper_records and job_records):
        print("\n** WARNING: one or more entity types produced zero records. **")

    print("\nRun complete.")


if __name__ == "__main__":
    asyncio.run(main())
