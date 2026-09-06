"""
Jobs ingestion: 4 job boards beyond RemoteOK, all via public JSON/RSS
feeds rather than HTML scraping (per TRD §2.4: prefer clean feeds/APIs
over scraping HTML wherever a public one exists — reserved only for
sources with no alternative).

Each source's fetch function returns a common intermediate dict shape
so `to_job_records` can treat them uniformly; only the fetch layer
needs to know each board's native response shape.

Sources used:
  - Remotive      https://remotive.com/api/remote-jobs   (JSON API)
  - Arbeitnow     https://www.arbeitnow.com/api/job-board-api  (JSON API)
  - Himalayas     https://himalayas.app/jobs/api          (JSON API)
  - WeWorkRemotely (programming category RSS feed)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import httpx
from tenacity import retry, stop_after_attempt, wait_random_exponential

from src.pipeline.freshness import is_fresh_or_heuristically_new, normalize_date
from src.pipeline.schemas import JobContent, JobRecord, Source
from src.resolution.canonicalizer import EntityCanonicalizer
from src.scrapers.role_family import classify_is_remote, classify_role_family

logger = logging.getLogger("jobs_other")

REMOTIVE_API = "https://remotive.com/api/remote-jobs"
ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"
HIMALAYAS_API = "https://himalayas.app/jobs/api"
WWR_FEED = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


@retry(wait=wait_random_exponential(multiplier=1, max=15), stop=stop_after_attempt(3))
async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url, timeout=20.0, headers={"User-Agent": BROWSER_UA}, follow_redirects=True)
    resp.raise_for_status()
    return resp


# --- Intermediate common shape -----------------------------------------
# {board, company, title, url, raw_date, is_remote, tags}


async def fetch_remotive(client: httpx.AsyncClient) -> list[dict]:
    resp = await _get(client, REMOTIVE_API)
    jobs = resp.json().get("jobs", [])
    out = []
    for j in jobs:
        out.append({
            "board": "Remotive",
            "company": j.get("company_name", "").strip() or "Unknown",
            "title": j.get("title", ""),
            "url": j.get("url"),
            "raw_date": j.get("publication_date"),
            "is_remote": True,  # Remotive is remote-only by design
            "tags": j.get("tags", []),
        })
    return out


async def fetch_arbeitnow(client: httpx.AsyncClient) -> list[dict]:
    resp = await _get(client, ARBEITNOW_API)
    jobs = resp.json().get("data", [])
    out = []
    for j in jobs:
        out.append({
            "board": "Arbeitnow",
            "company": j.get("company_name", "").strip() or "Unknown",
            "title": j.get("title", ""),
            "url": j.get("url"),
            "raw_date": j.get("created_at"),  # epoch seconds
            "is_remote": bool(j.get("remote", False)),
            "tags": j.get("tags", []),
        })
    return out


async def fetch_himalayas(client: httpx.AsyncClient) -> list[dict]:
    resp = await _get(client, HIMALAYAS_API)
    jobs = resp.json().get("jobs", [])
    out = []
    for j in jobs:
        out.append({
            "board": "Himalayas",
            "company": j.get("companyName", "").strip() or "Unknown",
            "title": j.get("title", ""),
            "url": j.get("applicationLink") or j.get("guid"),
            "raw_date": j.get("pubDate"),
            "is_remote": True,  # Himalayas is a remote-only board
            "tags": j.get("categories", []),
        })
    return out


async def fetch_weworkremotely(client: httpx.AsyncClient) -> list[dict]:
    resp = await _get(client, WWR_FEED)
    parsed = feedparser.parse(resp.content)
    out = []
    for e in parsed.entries:
        title_raw = e.get("title", "")
        if ":" in title_raw:
            company, title = title_raw.split(":", 1)
            company, title = company.strip(), title.strip()
        else:
            company, title = "Unknown", title_raw
        tags = [t.get("term", "") for t in e.get("tags", [])] if e.get("tags") else []
        out.append({
            "board": "WeWorkRemotely",
            "company": company,
            "title": title,
            "url": e.get("link"),
            "raw_date": e.get("published"),
            "is_remote": True,  # WWR's programming category is remote-only
            "tags": tags,
        })
    return out


FETCHERS = [
    ("Remotive", fetch_remotive),
    ("Arbeitnow", fetch_arbeitnow),
    ("Himalayas", fetch_himalayas),
    ("WeWorkRemotely", fetch_weworkremotely),
]


async def verify_source_live(client: httpx.AsyncClient, name: str, fetch_fn) -> bool:
    try:
        listings = await fetch_fn(client)
        return len(listings) > 0
    except Exception as e:
        logger.warning("Job board unreachable: %s (%s)", name, e)
        return False


def to_job_records(listings: list[dict], resolver: EntityCanonicalizer, now=None) -> list[JobRecord]:
    now = now or datetime.now(timezone.utc)
    records: list[JobRecord] = []

    for job in listings:
        if not job.get("url"):
            continue
        fresh, reason = is_fresh_or_heuristically_new(job["raw_date"], seen_before=False, now=now)
        if not fresh:
            continue

        canonical_company = resolver.resolve(
            job["company"], source_record_id=f"{job['board']}-{job['url']}"
        )
        published = normalize_date(job["raw_date"], reference_time=now) or now

        records.append(
            JobRecord(
                source=Source(name=job["board"], url=job["url"]),
                content=JobContent(
                    company=canonical_company,
                    date=published,
                    is_remote=job.get("is_remote", classify_is_remote("", job.get("tags"))),
                    role_family=classify_role_family(job["title"], job.get("tags")),
                ),
            )
        )

    return records


async def fetch_all_other_jobs(
    client: httpx.AsyncClient,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Returns (raw_listings, substitutions) across all 4 boards."""
    all_listings: list[dict] = []
    substitutions: list[tuple[str, str]] = []

    for name, fetch_fn in FETCHERS:
        live = await verify_source_live(client, name, fetch_fn)
        if not live:
            substitutions.append((name, "board unreachable or returned zero listings at build time"))
            continue
        listings = await fetch_fn(client)
        logger.info("%s: %d raw listings", name, len(listings))
        all_listings.extend(listings)

    return all_listings, substitutions


if __name__ == "__main__":
    import asyncio

    async def _demo():
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        async with httpx.AsyncClient() as client:
            listings, subs = await fetch_all_other_jobs(client)
            print(f"Fetched {len(listings)} raw listings across 4 boards")
            print(f"Substitutions needed: {subs}")
            resolver = EntityCanonicalizer(seed_path="src/resolution/seed_entities.json")
            records = to_job_records(listings, resolver)
            print(f"{len(records)} within 24h freshness window")
            if records:
                print(records[0].model_dump_json(indent=2))

    asyncio.run(_demo())
