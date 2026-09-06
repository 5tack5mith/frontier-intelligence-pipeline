"""
Slow path: Research Papers only (arXiv + GitHub enrichment). This is
the ~70-minute leg of the pipeline, almost entirely spent respecting
GitHub's ~30 req/min search-endpoint rate limit while matching 1,000
papers to repos. Meant to run every 2 days in CI (see
.github/workflows/papers_pipeline.yml) rather than every 6 hours —
arXiv's paper set barely shifts in a 6h window, so re-running this
leg that often would mostly re-fetch the same papers at high cost.

Writes/overwrites exactly one output file: research_papers.jsonl/csv.
Never touches EntityCanonicalizer (papers have no company-name field
to resolve) and therefore never writes to entity_mapping_log — that
file is entirely owned by run_fast.py. Does NOT touch startups/
products/jobs/news outputs either.

Run with:  python -m src.pipeline.run_papers
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.output.writer import validate_and_write
from src.pipeline.steps import OUTPUT_DIR, print_spot_check, run_research_papers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_papers")


async def main():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        paper_records = await run_research_papers(client)

    logger.info("=== Writing output (research_papers only) ===")
    OUTPUT_DIR.mkdir(exist_ok=True)
    summary = validate_and_write("research_papers", paper_records, OUTPUT_DIR)

    print("\n" + "=" * 70)
    print("PAPERS PATH SUMMARY (startups/products/jobs/news/entity_mapping_log untouched)")
    print("=" * 70)
    print(f"  research_papers      total={summary['total']:5d}  valid={summary['valid']:5d}  rejected={summary['rejected']:4d}")

    print_spot_check("research_papers", [r.source.url for r in paper_records], "arxiv.org")

    if not paper_records:
        print("\n** WARNING: zero research paper records produced. **")

    print("\nPapers path run complete.")


if __name__ == "__main__":
    asyncio.run(main())
