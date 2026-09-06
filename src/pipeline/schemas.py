"""
Canonical data models for the GraphOne / FrontierAtlas ingestion pipeline.

These map 1:1 to the schemas specified in the assignment brief. Every
scraper module must emit records that validate against these models
before they are written to output. This is intentional friction: a
field-shape bug should fail loudly here, not silently at Sheet-export
time.

Design rule: prefer `None` / "UNKNOWN" over fabricating a plausible
value. A null field is honest. A guessed field is a hallucination risk
and grounds for disqualification per the brief.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Shared sub-objects
# ---------------------------------------------------------------------------

class Source(BaseModel):
    name: str = Field(..., description="Name of the source site, e.g. 'Y Combinator'")
    url: str = Field(..., description="Original source URL. Must be a real, resolvable URL.")

    @field_validator("url")
    @classmethod
    def url_must_look_real(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"source.url must be an absolute http(s) URL, got: {v!r}")
        return v


class PricingModel(str, Enum):
    FREE = "FREE"
    FREEMIUM = "FREEMIUM"
    PAID = "PAID"
    ENTERPRISE = "ENTERPRISE"
    UNKNOWN = "UNKNOWN"  # deliberate addition beyond the brief's 4 values —
    # see TRD §3.3: we refuse to force a guess into one of the 4 real values.


class RecordType(str, Enum):
    STARTUP = "STARTUP"
    PRODUCT = "PRODUCT"
    RESEARCH_PAPER = "RESEARCH_PAPER"
    JOB = "JOB"


SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

class StartupContentData(BaseModel):
    employeeCount: Optional[int] = Field(
        default=None, description="Number of employees, if available. Null if unknown."
    )


class StartupContent(BaseModel):
    entityName: str = Field(..., description="Canonical startup name (post entity-resolution)")
    data: StartupContentData = Field(default_factory=StartupContentData)


class StartupRecord(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    recordType: RecordType = RecordType.STARTUP
    source: Source
    content: StartupContent
    collectedAt: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

class ProductContent(BaseModel):
    startupName: str = Field(..., description="Canonical startup name this product belongs to")
    pricingModel: PricingModel = PricingModel.UNKNOWN


class ProductRecord(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    recordType: RecordType = RecordType.PRODUCT
    source: Source
    content: ProductContent
    collectedAt: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Research Paper
# ---------------------------------------------------------------------------

class ResearchPaperContent(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    paper_url: str = Field(..., description="Link to the arXiv abstract/PDF page")
    github_url: Optional[str] = Field(
        default=None, description="Associated code repo URL, if a confident match was found"
    )
    github_stars: Optional[int] = Field(
        default=None, description="Current GitHub stars. Null if no repo match — never estimate."
    )
    published_date: datetime


class ResearchPaperRecord(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    recordType: RecordType = RecordType.RESEARCH_PAPER
    source: Source
    content: ResearchPaperContent
    collectedAt: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

class JobContent(BaseModel):
    company: str = Field(..., description="Canonicalized company name")
    date: datetime = Field(..., description="Normalized publication date, ISO-8601")
    is_remote: bool
    role_family: str = Field(..., description="e.g. 'Engineering', 'Data', 'Design', 'Sales'")


class JobRecord(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    recordType: RecordType = RecordType.JOB
    source: Source
    content: JobContent
    collectedAt: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# News (not in the brief's schema table, but needed for the News tab —
# kept structurally consistent with the others)
# ---------------------------------------------------------------------------

class NewsContent(BaseModel):
    title: str
    full_text: str
    published_date: datetime
    is_remote: Optional[bool] = None  # n/a, kept absent in practice


class NewsRecord(BaseModel):
    schemaVersion: str = SCHEMA_VERSION
    recordType: str = "NEWS"
    source: Source
    content: NewsContent
    collectedAt: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Entity Mapping Log (6th output tab)
# ---------------------------------------------------------------------------

class MatchMethod(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    MANUAL_SEED = "manual_seed"


class EntityMappingLogEntry(BaseModel):
    raw_name: str
    canonical_name: str
    source_record_id: Optional[str] = None
    match_method: MatchMethod
    confidence: Optional[float] = Field(
        default=None, description="Fuzzy match score 0-100 if match_method == fuzzy"
    )


if __name__ == "__main__":
    # Smoke test — validate one of each record type constructs cleanly.
    s = StartupRecord(
        source=Source(name="Y Combinator", url="https://www.ycombinator.com/companies/openai"),
        content=StartupContent(entityName="OpenAI", data=StartupContentData(employeeCount=3000)),
    )
    p = ProductRecord(
        source=Source(name="Y Combinator", url="https://www.ycombinator.com/companies/openai"),
        content=ProductContent(startupName="OpenAI", pricingModel=PricingModel.FREEMIUM),
    )
    rp = ResearchPaperRecord(
        source=Source(name="arXiv", url="https://arxiv.org/abs/2301.00001"),
        content=ResearchPaperContent(
            title="Example Paper",
            authors=["Jane Doe"],
            paper_url="https://arxiv.org/abs/2301.00001",
            github_url=None,
            github_stars=None,
            published_date=utc_now(),
        ),
    )
    j = JobRecord(
        source=Source(name="RemoteOK", url="https://remoteok.com/remote-jobs/12345"),
        content=JobContent(company="Acme AI", date=utc_now(), is_remote=True, role_family="Engineering"),
    )
    print(s.model_dump_json(indent=2))
    print(p.model_dump_json(indent=2))
    print(rp.model_dump_json(indent=2))
    print(j.model_dump_json(indent=2))
    print("All schemas validate OK.")
