"""
Research paper ingestion: arXiv (official API) for paper metadata,
GitHub REST API for enrichment (associated repo + live star count).

Papers with Code is NOT used — see TRD §0. PWC was shut down by Meta
in July 2025 and the brief's example URL points to a non-existent
domain. arXiv + GitHub is the substitute, and gives us live star
counts natively (arguably better than PWC's static leaderboard data
ever was).
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_random_exponential

from src.pipeline.schemas import ResearchPaperContent, ResearchPaperRecord, Source

logger = logging.getLogger("arxiv_papers")

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_REQUEST_DELAY_SECONDS = 3.0  # arXiv's community norm: ~1 req/3s

GITHUB_API = "https://api.github.com"


def parse_arxiv_feed(xml_text: str) -> list[dict]:
    """Parse an arXiv Atom API response into plain dicts. Pure function,
    no network — testable offline against a saved sample response."""
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        title_el = entry.find("atom:title", ARXIV_NS)
        summary_el = entry.find("atom:summary", ARXIV_NS)
        published_el = entry.find("atom:published", ARXIV_NS)
        authors = [
            a.find("atom:name", ARXIV_NS).text
            for a in entry.findall("atom:author", ARXIV_NS)
            if a.find("atom:name", ARXIV_NS) is not None
        ]
        # Prefer the "alternate" html link as the canonical paper_url
        paper_url = None
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.get("rel") == "alternate":
                paper_url = link.get("href")
                break
        if title_el is None or published_el is None or paper_url is None:
            logger.warning("Skipping malformed arXiv entry (missing required field)")
            continue

        papers.append(
            {
                "title": title_el.text.strip().replace("\n", " "),
                "authors": authors,
                "paper_url": paper_url,
                "published_date": published_el.text.strip(),
                "summary": (summary_el.text or "").strip() if summary_el is not None else "",
            }
        )
    return papers


@retry(wait=wait_random_exponential(multiplier=1, max=20), stop=stop_after_attempt(3))
async def fetch_arxiv_page(
    client: httpx.AsyncClient, category: str, start: int, max_results: int = 100
) -> str:
    resp = await client.get(
        ARXIV_API,
        params={
            "search_query": f"cat:{category}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": start,
            "max_results": max_results,
        },
        timeout=30.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


async def fetch_arxiv_papers(
    categories: list[str], target_count: int, client: httpx.AsyncClient
) -> list[dict]:
    """Paginate across the given arXiv categories until target_count
    unique papers (by paper_url) are collected."""
    collected: dict[str, dict] = {}
    for category in categories:
        start = 0
        page_size = 100
        while len(collected) < target_count:
            xml_text = await fetch_arxiv_page(client, category, start, page_size)
            papers = parse_arxiv_feed(xml_text)
            if not papers:
                break  # exhausted this category
            for p in papers:
                collected[p["paper_url"]] = p
            start += page_size
            logger.info(
                "arXiv %s: collected %d/%d so far", category, len(collected), target_count
            )
            await asyncio.sleep(ARXIV_REQUEST_DELAY_SECONDS)
            if len(collected) >= target_count:
                break
    return list(collected.values())[:target_count]


# Repo-name patterns that overwhelmingly indicate a link-aggregator /
# "awesome list" / daily-arxiv-tracker repo rather than a paper's own
# implementation. GitHub's `"<title>" in:readme` search surfaces these
# constantly, since an aggregator's README legitimately contains
# hundreds of paper titles — none of which it is the "code for". Live
# testing against real arXiv papers showed the SAME aggregator repo
# matching multiple unrelated papers in one run, which is exactly the
# false-positive TRD §2.2 warns is worse than leaving github_url null.
_AGGREGATOR_NAME_PATTERNS = (
    "awesome", "daily-arxiv", "daily-paper", "arxiv-daily", "paper-list",
    "papers-list", "arxiv-sanity", "paper-reading", "weekly-arxiv",
    "arxiv-digest", "reading-list", "paper-notes", "daily", "radar",
    "digest", "tracker", "roundup", "papers-of-the-day", "hf-daily",
    "paper-radar", "research-radar", "arxiv-papers", "ai-papers",
    "paper-bot", "papers-feed", "arxiv-feed",
)


def _looks_like_aggregator_repo(repo: dict) -> bool:
    name = (repo.get("name") or "").lower()
    full_name = (repo.get("full_name") or "").lower()
    description = (repo.get("description") or "").lower()

    # GitHub "profile README" repos (owner/owner — the special repo
    # that renders as a user's profile page) are never a paper's own
    # implementation. Live testing found several such profiles padded
    # with dozens of unrelated arXiv paper titles (apparently for
    # search-visibility/spam purposes) — the same profile repo matched
    # multiple different, unrelated papers in one run.
    if "/" in full_name:
        owner, _, repo_name = full_name.partition("/")
        if owner == repo_name:
            return True

    if any(p in name or p in full_name for p in _AGGREGATOR_NAME_PATTERNS):
        return True
    if any(
        p in description
        for p in (
            "curated list", "daily update", "auto-updat", "collection of paper",
            "papers i read", "daily tracking", "papers on arxiv", "tracking of",
            "paper tracker", "arxiv papers", "list of paper",
        )
    ):
        return True
    # "...-papers" / "papers-..." repo names are overwhelmingly listing
    # repos (curated or bot-generated), not a single paper's own code.
    if "papers" in name:
        return True
    # Aggregator/tracker repos are almost always markdown-only (no
    # primary programming language); a real implementation repo has one.
    if repo.get("language") is None:
        return True
    return False


_BOT_GENERATED_README_MARKERS = (
    "auto-generated by", "auto generated by", "do not edit manually",
    "this file is generated", "generated automatically", "auto-updated",
    "self-updating", "source: configs/", "source: data/",
)


def _readme_looks_bot_generated(readme_text_lower: str) -> bool:
    """Content-level signal, independent of repo naming: a README that
    announces its own auto-generation (found live in a repo called
    'PaperFlow' — 'AUTO-GENERATED BY PAPERFLOW. DO NOT EDIT MANUALLY.')
    is a listing/tracker repo regardless of what it's named, and this
    generalizes far better than trying to enumerate every possible
    aggregator project name."""
    # Only check the opening of the file — real implementation READMEs
    # sometimes mention "generated" deep in a changelog/CI section,
    # which shouldn't disqualify them.
    head = readme_text_lower[:500]
    return any(marker in head for marker in _BOT_GENERATED_README_MARKERS)


@retry(wait=wait_random_exponential(multiplier=1, max=10), stop=stop_after_attempt(2))
async def find_github_repo_for_paper(
    client: httpx.AsyncClient, title: str, github_token: Optional[str]
) -> Optional[dict]:
    """
    Attempt to find a GitHub repo that is plausibly the paper's own
    implementation. Returns None (not a guess) if no confident match
    is found — see TRD §2.2: a wrong repo mapped to a paper is worse
    than no repo at all.

    Two-stage filter to avoid false positives from aggregator repos
    (awesome-lists, daily-arxiv trackers) whose READMEs legitimately
    contain hundreds of unrelated paper titles:
      1. Reject candidates whose repo name matches a known aggregator
         naming pattern before even considering them.
      2. Require the exact paper title to appear verbatim
         (case-insensitive) in the candidate's actual README content —
         GitHub's search relevance alone is not sufficient evidence.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    # Deliberately NOT sorted by stars: live testing found this buries
    # a paper's own (brand-new, 0-star) repo below unrelated aggregator
    # repos that have accumulated stars over time simply by existing
    # longer — e.g. a real match at position 3 by relevance dropped out
    # of the top 5 entirely once sorted by stars. Default relevance
    # ordering surfaces the actual best textual match first.
    query = f'"{title}" in:readme'
    resp = await client.get(
        f"{GITHUB_API}/search/repositories",
        params={"q": query, "per_page": 10},
        headers=headers,
        timeout=20.0,
    )
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        logger.warning("GitHub rate limit hit during repo search")
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
    if resp.status_code != 200:
        logger.warning("GitHub search failed (%s) for title=%r", resp.status_code, title[:50])
        return None

    items = resp.json().get("items", [])
    title_lower = title.lower().strip()

    for candidate in items:
        if _looks_like_aggregator_repo(candidate):
            continue

        readme_resp = await client.get(
            f"{GITHUB_API}/repos/{candidate['full_name']}/readme",
            headers={**headers, "Accept": "application/vnd.github.raw+json"},
            timeout=20.0,
        )
        if readme_resp.status_code != 200:
            continue
        readme_text = readme_resp.text.lower()
        if title_lower not in readme_text:
            continue
        if _readme_looks_bot_generated(readme_text):
            continue
        return {
            "github_url": candidate["html_url"],
            "github_stars": candidate["stargazers_count"],
        }

    return None


async def enrich_papers_with_github(
    papers: list[dict], client: httpx.AsyncClient
) -> list[dict]:
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        logger.warning(
            "GITHUB_TOKEN not set — GitHub enrichment will run unauthenticated "
            "at 60 req/hr instead of 5000 req/hr. Set GITHUB_TOKEN for real runs."
        )

    for paper in papers:
        try:
            match = await find_github_repo_for_paper(client, paper["title"], github_token)
        except Exception as e:
            logger.warning("GitHub enrichment failed for %r: %s", paper["title"][:50], e)
            match = None

        if match:
            paper["github_url"] = match["github_url"]
            paper["github_stars"] = match["github_stars"]
        else:
            paper["github_url"] = None
            paper["github_stars"] = None

        # GitHub's *search* endpoint (used above) has its own ~30
        # req/min limit, separate from and much tighter than the core
        # API's 5000/hr — this delay is sized for that limit, not the
        # core one.
        await asyncio.sleep(2.2)

    return papers


def to_research_paper_records(papers: list[dict]) -> list[ResearchPaperRecord]:
    records = []
    for p in papers:
        try:
            published = datetime.fromisoformat(p["published_date"].replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Could not parse published_date=%r, skipping", p.get("published_date"))
            continue

        records.append(
            ResearchPaperRecord(
                source=Source(name="arXiv", url=p["paper_url"]),
                content=ResearchPaperContent(
                    title=p["title"],
                    authors=p["authors"],
                    paper_url=p["paper_url"],
                    github_url=p.get("github_url"),
                    github_stars=p.get("github_stars"),
                    published_date=published,
                ),
            )
        )
    return records


if __name__ == "__main__":
    # Offline smoke test: parse a realistic saved arXiv response with no
    # network call, proving the XML parsing logic is correct before it's
    # ever pointed at the live API (which this sandbox can't reach —
    # export.arxiv.org is not in this environment's egress allowlist;
    # this is a sandbox limitation, not a problem with the source).
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2601.01234v1</id>
    <updated>2026-01-15T18:00:00Z</updated>
    <published>2026-01-15T18:00:00Z</published>
    <title>Scaling Laws for Retrieval-Augmented Generation</title>
    <summary>We study how retrieval augmentation affects scaling behavior...</summary>
    <author><name>Jane Q. Researcher</name></author>
    <author><name>Alex Kim</name></author>
    <link href="http://arxiv.org/abs/2601.01234v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2601.01234v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>"""

    parsed = parse_arxiv_feed(sample_xml)
    print(f"Parsed {len(parsed)} paper(s):")
    for p in parsed:
        print(f"  title: {p['title']}")
        print(f"  authors: {p['authors']}")
        print(f"  paper_url: {p['paper_url']}")
        print(f"  published_date: {p['published_date']}")

    assert len(parsed) == 1
    assert parsed[0]["title"] == "Scaling Laws for Retrieval-Augmented Generation"
    assert parsed[0]["authors"] == ["Jane Q. Researcher", "Alex Kim"]
    assert parsed[0]["paper_url"] == "http://arxiv.org/abs/2601.01234v1"

    # Also prove the pydantic conversion works end to end
    parsed[0]["github_url"] = None
    parsed[0]["github_stars"] = None
    records = to_research_paper_records(parsed)
    assert len(records) == 1
    print("\nConverted to ResearchPaperRecord:")
    print(records[0].model_dump_json(indent=2))

    print("\narXiv parser smoke test PASSED (offline, no network).")
