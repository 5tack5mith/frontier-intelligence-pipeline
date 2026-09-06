"""
Shared per-source pipeline steps, extracted so run_all.py (full local
run), run_fast.py (startups+jobs+news, every 6h in CI), and
run_papers.py (research papers, every 2 days in CI) can each compose
the subset they need without duplicating scraper-orchestration logic.

Note: only run_startups_and_products and run_jobs touch
EntityCanonicalizer. run_research_papers and run_news never resolve an
entity name, so they never contribute to the Entity Mapping Log — the
log is entirely a product of the "fast" path. This is what makes the
fast/slow workflow split safe: the slow (papers) run has nothing to
write to the Entity Mapping Log tab, so it never needs to touch it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

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

logger = logging.getLogger("pipeline.steps")

STARTUP_TARGET = 1000         # brief's full target (1,732 AI-tagged available on YC/Algolia)
PAPER_TARGET = 1000           # brief's full target
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


def print_spot_check(name: str, urls: list[str], expected_domain: str | None = None, sample_size: int = 5) -> None:
    import random

    sample = random.sample(urls, min(sample_size, len(urls))) if urls else []
    print(f"\n  [{name}] {len(sample)} sampled:")
    for url in sample:
        flag = ""
        if expected_domain and expected_domain not in url:
            flag = f"  ** FLAG: expected domain containing {expected_domain!r} **"
        print(f"    {url}{flag}")
