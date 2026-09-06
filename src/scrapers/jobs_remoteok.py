"""
Jobs ingestion: RemoteOK's public, unauthenticated JSON API.

`https://remoteok.com/api` is a genuinely public JSON feed (not a
scrape target) — its first array element is always a legal/meta
notice, not a job, and every subsequent element is a real listing with
a native epoch `date` field, which `freshness.normalize_date` already
supports directly.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_random_exponential

from src.pipeline.freshness import is_fresh_or_heuristically_new, normalize_date
from src.pipeline.schemas import JobContent, JobRecord, Source
from src.resolution.canonicalizer import EntityCanonicalizer
from src.scrapers.role_family import classify_role_family

logger = logging.getLogger("jobs_remoteok")

REMOTEOK_API = "https://remoteok.com/api"


@retry(wait=wait_random_exponential(multiplier=1, max=15), stop=stop_after_attempt(3))
async def fetch_remoteok_raw(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(
        REMOTEOK_API, timeout=20.0, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    data = resp.json()
    # First element is a legal/meta notice, not a job listing.
    return [item for item in data if isinstance(item, dict) and "position" in item]


def to_job_records(
    listings: list[dict], resolver: EntityCanonicalizer, now=None
) -> list[JobRecord]:
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    records: list[JobRecord] = []

    for job in listings:
        slug = job.get("slug")
        if not slug:
            continue
        url = f"https://remoteok.com/remote-jobs/{slug}"
        raw_date = job.get("epoch") or job.get("date")
        fresh, reason = is_fresh_or_heuristically_new(raw_date, seen_before=False, now=now)
        if not fresh:
            continue

        company = job.get("company") or "Unknown"
        canonical_company = resolver.resolve(company, source_record_id=f"remoteok-{job.get('id')}")
        tags = job.get("tags", [])
        location = " ".join(tags) if tags else ""
        is_remote = True  # RemoteOK is remote-only by design

        published = normalize_date(raw_date, reference_time=now) or now

        records.append(
            JobRecord(
                source=Source(name="RemoteOK", url=url),
                content=JobContent(
                    company=canonical_company,
                    date=published,
                    is_remote=is_remote,
                    role_family=classify_role_family(job.get("position", ""), tags),
                ),
            )
        )

    return records


if __name__ == "__main__":
    import asyncio

    async def _demo():
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        async with httpx.AsyncClient() as client:
            listings = await fetch_remoteok_raw(client)
            print(f"Fetched {len(listings)} raw RemoteOK listings")
            resolver = EntityCanonicalizer(seed_path="src/resolution/seed_entities.json")
            records = to_job_records(listings, resolver)
            print(f"{len(records)} within 24h freshness window")
            if records:
                print(records[0].model_dump_json(indent=2))

    asyncio.run(_demo())
