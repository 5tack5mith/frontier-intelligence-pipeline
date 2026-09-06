"""
Startups + Products ingestion via the Y Combinator company directory.

YC's `/companies` page is a Next.js app whose search UI calls Algolia
directly. The public Algolia app ID and a *search-only* API key are
embedded in the page's inline script as `window.AlgoliaOpts` — this is
intentional on YC's part (Algolia search-only keys are designed to be
safe to expose client-side; they can only read the public index, not
write to it). We fetch the page once to extract those two values, then
call Algolia's REST search endpoint directly rather than scraping
rendered HTML.

One company record from this source feeds two output tabs:
  - StartupRecord (name, employee count, YC profile URL)
  - ProductRecord (same company's primary product; pricingModel is
    UNKNOWN by construction — YC's directory does not expose pricing,
    and TRD.md/schemas.py both call out that guessing a tier here would
    be exactly the kind of hallucination the brief disqualifies for)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_random_exponential

from src.pipeline.schemas import (
    ProductContent,
    ProductRecord,
    PricingModel,
    Source,
    StartupContent,
    StartupContentData,
    StartupRecord,
)
from src.resolution.canonicalizer import EntityCanonicalizer

logger = logging.getLogger("yc_startups")

YC_COMPANIES_PAGE = "https://www.ycombinator.com/companies"
ALGOLIA_OPTS_RE = re.compile(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", re.DOTALL)
ALGOLIA_INDEX = "YCCompany_production"
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 0.5  # self-imposed ~2 req/sec per TRD §2.1


@dataclass
class AlgoliaCreds:
    app_id: str
    api_key: str


@retry(wait=wait_random_exponential(multiplier=1, max=15), stop=stop_after_attempt(3))
async def fetch_algolia_credentials(client: httpx.AsyncClient) -> AlgoliaCreds:
    """Fetch the YC companies page once and extract the embedded public
    Algolia app id / search-only key from `window.AlgoliaOpts`."""
    resp = await client.get(
        YC_COMPANIES_PAGE, timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    match = ALGOLIA_OPTS_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Could not find window.AlgoliaOpts in the YC companies page — "
            "YC may have changed how it embeds Algolia credentials."
        )
    opts = json.loads(match.group(1))
    return AlgoliaCreds(app_id=opts["app"], api_key=opts["key"])


@retry(wait=wait_random_exponential(multiplier=1, max=15), stop=stop_after_attempt(3))
async def _algolia_search(
    client: httpx.AsyncClient, creds: AlgoliaCreds, page: int, hits_per_page: int
) -> dict:
    url = f"https://{creds.app_id}-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "X-Algolia-API-Key": creds.api_key,
        "X-Algolia-Application-Id": creds.app_id,
        "Content-Type": "application/json",
    }
    params = (
        f"query=&hitsPerPage={hits_per_page}&page={page}"
        f'&facetFilters=[["tags:Artificial Intelligence","tags:AI"]]'
    )
    payload = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}
    resp = await client.post(url, headers=headers, json=payload, timeout=20.0)
    resp.raise_for_status()
    return resp.json()["results"][0]


async def fetch_yc_ai_companies(
    client: httpx.AsyncClient, creds: AlgoliaCreds, target_count: int
) -> list[dict]:
    """Paginate the Algolia index, filtered to companies tagged
    'Artificial Intelligence' or 'AI', until target_count unique
    companies (by objectID) are collected."""
    collected: dict[str, dict] = {}
    page = 0
    while len(collected) < target_count:
        result = await _algolia_search(client, creds, page, PAGE_SIZE)
        hits = result.get("hits", [])
        if not hits:
            break
        for hit in hits:
            collected[hit["objectID"]] = hit
        logger.info("YC/Algolia: collected %d/%d so far", len(collected), target_count)
        page += 1
        if page >= result.get("nbPages", 0):
            break
        await asyncio.sleep(REQUEST_DELAY_SECONDS)
    return list(collected.values())[:target_count]


def to_startup_and_product_records(
    companies: list[dict], resolver: EntityCanonicalizer
) -> tuple[list[StartupRecord], list[ProductRecord]]:
    startups: list[StartupRecord] = []
    products: list[ProductRecord] = []

    for c in companies:
        raw_name = c.get("name") or ""
        if not raw_name:
            continue
        slug = c.get("slug", "")
        profile_url = f"https://www.ycombinator.com/companies/{slug}" if slug else c.get("website")
        if not profile_url:
            continue

        canonical_name = resolver.resolve(raw_name, source_record_id=f"yc-{c.get('objectID')}")
        source = Source(name="Y Combinator", url=profile_url)

        startups.append(
            StartupRecord(
                source=source,
                content=StartupContent(
                    entityName=canonical_name,
                    data=StartupContentData(employeeCount=c.get("team_size")),
                ),
            )
        )

        # YC's directory does not expose pricing info anywhere in the
        # payload — never infer a tier from category/tags/description.
        # Per TRD §2.4 / schemas.py, UNKNOWN is the honest default.
        products.append(
            ProductRecord(
                source=source,
                content=ProductContent(
                    startupName=canonical_name,
                    pricingModel=PricingModel.UNKNOWN,
                ),
            )
        )

    return startups, products


if __name__ == "__main__":
    async def _demo():
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        async with httpx.AsyncClient() as client:
            creds = await fetch_algolia_credentials(client)
            print(f"Extracted Algolia creds: app_id={creds.app_id}, key len={len(creds.api_key)}")
            companies = await fetch_yc_ai_companies(client, creds, target_count=5)
            print(f"Fetched {len(companies)} companies")
            resolver = EntityCanonicalizer(seed_path="src/resolution/seed_entities.json")
            startups, products = to_startup_and_product_records(companies, resolver)
            print(startups[0].model_dump_json(indent=2))
            print(products[0].model_dump_json(indent=2))

    asyncio.run(_demo())
