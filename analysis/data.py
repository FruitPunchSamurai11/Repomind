"""Load the raw activity rows every Module 4 metric works from.

One query per signal, all bot-filtered the same way, so metrics never
disagree about which activity they are looking at. Paths under vendored /
third-party directories are dropped by the rule in analysis/exclusions.py
(configurable; recorded in every output).
"""
import sqlite3
from datetime import datetime, timezone

from analysis.exclusions import active_dirs, is_excluded


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_activity(conn: sqlite3.Connection, repository_id: str, include_bots: bool = False,
                  exclude_dirs: tuple[str, ...] | None = None) -> dict:
    """exclude_dirs: None = the configured rule (analysis.exclusions), () = keep every path."""
    bot = "" if include_bots else "AND d.is_bot = 0"

    commits = conn.execute(
        f"""
        SELECT c.commit_id, c.author_developer_id, c.timestamp, f.path, cf.additions, cf.deletions
        FROM commit_file cf
        JOIN commit_ c ON c.commit_id = cf.commit_id
        JOIN file f ON f.file_id = cf.file_id
        JOIN developer d ON d.developer_id = c.author_developer_id
        WHERE c.repository_id = ? {bot}
        """,
        (repository_id,),
    ).fetchall()

    pr_authored = conn.execute(
        f"""
        SELECT DISTINCT p.author_developer_id, p.pr_id, f.path
        FROM pull_request p
        JOIN pr_commit pc ON pc.pr_id = p.pr_id
        JOIN commit_file cf ON cf.commit_id = pc.commit_id
        JOIN file f ON f.file_id = cf.file_id
        JOIN developer d ON d.developer_id = p.author_developer_id
        WHERE p.repository_id = ? {bot}
        """,
        (repository_id,),
    ).fetchall()

    reviewed = conn.execute(
        f"""
        SELECT DISTINCT r.reviewer_developer_id, p.pr_id, f.path
        FROM review r
        JOIN pull_request p ON p.pr_id = r.pr_id
        JOIN pr_commit pc ON pc.pr_id = p.pr_id
        JOIN commit_file cf ON cf.commit_id = pc.commit_id
        JOIN file f ON f.file_id = cf.file_id
        JOIN developer d ON d.developer_id = r.reviewer_developer_id
        WHERE p.repository_id = ? AND r.reviewer_developer_id IS NOT p.author_developer_id {bot}
        """,
        (repository_id,),
    ).fetchall()

    issues_resolved = conn.execute(
        f"""
        SELECT DISTINCT p.author_developer_id, pi.issue_number, f.path
        FROM pull_request p
        JOIN pr_issue pi ON pi.pr_id = p.pr_id
        JOIN pr_commit pc ON pc.pr_id = p.pr_id
        JOIN commit_file cf ON cf.commit_id = pc.commit_id
        JOIN file f ON f.file_id = cf.file_id
        JOIN developer d ON d.developer_id = p.author_developer_id
        WHERE p.repository_id = ? {bot}
        """,
        (repository_id,),
    ).fetchall()

    dirs = active_dirs() if exclude_dirs is None else exclude_dirs
    keep = lambda rows, idx: [tuple(r) for r in rows if not is_excluded(r[idx], dirs)]
    return {
        "commits": keep(commits, 3),
        "pr_authored": keep(pr_authored, 2),
        "reviewed": keep(reviewed, 2),
        "issues_resolved": keep(issues_resolved, 2),
    }
