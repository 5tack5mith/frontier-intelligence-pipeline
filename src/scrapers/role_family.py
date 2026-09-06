"""
Small rules-based role-family classifier shared by all job scrapers.

Per TRD §2.4: derive `role_family` via simple keyword rules — no LLM
call needed for a field this mechanical.
"""

from __future__ import annotations

_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Data", ("data scientist", "data engineer", "data analyst", "analytics", "machine learning", "ml engineer", "ai engineer", "research scientist")),
    ("Design", ("designer", "ux", "ui/", "product design", "graphic design")),
    ("Product", ("product manager", "product owner", "product lead")),
    ("Sales", ("sales", "account executive", "business development", "bdr", "sdr")),
    ("Marketing", ("marketing", "growth", "seo", "content strategist", "brand")),
    ("Support", ("customer support", "customer success", "support engineer", "help desk")),
    ("Finance", ("accountant", "finance", "controller", "bookkeeper")),
    ("HR", ("recruiter", "people ops", "human resources", "talent acquisition")),
    ("Legal", ("legal counsel", "paralegal", "compliance officer")),
    ("Operations", ("operations", "supply chain", "logistics")),
    ("Engineering", ("engineer", "developer", "programmer", "devops", "sre", "architect", "backend", "frontend", "full stack", "full-stack", "qa", "sdet")),
]

DEFAULT_ROLE_FAMILY = "Other"


def classify_role_family(title: str, tags: list[str] | None = None) -> str:
    """Classify a job's role family from its title (primary signal) and
    tags (secondary signal), via simple substring keyword rules."""
    haystack = " ".join([title or "", " ".join(tags or [])]).lower()

    for role_family, keywords in _KEYWORD_RULES:
        if any(kw in haystack for kw in keywords):
            return role_family

    return DEFAULT_ROLE_FAMILY


def classify_is_remote(location_text: str, tags: list[str] | None = None, board_is_remote_only: bool = False) -> bool:
    if board_is_remote_only:
        return True
    haystack = " ".join([location_text or "", " ".join(tags or [])]).lower()
    return "remote" in haystack or "anywhere" in haystack or "worldwide" in haystack


if __name__ == "__main__":
    cases = [
        ("Senior Backend Engineer", [], "Engineering"),
        ("Data Scientist, NLP", [], "Data"),
        ("Product Designer", [], "Design"),
        ("Account Executive - EMEA", [], "Sales"),
        ("Customer Success Manager", [], "Support"),
        ("Random Title With No Keywords At All", [], "Other"),
    ]
    for title, tags, expected in cases:
        result = classify_role_family(title, tags)
        status = "PASS" if result == expected else "FAIL"
        print(f"  [{status}] {title!r} -> {result} (expected {expected})")
        assert result == expected

    assert classify_is_remote("", ["remote"]) is True
    assert classify_is_remote("Berlin, Germany", []) is False
    assert classify_is_remote("", [], board_is_remote_only=True) is True
    print("\nrole_family smoke test PASSED.")
