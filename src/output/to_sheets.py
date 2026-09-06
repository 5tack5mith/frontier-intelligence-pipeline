"""
Push the already-written JSONL output files to 6 tabs of a Google
Sheet, using a service account for auth.

Deliberately reuses `output/*.jsonl` as the source of truth rather than
re-running any scraper — those files are already schema-validated (see
`src/output/writer.py`), so this module's only job is flatten + upload.

Env vars required (see .env):
  GOOGLE_SERVICE_ACCOUNT_JSON_PATH — path to the service account key file
  GOOGLE_SHEET_ID                  — the spreadsheet ID (not the full URL)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import gspread
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("to_sheets")

OUTPUT_DIR = Path("output")

TAB_NAMES = {
    "startups": "Startups",
    "products": "Products",
    "research_papers": "Research Papers",
    "jobs": "Jobs",
    "news": "News",
    "entity_mapping_log": "Entity Mapping Log",
}


def _flatten(d: dict, parent_key: str = "", sep: str = ".") -> dict:
    items: dict = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.update(_flatten(v, new_key, sep=sep))
        elif isinstance(v, list):
            items[new_key] = json.dumps(v, default=str)
        else:
            items[new_key] = v
    return items


def load_jsonl_as_dataframe(path: Path) -> pd.DataFrame:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(_flatten(json.loads(line)))
    return pd.DataFrame(rows)


def get_or_create_worksheet(sheet: gspread.Spreadsheet, tab_name: str, rows: int, cols: int) -> gspread.Worksheet:
    try:
        ws = sheet.worksheet(tab_name)
        ws.clear()
        return ws
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=tab_name, rows=max(rows, 1), cols=max(cols, 1))


def _format_cell(v) -> str:
    """Stringify one cell for Sheets output. pandas upcasts an int
    column to float64 the moment any row in it is null (e.g.
    employeeCount is null for some startups) — without this, those
    columns round-trip through the DataFrame as 3000.0 instead of
    3000. Any float that is a whole number is written back as an int
    string; nulls become empty cells rather than "nan"/"<NA>"."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def push_dataframe_to_tab(sheet: gspread.Spreadsheet, tab_name: str, df: pd.DataFrame) -> int:
    header = df.columns.tolist()
    values = [[_format_cell(v) for v in row] for row in df.itertuples(index=False, name=None)]
    ws = get_or_create_worksheet(sheet, tab_name, rows=len(values) + 1, cols=len(header) or 1)
    if header:
        ws.update([header] + values, value_input_option="RAW")
    logger.info("%s: pushed %d data rows", tab_name, len(values))
    return len(values)


def push_all_outputs(only: list[str] | None = None) -> dict[str, int]:
    """
    Push output/*.jsonl to their Sheet tabs. `only` restricts this to a
    subset of base names (e.g. ["startups", "products", "jobs", "news",
    "entity_mapping_log"] for the fast CI workflow, or
    ["research_papers"] for the slow one) — tabs not in `only` are never
    touched (no .clear(), no .update()), so the two scheduled workflows
    can push independently without one clobbering the other's tabs.
    """
    key_path = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON_PATH"]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]

    gc = gspread.service_account(filename=key_path)
    sheet = gc.open_by_key(sheet_id)

    tab_map = TAB_NAMES if only is None else {k: v for k, v in TAB_NAMES.items() if k in only}
    if only is not None:
        unknown = set(only) - set(TAB_NAMES)
        if unknown:
            raise ValueError(f"Unknown --only name(s): {sorted(unknown)}. Valid: {sorted(TAB_NAMES)}")

    results: dict[str, int] = {}
    for base_name, tab_name in tab_map.items():
        jsonl_path = OUTPUT_DIR / f"{base_name}.jsonl"
        if not jsonl_path.exists():
            logger.warning("Missing output file, skipping: %s", jsonl_path)
            results[tab_name] = 0
            continue
        df = load_jsonl_as_dataframe(jsonl_path)
        if df.empty:
            logger.warning("%s has zero rows — pushing empty tab", base_name)
            results[tab_name] = 0
            continue
        results[tab_name] = push_dataframe_to_tab(sheet, tab_name, df)

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help=f"Comma-separated subset of {sorted(TAB_NAMES)} to push (default: all 6)",
    )
    args = parser.parse_args()
    only_list = [s.strip() for s in args.only.split(",")] if args.only else None

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    counts = push_all_outputs(only=only_list)
    print("\nPush complete. Rows written per tab:")
    for tab, n in counts.items():
        print(f"  {tab:22s} {n}")
