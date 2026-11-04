"""Tests for the idea scorer's qualification, weighting and comment text."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from score_ideas import (
    MARKER,
    Rules,
    Score,
    Voter,
    load_rules,
    parse_customers,
    qualifies,
    render,
    score,
    voters_from,
)

NOW = datetime(2026, 11, 5, tzinfo=UTC)
RULES = Rules(
    category="Ideas", min_account_days=90, min_contributions=5, customer_weight=3
)


def voter(login: str, days: int, contributions: int) -> Voter:
    return Voter(
        login=login, created_at=NOW - timedelta(days=days), contributions=contributions
    )


@pytest.mark.parametrize(
    ("days", "contributions", "expected"),
    [
        (90, 5, True),
        (89, 5, False),
        (90, 4, False),
        (0, 0, False),
        (3650, 1000, True),
    ],
)
def test_vote_counts_only_from_old_active_accounts(days, contributions, expected):
    assert qualifies(voter("someone", days, contributions), RULES, NOW) is expected


@pytest.mark.parametrize(
    ("voters", "customers", "expected"),
    [
        ([], frozenset(), Score(points=0, qualified=0, from_customers=0, upvotes=7)),
        ([voter("dev", 400, 50)], frozenset(), Score(1, 1, 0, 7)),
        ([voter("Buyer", 400, 50)], frozenset({"buyer"}), Score(3, 1, 1, 7)),
        (
            [voter("fresh", 10, 50), voter("idle", 400, 0)],
            frozenset(),
            Score(0, 0, 0, 7),
        ),
        (
            [voter("dev", 400, 50), voter("DEV", 400, 50)],
            frozenset(),
            Score(1, 1, 0, 7),
        ),
        ([voter("fresh", 10, 50)], frozenset({"fresh"}), Score(0, 0, 0, 7)),
    ],
    ids=[
        "no votes",
        "one vote",
        "customer counts 3x",
        "unqualified skipped",
        "same login once",
        "customer still needs history",
    ],
)
def test_score_weighs_qualified_votes(voters, customers, expected):
    assert score(voters, 7, customers, RULES, NOW) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("Alice, bob ,,", frozenset({"alice", "bob"})),
        (" , ", frozenset()),
    ],
)
def test_customer_secret_parses_to_lowercase_logins(raw, expected):
    assert parse_customers(raw) == expected


def test_comment_carries_marker_score_and_date():
    body = render(Score(points=4, qualified=2, from_customers=1, upvotes=9), NOW)
    assert body.startswith(MARKER)
    assert "**Community score: 4**" in body
    assert "2 qualified thumbs-up votes (1 from customers), 9 upvotes" in body
    assert body.endswith("Scored 2026-11-05.")


def test_single_vote_reads_singular():
    assert "1 qualified thumbs-up vote (" in render(Score(1, 1, 0, 0), NOW)


def test_deleted_accounts_are_skipped():
    nodes = [
        {"user": None},
        {
            "user": {
                "login": "dev",
                "createdAt": "2020-01-01T00:00:00Z",
                "contributionsCollection": {
                    "contributionCalendar": {"totalContributions": 12}
                },
            }
        },
    ]
    found = voters_from(nodes)
    assert [v.login for v in found] == ["dev"]
    assert found[0].created_at == datetime(2020, 1, 1, tzinfo=UTC)


def test_shipped_rules_load():
    rules = load_rules(Path(__file__).resolve().parent.parent / "scoring.toml")
    assert rules == Rules(
        category="Ideas", min_account_days=90, min_contributions=5, customer_weight=3
    )


@pytest.mark.parametrize(
    "toml",
    [
        'category = "Ideas"\nmin_account_days = -1\nmin_contributions_last_year = 5\ncustomer_weight = 3\n',
        'category = "Ideas"\nmin_account_days = 90\nmin_contributions_last_year = 5\ncustomer_weight = 0\n',
    ],
    ids=["negative threshold", "zero weight"],
)
def test_bad_rules_are_refused(tmp_path, toml):
    path = tmp_path / "scoring.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ValueError):
        load_rules(path)


def test_missing_threshold_is_refused(tmp_path):
    path = tmp_path / "scoring.toml"
    path.write_text('category = "Ideas"\n', encoding="utf-8")
    with pytest.raises(KeyError):
        load_rules(path)
