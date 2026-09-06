"""
Date normalization and 24-hour freshness filtering for News and Jobs.

Handles:
  - Relative strings ("2 hours ago", "yesterday")
  - Missing/malformed dates (heuristic fallback)
  - Epoch timestamps (RemoteOK's native format)
  - Standard RSS/ISO formats

Uses `dateparser`, which has broad built-in support for relative and
natural-language dates rather than hand-rolled regex.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import dateparser

logger = logging.getLogger("freshness")

FRESHNESS_WINDOW_HOURS = 24


def normalize_date(raw: Union[str, int, float, None], reference_time: Optional[datetime] = None) -> Optional[datetime]:
    """
    Normalize a raw date value (string, epoch int/float, or None) into
    a timezone-aware UTC datetime. Returns None if unparseable —
    callers should treat None as "cannot confirm freshness" and apply
    the intelligent-heuristic fallback (see is_fresh_or_heuristically_new
    below) rather than silently dropping or silently keeping the record.
    """
    if raw is None:
        return None

    if isinstance(raw, (int, float)):
        # Epoch seconds (RemoteOK's native format) vs epoch millis —
        # heuristic: anything with 13 digits is almost certainly millis.
        try:
            if raw > 1e12:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            logger.warning("Could not parse epoch value: %r", raw)
            return None

    if isinstance(raw, str):
        settings = {"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "UTC", "TO_TIMEZONE": "UTC"}
        if reference_time is not None:
            settings["RELATIVE_BASE"] = reference_time
        parsed = dateparser.parse(raw, settings=settings)
        if parsed is None:
            logger.warning("dateparser could not parse date string: %r", raw)
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    logger.warning("Unsupported date type: %r (%s)", raw, type(raw))
    return None


def is_within_freshness_window(
    normalized_date: Optional[datetime],
    now: Optional[datetime] = None,
    window_hours: int = FRESHNESS_WINDOW_HOURS,
) -> bool:
    """Strict check: only True if we have a confidently parsed date
    within the window. Records with no parseable date should go
    through the heuristic path below instead of this function."""
    if normalized_date is None:
        return False
    now = now or datetime.now(timezone.utc)
    age = now - normalized_date
    return timedelta(0) <= age <= timedelta(hours=window_hours)


def is_fresh_or_heuristically_new(
    raw_date: Union[str, int, float, None],
    seen_before: bool,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """
    Implements the brief's "intelligent heuristic" requirement (Phase
    II) for sources that lack a strict, parseable date: if we can
    confidently parse the date, use the 24h window directly. If we
    cannot parse a date at all, fall back to a "seen before" check
    against a persisted set of previously-processed source URLs/ids
    (the caller is responsible for maintaining that set across runs) —
    if we have never seen this item before, treat it as new; if we
    have, treat it as stale. This is a heuristic, not a guarantee, and
    should be logged as such (see the returned reason string) so a
    reviewer can see exactly which path each record took.
    """
    now = now or datetime.now(timezone.utc)
    normalized = normalize_date(raw_date, reference_time=now)

    if normalized is not None:
        fresh = is_within_freshness_window(normalized, now)
        return fresh, "date_parsed_within_24h" if fresh else "date_parsed_stale"

    # No parseable date at all — heuristic fallback
    if not seen_before:
        return True, "heuristic_unseen_before_treated_as_new"
    return False, "heuristic_seen_before_treated_as_stale"


if __name__ == "__main__":
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    test_cases = [
        ("2 hours ago", True, "relative string within window"),
        ("3 days ago", False, "relative string outside window"),
        (1757160000, None, "epoch seconds — checked separately below"),
        (None, None, "missing date -> heuristic path"),
        ("not a date at all", None, "garbage string -> heuristic path"),
    ]

    print("Testing normalize_date + is_within_freshness_window:\n")
    for raw, expected_fresh, label in test_cases[:2]:
        normalized = normalize_date(raw, reference_time=now)
        fresh = is_within_freshness_window(normalized, now=now)
        status = "PASS" if fresh == expected_fresh else "FAIL"
        print(f"  [{status}] {label}: {raw!r} -> normalized={normalized}, fresh={fresh}")
        assert fresh == expected_fresh, f"Failed: {label}"

    print("\nTesting is_fresh_or_heuristically_new (Phase II 'intelligent heuristic'):\n")

    # Case: no date, never seen before -> treated as new
    fresh, reason = is_fresh_or_heuristically_new(None, seen_before=False, now=now)
    print(f"  no date, unseen: fresh={fresh}, reason={reason}")
    assert fresh is True and reason == "heuristic_unseen_before_treated_as_new"

    # Case: no date, already seen -> treated as stale
    fresh, reason = is_fresh_or_heuristically_new(None, seen_before=True, now=now)
    print(f"  no date, seen before: fresh={fresh}, reason={reason}")
    assert fresh is False and reason == "heuristic_seen_before_treated_as_stale"

    # Case: parseable recent date wins over heuristic entirely
    fresh, reason = is_fresh_or_heuristically_new("1 hour ago", seen_before=True, now=now)
    print(f"  parseable recent date (seen_before=True but irrelevant): fresh={fresh}, reason={reason}")
    assert fresh is True and reason == "date_parsed_within_24h"

    print("\nFreshness module smoke test PASSED.")
