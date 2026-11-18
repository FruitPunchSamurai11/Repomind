"""Load normalized data/processed/<repo>/*.jsonl into the SQLite database
and materialize the pr_commit / pr_issue relationship edges.
"""
import json
import sqlite3
from pathlib import Path


def _read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_repository(conn: sqlite3.Connection, processed_dir: Path):
    for r in _read_jsonl(processed_dir / "repository.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO repository VALUES (?, ?, ?, ?, ?, ?)",
            (r["repository_id"], r["owner"], r["name"], r["url"], r.get("default_branch"), r.get("head_sha")),
        )


DEV_COLS = "developer_id, github_login, display_name, email_hash, is_bot, identity_confidence, identity_sources"


def load_developers(conn: sqlite3.Connection, processed_dir: Path, stats: dict | None = None) -> int:
    """Developer ids are scoped per repository (normalization.identity), so a row is only ever
    written by its own repository. A row that already exists with DIFFERENT attributes means an
    unscoped id is shared with another repository (the old, load-order-dependent behaviour); such
    rows are counted in stats["developer_conflicts"] so load_all can report them."""
    n = conflicts = 0
    for d in _read_jsonl(processed_dir / "developers.jsonl"):
        row = (d["developer_id"], d.get("github_login"), d.get("display_name"), d.get("email_hash"),
               int(d.get("is_bot", False)), d.get("identity_confidence"), json.dumps(d.get("identity_sources", [])))
        old = conn.execute(f"SELECT {DEV_COLS} FROM developer WHERE developer_id = ?", (row[0],)).fetchone()
        if old is not None and tuple(old) != row:
            conflicts += 1
        conn.execute("INSERT OR REPLACE INTO developer VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        n += 1
    if stats is not None:
        stats["developer_conflicts"] = conflicts
    return n


def prune_orphan_developers(conn: sqlite3.Connection) -> int:
    """Delete developer rows no commit, issue, PR or review refers to (e.g. rows left by an
    earlier id scheme after every repository has been reloaded)."""
    cur = conn.execute(
        """DELETE FROM developer WHERE developer_id NOT IN (
             SELECT author_developer_id FROM commit_ WHERE author_developer_id IS NOT NULL
             UNION SELECT committer_developer_id FROM commit_ WHERE committer_developer_id IS NOT NULL
             UNION SELECT author_developer_id FROM issue WHERE author_developer_id IS NOT NULL
             UNION SELECT author_developer_id FROM pull_request WHERE author_developer_id IS NOT NULL
             UNION SELECT reviewer_developer_id FROM review WHERE reviewer_developer_id IS NOT NULL)""")
    return cur.rowcount


def load_files(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for f in _read_jsonl(processed_dir / "files.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO file VALUES (?, ?, ?)",
            (f["file_id"], f["repository_id"], f["path"]),
        )
        n += 1
    return n


def load_commits(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for c in _read_jsonl(processed_dir / "commits.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO commit_ VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c["commit_id"],
                c["repository_id"],
                c.get("author_developer_id"),
                c.get("committer_developer_id"),
                c.get("timestamp"),
                c.get("subject"),
                c.get("body"),
                json.dumps(c.get("parent_shas", [])),
                int(c.get("is_merge", False)),
                c.get("raw_source_reference"),
            ),
        )
        n += 1
    return n


def load_commit_files(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for cf in _read_jsonl(processed_dir / "commit_files.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO commit_file VALUES (?, ?, ?, ?, ?)",
            (cf["commit_file_id"], cf["commit_id"], cf["file_id"], cf.get("additions"), cf.get("deletions")),
        )
        n += 1
    return n


def load_issues(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for i in _read_jsonl(processed_dir / "issues.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO issue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i["issue_id"],
                i["repository_id"],
                i.get("github_number"),
                i.get("title"),
                i.get("body"),
                i.get("state"),
                i.get("author_developer_id"),
                i.get("created_at"),
                i.get("closed_at"),
                json.dumps(i.get("labels", [])),
                i.get("raw_source_reference"),
            ),
        )
        n += 1
    return n


def load_pull_requests(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for p in _read_jsonl(processed_dir / "pull_requests.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO pull_request VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["pr_id"],
                p["repository_id"],
                p.get("github_number"),
                p.get("title"),
                p.get("body"),
                p.get("state"),
                p.get("author_developer_id"),
                p.get("created_at"),
                p.get("merged_at"),
                json.dumps(p.get("labels", [])),
                json.dumps(p.get("commit_shas", [])),
                json.dumps(p.get("closing_issue_numbers", [])),
                p.get("raw_source_reference"),
            ),
        )
        for sha in p.get("commit_shas", []):
            conn.execute("INSERT OR IGNORE INTO pr_commit VALUES (?, ?)", (p["pr_id"], sha))
        for issue_number in p.get("closing_issue_numbers", []):
            conn.execute("INSERT OR IGNORE INTO pr_issue VALUES (?, ?)", (p["pr_id"], issue_number))
        n += 1
    return n


def load_reviews(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for r in _read_jsonl(processed_dir / "reviews.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO review VALUES (?, ?, ?, ?, ?, ?)",
            (r["review_id"], r["pr_id"], r.get("reviewer_developer_id"), r.get("state"), r.get("submitted_at"), r.get("body")),
        )
        n += 1
    return n


def load_labels(conn: sqlite3.Connection, processed_dir: Path) -> int:
    n = 0
    for l in _read_jsonl(processed_dir / "labels.jsonl"):
        conn.execute(
            "INSERT OR REPLACE INTO label VALUES (?, ?, ?, ?)",
            (l["label_id"], l["repository_id"], l.get("name"), l.get("color")),
        )
        n += 1
    return n


def load_all(conn: sqlite3.Connection, processed_dir: Path) -> dict:
    processed_dir = Path(processed_dir)
    load_repository(conn, processed_dir)
    dev_stats: dict = {}
    counts = {
        "developers": load_developers(conn, processed_dir, dev_stats),
        "files": load_files(conn, processed_dir),
        "commits": load_commits(conn, processed_dir),
        "commit_files": load_commit_files(conn, processed_dir),
        "issues": load_issues(conn, processed_dir),
        "pull_requests": load_pull_requests(conn, processed_dir),
        "reviews": load_reviews(conn, processed_dir),
        "labels": load_labels(conn, processed_dir),
    }
    counts["developer_conflicts"] = dev_stats["developer_conflicts"]
    counts["orphan_developers_pruned"] = prune_orphan_developers(conn)
    conn.commit()
    return counts
