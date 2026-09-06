# Technical Requirements Document
## GraphOne / FrontierAtlas — Intelligence Graph Ingestion Pipeline (Intern Trial Task)

**Author:** [Your name]
**Date:** [Submission date]
**Scope:** 3-day take-home trial — reduced-volume real pipeline + documented scale strategy

---

## 0. Read this first — one correction to the brief

The assignment's example URL for Papers with Code (`https://paperswithcode.co/paper/98456`) points to a **domain that isn't the real one and doesn't resolve**. Papers with Code (the real site was `paperswithcode.com`) **was shut down by Meta in July 2025** and now redirects to Hugging Face's "Trending Papers." This isn't a workaround-able typo — the source no longer exists in the form the brief describes.

**Decision:** Replace Papers with Code with **arXiv (official API) + GitHub REST API** for the research-papers vertical. This is arguably a *better* source (arXiv is the origin of the papers; GitHub gives live, real-time star counts, which is exactly what the brief asks for under "dynamic metrics"). Mention this substitution explicitly and prominently in your README and architecture doc — reviewers set this trap intentionally to see if you verify sources instead of hallucinating around a dead link. Flagging it correctly is a strong signal, not a weakness to hide.

---

## 1. Objective & Philosophy

Build a **small, fully real, end-to-end pipeline** across all 6 phases, prove zero hallucination, and **argue scale** rather than brute-force it. Every row in every output sheet must trace to a working source URL.

**Target volumes for this trial (not the brief's 1,000/1,000/1,000):**

| Entity | Trial target | Brief's stated target | Why reduced |
|---|---|---|---|
| Startups | 150–300 | 1,000 | No single clean bulk API hits 1,000 in reasonable time without proxy infra |
| Products | 150–300 | 1,000 | Derived from the same startup source; scale ceiling matches startups |
| Research Papers | 200–400 | 1,000 | Bounded by GitHub API rate limit (5,000/hr) shared across the whole run |
| Jobs (24h fresh) | All found (uncapped) | All found | Brief already says "all," no target mismatch |
| News (24h fresh) | All found (uncapped) | All found | Same |

State this table explicitly in your architecture doc's opening paragraph, with the "how we'd scale to 500k" argument in Section VI (see §7 below). Reviewers are told in the brief itself that "you are not expected to scrape 500k records for this trial" — you are not cutting a corner, you are following the brief's own instruction.

---

## 2. Confirmed Real Data Sources

All of the below were verified as currently live, public, and requiring no paid proxy service. Use these exactly — do not substitute scraped HTML where a clean API/feed exists, since APIs are faster, more reliable, and demonstrate better engineering judgment than fighting anti-bot systems for no reason.

### 2.1 Startups & Products — Y Combinator Directory

- **Source:** `https://www.ycombinator.com/companies` — backed by a **public Algolia search index** that the website's own filter UI calls directly. No login, no API key, no proxy needed.
- **How to get the Algolia credentials:** The public app ID and search-only API key are embedded in the YC companies page's JS bundle (these are meant to be public — Algolia "search-only" keys are safe to expose client-side, that's the whole design of that key type). Fetch the page once, extract the `algoliaAppId` / `algoliaApiKey` values from the embedded JSON/script tags, then call Algolia's REST search endpoint directly:
  ```
  POST https://{APP_ID}-dsn.algolia.net/1/indexes/*/queries
  Headers: X-Algolia-API-Key, X-Algolia-Application-Id
  ```
- **Filter to AI companies:** query with `industry:AI` or free-text `"AI"` plus facet filters YC exposes (batch, industry, tags, isHiring).
- **Fields available per company:** name, one-liner, long description, website, batch (e.g. W24), status (Active/Acquired/Public), team size, location, industries/tags, YC profile URL, hiring flag.
- **This one source covers three of your six output tabs:**
  - **Startups tab** → company name, employee count (team size), source URL = YC profile page.
  - **Products tab** → treat each startup's primary product as one row; `pricingModel` will need to be inferred/enriched (see §2.4) since YC doesn't expose pricing — mark as `"UNKNOWN"` where not determinable rather than guessing, since inventing a pricing tier is exactly the kind of hallucination the brief disqualifies for.
  - Optionally cross-reference `isHiring` flag as a lightweight secondary signal for the Jobs tab, but don't rely on it as one of your "5 job boards" — it doesn't give full job postings.
- **Rate limiting:** Algolia is generous, but self-impose ~2 req/sec with backoff to be a good citizen — no published hard limit, but don't hammer it.

### 2.2 Research Papers — arXiv (official API) + GitHub REST API

- **arXiv API:** `http://export.arxiv.org/api/query` — official, free, no key required.
  - Query params: `search_query=cat:cs.AI` (or `cs.LG`, `cs.CL`, `cs.CV`), `sortBy=submittedDate`, `sortOrder=descending`, `start`, `max_results` (paginate in batches of 100).
  - Returns Atom XML: title, authors, abstract, PDF link, arXiv ID, categories, published/updated timestamps — directly maps to your `ResearchPaper` schema.
  - **Rate limit:** arXiv asks for ~1 request per 3 seconds (be polite, no hard published cap but this is the community norm — respect it or risk IP block).
- **Finding the GitHub repo for a paper:** arXiv does not link to code. Two viable approaches, in order of preference:
  1. Search GitHub's code/repo search API for the paper's arXiv ID or exact title (`GET /search/repositories?q="<arxiv_id>"` or title text) — works for well-known papers whose repos mention the ID in their README.
  2. Where no direct match is found, leave `github_url` and `github_stars` **null** rather than guessing a plausible-looking repo — a wrong repo mapped to a paper is a hallucination-adjacent error and risks disqualification.
- **GitHub REST API for stars:** `GET /repos/{owner}/{repo}` → `stargazers_count`. Authenticated: **5,000 requests/hour**; unauthenticated: 60/hour. **Use a personal access token** — trivial to generate, free, no special scopes needed for public repo reads.
- **Budget math:** ~400 papers × (1 search call + 1 repo-detail call) ≈ 800 GitHub calls — comfortably inside the 5,000/hr authenticated budget with room for retries.

### 2.3 News — 5 AI-focused sources (RSS-first)

Prefer sites with **RSS/Atom feeds** — they hand you a real, structured `pubDate` for free, which directly solves the "24-hour freshness" and date-normalization requirement without fragile HTML date-parsing. Recommended set (verify feed URLs are live at build time, as these can change):
1. **TechCrunch AI** — `https://techcrunch.com/category/artificial-intelligence/feed/`
2. **VentureBeat AI** — `https://venturebeat.com/category/ai/feed/`
3. **MIT Technology Review AI** — check `/feed` on their AI section
4. **The Verge AI** — `https://www.theverge.com/artificial-intelligence/rss/index.xml`
5. **Ars Technica AI** — check their tag-feed for AI/ML content

For full-text extraction beyond the RSS summary, fetch the article URL and extract with a readability-style library (see §4 stack). If a source lacks a reliable feed, fall back to scraping its listing page, but treat this as the exception, not the default — you have 5 slots, spend them on feed-having sources first.

### 2.4 Jobs — RemoteOK confirmed; 4 more needed

- **RemoteOK: `https://remoteok.com/api`** — a genuinely public, unauthenticated **JSON** endpoint (not a scrape target at all — it's a real feed). Returns id, position, company, tags, location, salary range, description, apply URL, and an epoch `date` field — solves freshness natively.
- **For the remaining 4 job boards**, prioritize sources with similar public JSON feeds over HTML scraping wherever possible (reduces Phase V anti-bot burden). At build time, check for public feeds/APIs from other remote/tech job boards before falling back to HTML scraping + Playwright. If you must scrape HTML, isolate that code behind the same interface as the API-based sources so the pipeline doesn't care which method fed it.
- **is_remote / role_family fields:** derive `is_remote` from tags/location text (e.g., contains "remote" or board is remote-only by design); derive `role_family` via simple keyword classification (Engineering, Data, Design, Product, Sales, etc.) — a small rules-based classifier is fine here, doesn't need an LLM call.

### 2.5 Entity Resolution Seed List

Build a ~50-entry canonical mapping by hand from well-known names, e.g.:
```
"OpenAI" ← ["OpenAI", "OpenAI, Inc.", "Open AI", "OpenAI Inc"]
"Anthropic" ← ["Anthropic", "Anthropic PBC", "Anthropic, PBC"]
"Google DeepMind" ← ["DeepMind", "Google DeepMind", "Alphabet - DeepMind"]
```
Seed this from names you already recognize in your scraped YC/arXiv-author-affiliation data so the demo actually resolves something real, not an empty table.

---

## 3. Backend / Data Schema

Use the schemas exactly as specified in the brief — don't deviate, since the evaluator will likely validate against these field names directly.

### 3.1 Common envelope (all record types)
```json
{
  "schemaVersion": "1.0",
  "recordType": "STARTUP | PRODUCT | RESEARCH_PAPER | JOB",
  "source": { "name": "string", "url": "string (required, must resolve)" },
  "content": { "...type-specific fields, see below..." },
  "collectedAt": "ISO-8601 timestamp"
}
```

### 3.2 Startup
| Field | Type | Notes |
|---|---|---|
| content.entityName | string | canonicalized via Phase IV resolver |
| content.data.employeeCount | integer, nullable | from YC team_size; null if unknown — never fabricate |

### 3.3 Product
| Field | Type | Notes |
|---|---|---|
| content.startupName | string | canonical name, FK-style link to Startup entity |
| content.pricingModel | enum: FREE / FREEMIUM / PAID / ENTERPRISE / **UNKNOWN** | Add UNKNOWN as a 5th practical value in your own implementation even though the brief lists 4 — document this deviation explicitly as "no hallucinated pricing" rather than silently forcing a guess into one of the 4 |

### 3.4 Research Paper
| Field | Type | Notes |
|---|---|---|
| content.title, authors, paper_url | — | direct from arXiv Atom feed |
| content.github_url | string, nullable | null if no confident match found |
| content.github_stars | integer, nullable | null if no repo found; never estimate |
| content.published_date | ISO-8601 | arXiv `<published>` field |

### 3.5 Job
| Field | Type | Notes |
|---|---|---|
| content.company | string | canonicalized |
| content.date | ISO-8601 | normalized from source (epoch, relative string, or explicit date) |
| content.is_remote | boolean | derived |
| content.role_family | string | derived via keyword rules |

### 3.6 Entity Mapping Log (for the 6th sheet tab)
```json
{ "raw_name": "OpenAI, Inc.", "canonical_name": "OpenAI", "source_record_id": "...", "match_method": "exact | fuzzy | manual_seed" }
```

---

## 4. Recommended Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best async + data ecosystem overlap |
| HTTP/async | `httpx` (async client) + `asyncio` | Modern, supports HTTP/2, connection pooling |
| HTML parsing (news fallback) | `selectolax` or `BeautifulSoup4` | Fast; BS4 if you want familiarity |
| Full-text extraction | `trafilatura` | Purpose-built readability extraction, handles messy article HTML well |
| Feed parsing | `feedparser` | Handles RSS/Atom edge cases, relative/malformed dates |
| Date normalization | `dateparser` | Handles "2 hours ago" and messy formats out of the box — don't hand-roll regex for this |
| Rate limiting/backoff | `tenacity` | Decorator-based retry with exponential backoff + jitter, clean code |
| LLM calls | Direct `httpx` calls to each provider's REST endpoint (Gemini, Groq, DeepSeek) — avoid heavy SDKs for a 3-day trial | Keeps the fallback-chain logic transparent and easy to demo/explain in review |
| Fuzzy matching (entity resolution) | `rapidfuzz` | Fast Levenshtein-family matching for the canonicalization step |
| Storage (trial) | SQLite via `sqlalchemy`, or even flat JSONL files per entity type | No need for Postgres infra for this volume; argue Postgres+pgvector or a graph DB (Neo4j) in the architecture doc for the 500k+ case |
| Output | `gspread` (Google Sheets API) or `pandas.to_csv` + manual import | gspread if you want it scripted end-to-end |
| Concurrency | `asyncio.Semaphore` per source to cap concurrent requests | Prevents accidentally DoS-ing a source or triggering bot defenses |

---

## 5. Implementation Plan (sequenced for Claude Code)

Build in this order — each phase produces a working, testable artifact before moving to the next, so you always have something demoable even if time runs out.

### Step 1 — Project scaffold (~30 min)
```
src/
  scrapers/
    yc_startups.py
    arxiv_papers.py
    github_enrichment.py
    news_feeds.py
    jobs_remoteok.py
    jobs_other.py          # remaining 4 boards
  llm/
    orchestrator.py         # fallback chain: Gemini -> Groq -> DeepSeek
    chunker.py               # payload truncation logic
  resolution/
    canonicalizer.py
    seed_entities.json
  pipeline/
    run_all.py               # orchestrates all phases, writes JSONL
    schemas.py                # pydantic models matching brief exactly
  output/
    to_sheets.py              # or to_csv.py
README.md
architecture.pdf (or .md, convert later)
requirements.txt
.env.example
```
Define all pydantic models from §3 first — this gives Claude Code a strict contract every scraper must satisfy, catching field-mismatch bugs immediately rather than at Sheet-export time.

### Step 2 — Startups + Products via YC/Algolia (~2–3 hrs)
Build `yc_startups.py`: fetch the YC page once to extract Algolia credentials, then paginate the Algolia search endpoint filtered to AI-tagged companies, until you have 150–300 unique companies. Emit both Startup and Product records per company in one pass.

### Step 3 — Research papers via arXiv + GitHub (~3–4 hrs)
Build `arxiv_papers.py` (paginate `cat:cs.AI` + `cat:cs.LG`, sorted by date, until 200–400 collected), then `github_enrichment.py` (for each paper, attempt a GitHub search match, fetch star count if found, else null). Wire in `tenacity` retry/backoff here since this is where you'll actually hit GitHub's rate limit boundary and need to demonstrate graceful handling.

### Step 4 — News + Jobs freshness pipeline (~3–4 hrs)
Build feed-based news ingestion with `feedparser` + `trafilatura` for full text, and jobs from RemoteOK's real JSON API plus your other 4 chosen boards. Apply the 24-hour filter using `dateparser`-normalized timestamps uniformly across both.

### Step 5 — LLM orchestration layer (~3–4 hrs)
This is weighted 25% — invest real care here even though it's "just code":
- Build the 3-tier fallback chain (try provider A, on failure/429/500 fall to B, then C).
- Build the chunker: truncate input text to a token-safe budget *before* sending, keeping the most information-dense parts (e.g., keep the article's lead paragraphs + any paragraph containing extracted entities, drop boilerplate/nav text — this is where `trafilatura`'s clean extraction earns its keep).
- Explicitly simulate a 429 and a 413 in a test to prove the retry/backoff and chunking actually trigger — screenshot or log this for your README, since "we handle 429s" is a claim reviewers will want evidence for, not just a mentioned feature.

### Step 6 — Entity resolution (~1–2 hrs)
`rapidfuzz`-based fuzzy match against your 50-entry seed list; log every raw→canonical decision (including the match method) into the Entity Mapping Log output.

### Step 7 — Assemble outputs + write architecture doc (~2–3 hrs)
Export all 6 Google Sheet tabs, push code to GitHub with README + architecture.pdf, and do one final pass checking that every single row has a resolvable source URL.

**Total estimated build time: ~16–20 hours of focused work**, fitting inside a 3-day window with slack for debugging and the inevitable "this API changed its response shape" surprise.

---

## 6. Anti-Bot / Phase V Strategy (documentation-first)

Given the source list above, you've architecturally avoided most anti-bot fights: YC's Algolia endpoint, arXiv's API, GitHub's API, RSS feeds, and RemoteOK's JSON API are **all first-party, non-adversarial endpoints**. This is worth stating explicitly in your architecture doc as a deliberate strategy: *"prefer structured first-party APIs/feeds over scraping HTML from anti-bot-hardened surfaces — reserve Playwright/stealth techniques only for sources with no alternative."*

If you do need to demonstrate Playwright-based scraping for one JS-heavy source (recommended: pick just one, to show the capability without burning your time budget):
- Use `playwright-async` in headless Chromium with a realistic user-agent and viewport.
- Add randomized delays between actions (`asyncio.sleep(random.uniform(1,3))`).
- Rotate nothing exotic for a trial — just document that production would add residential proxy rotation (e.g., via a provider like Bright Data or similar) and browser fingerprint randomization, without actually needing to buy that infrastructure for this trial.

---

## 7. Architecture Doc (Phase VI) — talking points to hit

Your 3-page doc should explicitly answer:

1. **Scale to 500k:** "This trial ran single-process against ~5 sources at N records. Scaling is a horizontal-worker problem, not a code problem: each scraper module is already an independent async task; running M parallel worker processes (e.g., via Celery/RQ or simple process pools), each assigned a shard of the source space (e.g., different arXiv categories, different YC batch ranges, different job-board partitions), scales throughput linearly until we hit the *source's* rate limit, at which point the limiting factor becomes API quota, not our infrastructure. For GitHub specifically, this means provisioning multiple authenticated tokens round-robined across workers to multiply the effective 5,000/hr ceiling."
2. **413/429 handling:** describe your actual chunker + `tenacity` backoff-with-jitter implementation, with the real numbers you tested (max payload size chosen, backoff multiplier, max retries).
3. **Dedup across distributed runs:** a content-hash (e.g., SHA-256 of canonical URL, or URL+title) stored in a shared dedup store (Redis set, or a `processed_urls` table) that every worker checks before emitting a record — this is what prevents the same article being processed twice across nodes.
4. **Storage justification:** SQLite/flat-files for this trial's volume; for 500k+ with complex startup↔product↔paper↔person relationships, argue for **Postgres** (relational core + JSONB for flexible fields) plus either **pgvector** (if staying in Postgres, for semantic search over paper abstracts) or a dedicated graph DB like **Neo4j** if the "Intelligence Graph" framing in the brief's own name is taken literally — relationships between founders, startups, and papers are graph-shaped, and a graph DB makes multi-hop queries ("papers by authors who founded YC companies") tractable in ways a relational join chain isn't.

---

## 8. Checklist before you submit

- [ ] Every row in every Sheet tab has a working, real `source.url`
- [ ] `pricingModel` / `github_stars` / any uncertain field uses null/UNKNOWN rather than a plausible guess
- [ ] README explicitly documents the Papers-with-Code → arXiv+GitHub substitution and why
- [ ] At least one demonstrated (logged/screenshotted) instance of the 429 backoff and the 413 chunking actually firing
- [ ] Architecture doc under 3 pages, hits all 4 numbered questions from Phase VI
- [ ] Entity Mapping Log tab has real fuzzy-matched examples, not just the seed list unchanged
- [ ] All jobs/news rows are within 24 hours at time of the final run (rerun once right before submission — data goes stale fast)
