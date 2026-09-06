"""
Independent verification: reads row counts directly back from each tab
of the live Google Sheet (not from output/*.jsonl, not from
to_sheets.py's own "rows pushed" log) to confirm the push actually
landed. Requires GOOGLE_SERVICE_ACCOUNT_JSON_PATH + GOOGLE_SHEET_ID in
.env, same as to_sheets.py.
"""

from __future__ import annotations

import os

import gspread
from dotenv import load_dotenv

load_dotenv()

TAB_NAMES = ["Startups", "Products", "Research Papers", "Jobs", "News", "Entity Mapping Log"]


def main():
    gc = gspread.service_account(filename=os.environ["GOOGLE_SERVICE_ACCOUNT_JSON_PATH"])
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])

    print(f"Sheet: {sh.title}")
    print("=" * 50)
    for tab in TAB_NAMES:
        ws = sh.worksheet(tab)
        values = ws.get_all_values()
        data_rows = len(values) - 1 if values else 0
        cols = len(values[0]) if values else 0
        print(f"  {tab:20s} {data_rows:6d} rows  ({cols} cols)")


if __name__ == "__main__":
    main()
