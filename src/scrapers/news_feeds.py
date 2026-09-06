"""
News ingestion: 5 AI-focused RSS/Atom feeds, full-text extraction via
trafilatura, 24-hour freshness filtering via src/pipeline/freshness.py.

All 5 feed URLs below were verified live at build time (see TRD.md
§2.3 for the candidate list). One correction to the TRD's guessed URL:
The Verge's AI feed actually lives at
`/rss/ai-artificial-intelligence/index.xml`, not the `/artificial-intelligence/rss/...`
path the TRD guessed — verified by probing alternates against the
live site. VentureBeat's feed intermittently returns 429 under
default httpx headers; a browser-like User-Agent plus the module's
own retry/backoff resolves it (Content Distribution/WAF quirk, not a
dead feed).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import feedparser
import httpx
import trafilatura
from tenacity import retry, stop_after_attempt, wait_random_exponential

from src.pipeline.freshness import is_fresh_or_heuristically_new, normalize_date
from src.pipeline.schemas import NewsContent, NewsRecord, Source

logger = logging.getLogger("news_feeds")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

NEWS_SOURCES = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
]

ARTICLE_FETCH_DELAY_SECONDS = 0.5


@retry(wait=wait_random_exponential(multiplier=1, max=15), stop=stop_after_attempt(3))
async def _fetch(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url, timeout=20.0, headers={"User-Agent": BROWSER_UA}, follow_redirects=True)
    resp.raise_for_status()
    return resp


async def verify_feed_live(client: httpx.AsyncClient, url: str) -> bool:
    """Confirm a feed URL actually resolves and returns feed-shaped
    content before committing to it, per TRD §2.3 / the task's Step 4
    instruction not to silently force a broken source."""
    try:
        resp = await _fetch(client, url)
    except Exception as e:
        logger.warning("Feed unreachable: %s (%s)", url, e)
        return False
    parsed = feedparser.parse(resp.content)
    return len(parsed.entries) > 0


async def fetch_feed_entries(client: httpx.AsyncClient, source_name: str, feed_url: str) -> list[dict]:
    resp = await _fetch(client, feed_url)
    parsed = feedparser.parse(resp.content)
    entries = []
    for e in parsed.entries:
        raw_date = None
        if getattr(e, "published", None):
            raw_date = e.published
        elif getattr(e, "updated", None):
            raw_date = e.updated
        entries.append(
            {
                "source_name": source_name,
                "title": e.get("title", "").strip(),
                "link": e.get("link"),
                "raw_date": raw_date,
                "summary": e.get("summary", ""),
            }
        )
    return entries


async def extract_full_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await _fetch(client, url)
    except Exception as e:
        logger.warning("Could not fetch article %s: %s", url, e)
        return ""
    text = trafilatura.extract(resp.text, url=url) or ""
    return text


async def fetch_all_news(
    client: httpx.AsyncClient, seen_urls: set[str] | None = None
) -> tuple[list[NewsRecord], list[tuple[str, str]]]:
    """
    Returns (records, substitutions). `substitutions` is a list of
    (intended_source_name, reason) for any TRD-listed source that
    turned out dead at build time and had to be swapped — empty if
    all 5 verified sources were used as-is.
    """
    seen_urls = seen_urls or set()
    now = datetime.now(timezone.utc)
    records: list[NewsRecord] = []
    substitutions: list[tuple[str, str]] = []

    for source_name, feed_url in NEWS_SOURCES:
        live = await verify_feed_live(client, feed_url)
        if not live:
            substitutions.append((source_name, f"feed dead/unreachable at {feed_url}"))
            logger.warning("Skipping dead feed for %s", source_name)
            continue

        entries = await fetch_feed_entries(client, source_name, feed_url)
        logger.info("%s: %d entries from feed", source_name, len(entries))

        for entry in entries:
            if not entry["link"]:
                continue
            fresh, reason = is_fresh_or_heuristically_new(
                entry["raw_date"], seen_before=entry["link"] in seen_urls, now=now
            )
            if not fresh:
                continue

            full_text = await extract_full_text(client, entry["link"])
            if not full_text:
                full_text = entry["summary"]
            await asyncio.sleep(ARTICLE_FETCH_DELAY_SECONDS)

            published = normalize_date(entry["raw_date"], reference_time=now) or now

            records.append(
                NewsRecord(
                    source=Source(name=source_name, url=entry["link"]),
                    content=NewsContent(
                        title=entry["title"],
                        full_text=full_text,
                        published_date=published,
                    ),
                )
            )

    return records, substitutions


if __name__ == "__main__":
    async def _demo():
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        async with httpx.AsyncClient() as client:
            records, subs = await fetch_all_news(client)
            print(f"Fetched {len(records)} fresh (<=24h) news records")
            print(f"Substitutions needed: {subs}")
            if records:
                print(records[0].model_dump_json(indent=2)[:800])

    asyncio.run(_demo())
