"""Score Idea discussions by qualified thumbs-up votes and post each score."""

import json
import logging
import os
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GRAPHQL_URL = "https://api.github.com/graphql"
MARKER = "<!-- hyperi-idea-score -->"
logger = logging.getLogger("score_ideas")

IDEAS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    discussionCategories(first: 25) { nodes { id name } }
    discussions(
      first: 25, after: $after, orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id number title url upvoteCount category { name }
        reactions(content: THUMBS_UP, first: 100) {
          nodes { user { login createdAt
            contributionsCollection { contributionCalendar { totalContributions } } } }
        }
        comments(first: 50) { nodes { id body viewerDidAuthor } }
      }
    }
  }
}
"""

ISSUES_QUERY = """
query($q: String!, $after: String) {
  search(type: ISSUE, query: $q, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        number repository { nameWithOwner }
        reactions(content: THUMBS_UP, first: 100) {
          nodes { user { login createdAt
            contributionsCollection { contributionCalendar { totalContributions } } } }
        }
      }
    }
  }
}
"""

ADD_COMMENT = """
mutation($id: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $id, body: $body}) { comment { id } }
}
"""

UPDATE_COMMENT = """
mutation($id: ID!, $body: String!) {
  updateDiscussionComment(input: {commentId: $id, body: $body}) { comment { id } }
}
"""


class MissingFieldError(LookupError):
    """The org has not created the issue field scores are written to."""


@dataclass(frozen=True, slots=True)
class Rules:
    """Scoring thresholds from scoring.toml.

    Attributes:
        category: Discussion category that holds ideas.
        min_account_days: Youngest account age whose vote counts.
        min_contributions: Fewest contributions in the last year whose vote counts.
        customer_weight: How many times a customer's vote counts.
        issue_threshold: Score at which an idea gets a tracked issue.
        issue_label: Label on those issues.
        score_field: Org issue field that holds an issue's score.
        exclude_repos: Repos whose issues are never scored.
        exclude_prefixes: Repo name prefixes whose issues are never scored.

    """

    category: str
    min_account_days: int
    min_contributions: int
    customer_weight: int
    issue_threshold: int = 10
    issue_label: str = "community-idea"
    score_field: str = "Community score"
    exclude_repos: frozenset[str] = frozenset()
    exclude_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Voter:
    """A GitHub account that reacted with a thumbs-up.

    Attributes:
        login: GitHub login.
        created_at: When the account was created.
        contributions: Contributions in the last year.

    """

    login: str
    created_at: datetime
    contributions: int


@dataclass(frozen=True, slots=True)
class Score:
    """The weighted result for one idea.

    Attributes:
        points: Weighted total of qualified votes.
        qualified: Votes that met the thresholds.
        from_customers: Qualified votes from customer logins.
        upvotes: Raw Discussion upvotes, shown but not weighted.

    """

    points: int
    qualified: int
    from_customers: int
    upvotes: int


def load_rules(path: Path) -> Rules:
    """Read the thresholds file.

    Args:
        path: Path to scoring.toml.

    Returns:
        The scoring rules.

    Raises:
        ValueError: If a threshold is negative or the customer weight is below 1.

    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    rules = Rules(
        category=str(raw["category"]),
        min_account_days=int(raw["min_account_days"]),
        min_contributions=int(raw["min_contributions_last_year"]),
        customer_weight=int(raw["customer_weight"]),
        issue_threshold=int(raw.get("issue_threshold", 10)),
        issue_label=str(raw.get("issue_label", "community-idea")),
        score_field=str(raw.get("score_field", "Community score")),
        exclude_repos=frozenset(raw.get("exclude_repos", ())),
        exclude_prefixes=tuple(raw.get("exclude_prefixes", ())),
    )
    if rules.min_account_days < 0 or rules.min_contributions < 0:
        raise ValueError("thresholds must not be negative")
    if rules.customer_weight < 1:
        raise ValueError("customer_weight must be at least 1")
    return rules


def parse_customers(raw: str) -> frozenset[str]:
    """Turn the comma-separated customer secret into lowercase logins."""
    logins = (part.strip().lower() for part in raw.split(","))
    return frozenset(login for login in logins if login)


def qualifies(voter: Voter, rules: Rules, now: datetime) -> bool:
    """Return True when the account is old and active enough for its vote to count."""
    old_enough = (now - voter.created_at).days >= rules.min_account_days
    return old_enough and voter.contributions >= rules.min_contributions


def score(
    voters: Iterable[Voter],
    upvotes: int,
    customers: Set[str],
    rules: Rules,
    now: datetime,
) -> Score:
    """Weigh the thumbs-up votes on one idea.

    Args:
        voters: Accounts that reacted with a thumbs-up.
        upvotes: The Discussion's raw upvote count.
        customers: Lowercase customer logins.
        rules: Scoring thresholds.
        now: The time to measure account age against.

    Returns:
        The idea's score.

    """
    points = 0
    qualified = 0
    from_customers = 0
    unique = {voter.login.lower(): voter for voter in voters}
    for login, voter in unique.items():
        if not qualifies(voter, rules, now):
            continue
        qualified += 1
        if login in customers:
            from_customers += 1
            points += rules.customer_weight
        else:
            points += 1
    return Score(
        points=points,
        qualified=qualified,
        from_customers=from_customers,
        upvotes=upvotes,
    )


def render(result: Score, now: datetime) -> str:
    """Write the score comment, marked so the next run updates it in place."""
    votes = "vote" if result.qualified == 1 else "votes"
    return (
        f"{MARKER}\n**Community score: {result.points}**\n\n"
        f"{result.qualified} qualified thumbs-up {votes} "
        f"({result.from_customers} from customers), {result.upvotes} upvotes. "
        "Votes count from accounts with some GitHub history. "
        f"Scored {now:%Y-%m-%d}."
    )


def issue_body(title: str, url: str, result: Score) -> str:
    """Write the tracked issue's body, keyed to the discussion by its URL."""
    return (
        f"<!-- hyperi-idea: {url} -->\n"
        f"Community idea: [{title}]({url})\n\n"
        f"**Community score: {result.points}** ({result.qualified} qualified votes, "
        f"{result.from_customers} from customers). Vote and discuss on the idea. "
        "This issue tracks it for planning."
    )


def rest(token: str, method: str, path: str, payload: Mapping | None = None) -> object:
    """Call the GitHub REST API and return the decoded JSON."""
    request = urllib.request.Request(  # noqa: S310
        f"https://api.github.com{path}",
        data=json.dumps(dict(payload)).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw) if raw else None


def in_scope(repo: str, rules: Rules) -> bool:
    """Return True for a repo whose issues are scored, never an excluded one."""
    name = repo.split("/", 1)[1]
    excluded = name in rules.exclude_repos
    return not excluded and not name.startswith(tuple(rules.exclude_prefixes))


def field_id(token: str, org: str, name: str) -> int:
    """Find the org issue field the issue scores are written to.

    Raises:
        MissingFieldError: If the org has no issue field with that name.

    """
    try:
        fields = rest(token, "GET", f"/orgs/{org}/issue-fields")
    except urllib.error.HTTPError as error:
        # 403 means the app has not been granted the org's issue fields yet.
        if error.code != 403:
            raise
        raise MissingFieldError(f"app cannot read {org} issue fields") from error
    for field in fields:
        if field["name"] == name:
            return int(field["id"])
    raise MissingFieldError(f"no '{name}' issue field in {org}")


def write_field(token: str, repo: str, number: int, field: int, points: int) -> bool:
    """Set the issue's score field.

    Returns:
        False when the field already holds that score.

    """
    path = f"/repos/{repo}/issues/{number}/issue-field-values"
    current = rest(token, "GET", path) or []
    held = [v.get("value") for v in current if v.get("issue_field_id") == field]
    if held and held[0] is not None and float(held[0]) == points:
        return False
    values = {"issue_field_values": [{"field_id": field, "value": points}]}
    rest(token, "POST", path, values)
    return True


def score_issues(
    token: str,
    org: str,
    rules: Rules,
    customers: Set[str],
    now: datetime,
) -> int:
    """Score thumbs-up votes on open public issues and write each to the org field.

    Returns:
        How many issue fields were set.

    """
    # SHORTCUT: an issue whose last vote is withdrawn keeps its old score.
    # upgrade: once someone asks, also list issues by field value and zero them.
    field = field_id(token, org, rules.score_field)
    query = f"org:{org} is:issue is:open is:public reactions:>=1"
    written = 0
    after = None
    while True:
        variables = {"q": query, "after": after}
        page = graphql(token, ISSUES_QUERY, variables)["search"]
        for issue in page["nodes"]:
            repo = issue["repository"]["nameWithOwner"]
            if not in_scope(repo, rules):
                continue
            voters = voters_from(issue["reactions"]["nodes"])
            result = score(voters, 0, customers, rules, now)
            if write_field(token, repo, issue["number"], field, result.points):
                written += 1
                logger.info("%s#%s scored %s", repo, issue["number"], result.points)
        if not page["pageInfo"]["hasNextPage"]:
            return written
        after = page["pageInfo"]["endCursor"]


def track(token: str, repo: str, rules: Rules, idea: Mapping, result: Score) -> None:
    """Open or update the idea's tracked issue once it reaches the threshold."""
    if result.points < rules.issue_threshold:
        return
    query = f"/repos/{repo}/issues?labels={rules.issue_label}&state=all&per_page=100"
    marker = f"<!-- hyperi-idea: {idea['url']} -->"
    existing = [i for i in rest(token, "GET", query) if marker in (i.get("body") or "")]
    body = issue_body(idea["title"], idea["url"], result)
    if not existing:
        title = f"Idea: {idea['title']}"
        new = {"title": title, "body": body, "labels": [rules.issue_label]}
        rest(token, "POST", f"/repos/{repo}/issues", new)
    elif existing[0]["body"] != body:
        number = existing[0]["number"]
        rest(token, "PATCH", f"/repos/{repo}/issues/{number}", {"body": body})


def voters_from(reactions: Iterable[Mapping]) -> list[Voter]:
    """Build voters from reaction nodes, skipping deleted accounts."""
    found = []
    for node in reactions:
        user = node.get("user")
        if not user:
            continue
        calendar = user["contributionsCollection"]["contributionCalendar"]
        found.append(
            Voter(
                login=user["login"],
                created_at=datetime.fromisoformat(user["createdAt"]),
                contributions=int(calendar["totalContributions"]),
            )
        )
    return found


def graphql(token: str, query: str, variables: Mapping) -> dict:
    """Run one GraphQL request against GitHub.

    Raises:
        RuntimeError: If GitHub answers with errors.

    """
    payload = json.dumps({"query": query, "variables": dict(variables)}).encode()
    request = urllib.request.Request(  # noqa: S310
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = json.load(response)
    if body.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
    return body["data"]


def write_score(token: str, idea: Mapping, body: str) -> bool:
    """Add or update the idea's score comment.

    Returns:
        False when the existing comment already says the same thing.

    """
    mine = [
        comment
        for comment in idea["comments"]["nodes"]
        if comment["viewerDidAuthor"] and MARKER in comment["body"]
    ]
    if not mine:
        graphql(token, ADD_COMMENT, {"id": idea["id"], "body": body})
        return True
    # The date line changes every run, so compare everything before it.
    if mine[0]["body"].split("Scored")[0] == body.split("Scored")[0]:
        return False
    graphql(token, UPDATE_COMMENT, {"id": mine[0]["id"], "body": body})
    return True


def run(
    token: str,
    repo: str,
    rules: Rules,
    customers: Set[str],
    now: datetime,
) -> int:
    """Score every idea in the repo and write changed scores back.

    Returns:
        How many comments were added or updated.

    Raises:
        RuntimeError: If the repo has no discussion category named in the rules.

    """
    owner, name = repo.split("/", 1)
    written = 0
    after = None
    while True:
        variables = {"owner": owner, "name": name, "after": after}
        page = graphql(token, IDEAS_QUERY, variables)["repository"]
        categories = {c["name"] for c in page["discussionCategories"]["nodes"]}
        if rules.category not in categories:
            raise RuntimeError(f"no '{rules.category}' discussion category in {repo}")
        for idea in page["discussions"]["nodes"]:
            if idea["category"]["name"] != rules.category:
                continue
            voters = voters_from(idea["reactions"]["nodes"])
            result = score(voters, idea["upvoteCount"], customers, rules, now)
            if write_score(token, idea, render(result, now)):
                written += 1
                logger.info("idea #%s scored %s", idea["number"], result.points)
            track(token, repo, rules, idea, result)
        page_info = page["discussions"]["pageInfo"]
        if not page_info["hasNextPage"]:
            return written
        after = page_info["endCursor"]


def main() -> int:
    """Run the scorer with the Action's token and repo."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rules = load_rules(Path(__file__).resolve().parent.parent / "scoring.toml")
    customers = parse_customers(os.environ.get("CUSTOMER_GITHUB_LOGINS", ""))
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(UTC)
    written = run(token, repo, rules, customers, now)
    logger.info("updated %d score comments", written)
    # Issue scores need the org app's token, which reaches repos beyond this one.
    app_token = os.environ.get("APP_TOKEN", "")
    if not app_token:
        logger.warning("APP_TOKEN unset, issue scores skipped")
        return 0
    org = repo.split("/", 1)[0]
    try:
        fields = score_issues(app_token, org, rules, customers, now)
    except MissingFieldError as error:
        logger.warning("%s, issue scores skipped", error)
        return 0
    logger.info("updated %d issue score fields", fields)
    return 0


if __name__ == "__main__":
    sys.exit(main())
