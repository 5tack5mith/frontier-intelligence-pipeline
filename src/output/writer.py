"""
Output writer: validates every record against its pydantic schema, then
writes 6 JSONL files + 6 matching CSV files (one pair per entity type).

No Google Sheets credentials are configured for this run (per the
user's instruction to default to CSV), so JSONL is the source of
truth and CSV is a straightforward flattened export for manual import
into Sheets tabs. Records that fail schema validation are rejected and
logged to a `*_rejected.jsonl` file alongside the good output, rather
than silently dropped, so every input record is accounted for.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ValidationError

logger = logging.getLogger("writer")


def _flatten(d: dict, parent_key: str = "", sep: str = ".") -> dict:
    items: dict[str, Any] = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.update(_flatten(v, new_key, sep=sep))
        elif isinstance(v, list):
            items[new_key] = json.dumps(v, default=str)
        else:
            items[new_key] = v
    return items


def validate_and_write(
    name: str,
    records: list[BaseModel],
    output_dir: Path,
) -> dict:
    """
    Validate every record (they are already pydantic model instances,
    so validation is re-confirmed via a round-trip through
    model_validate on the dumped dict — catches any post-construction
    mutation bugs) and write:
      - {name}.jsonl        — one JSON object per line, valid records
      - {name}.csv           — flattened CSV, valid records
      - {name}_rejected.jsonl — raw dict + error, for records that failed

    Returns a summary dict: {total, valid, rejected}.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"{name}.jsonl"
    csv_path = output_dir / f"{name}.csv"
    rejected_path = output_dir / f"{name}_rejected.jsonl"

    model_cls = type(records[0]) if records else None
    valid_rows: list[dict] = []
    rejected_rows: list[dict] = []

    with open(jsonl_path, "w", encoding="utf-8") as jf, open(
        rejected_path, "w", encoding="utf-8"
    ) as rf:
        for record in records:
            raw = record.model_dump(mode="json")
            try:
                if model_cls is not None:
                    model_cls.model_validate(raw)
                jf.write(json.dumps(raw, default=str) + "\n")
                valid_rows.append(_flatten(raw))
            except ValidationError as e:
                logger.warning("Rejected invalid %s record: %s", name, e)
                rf.write(json.dumps({"record": raw, "error": str(e)}, default=str) + "\n")
                rejected_rows.append(raw)

    if valid_rows:
        pd.DataFrame(valid_rows).to_csv(csv_path, index=False)
    else:
        # Still emit an (empty) CSV with no rows so the file always exists.
        pd.DataFrame().to_csv(csv_path, index=False)

    if not rejected_rows:
        rejected_path.unlink(missing_ok=True)

    summary = {
        "total": len(records),
        "valid": len(valid_rows),
        "rejected": len(rejected_rows),
    }
    logger.info("%s: %d/%d records valid, %d rejected", name, summary["valid"], summary["total"], summary["rejected"])
    return summary


def write_plain_dicts(name: str, entries: list[dict], output_dir: Path) -> dict:
    """For the Entity Mapping Log, which is a list of plain dicts (from
    EntityCanonicalizer.export_log()) rather than pydantic models."""
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"{name}.jsonl"
    csv_path = output_dir / f"{name}.csv"

    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for entry in entries:
            jf.write(json.dumps(entry, default=str) + "\n")

    if entries:
        pd.DataFrame([_flatten(e) for e in entries]).to_csv(csv_path, index=False)
    else:
        pd.DataFrame().to_csv(csv_path, index=False)

    return {"total": len(entries), "valid": len(entries), "rejected": 0}


if __name__ == "__main__":
    import tempfile

    from src.pipeline.schemas import JobContent, JobRecord, Source, utc_now

    records = [
        JobRecord(
            source=Source(name="Test", url="https://example.com/job/1"),
            content=JobContent(company="Acme", date=utc_now(), is_remote=True, role_family="Engineering"),
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        summary = validate_and_write("jobs", records, out_dir)
        assert summary == {"total": 1, "valid": 1, "rejected": 0}
        assert (out_dir / "jobs.jsonl").exists()
        assert (out_dir / "jobs.csv").exists()
        print("Writer smoke test PASSED.")
