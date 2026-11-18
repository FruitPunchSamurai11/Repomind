"""Path-exclusion rule for vendored / third-party code (Modules 4 and 5).

Files copied in from other projects (Go `vendor/`, `third_party/`,
`node_modules/`, ...) are not the repository's own code: their "authors"
are whoever bumped a dependency, so they distort concentration rankings
and risk levels (e.g. openfaas/faas-netes: 1,749 of 2,118 ranked files
were under vendor/).

A path is excluded when any of its DIRECTORY segments equals one of the
patterns (case-insensitive); file names are never matched. The rule is
applied wherever Module 4/5 activity is loaded (analysis.data.load_activity,
evaluation.backtest.load_commits) and is recorded in every output.

Configure with the environment variable REPOMIND_EXCLUDE_DIRS:
  unset            -> DEFAULT_DIRS
  "none" or ""     -> no exclusion (the behaviour before this rule)
  "a,b,c"          -> exclude directories named a, b or c
"""
import os
import sqlite3

DEFAULT_DIRS = ("vendor", "vendored", "_vendor", "third_party", "third-party", "thirdparty", "3rdparty",
                "node_modules", "bower_components")
ENV = "REPOMIND_EXCLUDE_DIRS"


def active_dirs() -> tuple[str, ...]:
    raw = os.environ.get(ENV)
    if raw is None:
        return DEFAULT_DIRS
    if raw.strip().lower() in ("", "none"):
        return ()
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip())


def rule_label(dirs: tuple[str, ...] | None = None) -> str:
    dirs = active_dirs() if dirs is None else dirs
    return "exclude dirs: " + ",".join(dirs) if dirs else "none"


def is_excluded(path: str, dirs: tuple[str, ...] | None = None) -> bool:
    dirs = active_dirs() if dirs is None else dirs
    if not dirs or not path:
        return False
    return any(seg.lower() in dirs for seg in path.split("/")[:-1])


def excluded_file_count(conn: sqlite3.Connection, repository_id: str, dirs: tuple[str, ...] | None = None) -> int:
    dirs = active_dirs() if dirs is None else dirs
    if not dirs:
        return 0
    return sum(1 for (p,) in conn.execute("SELECT path FROM file WHERE repository_id = ?", (repository_id,))
               if is_excluded(p, dirs))


def describe(conn: sqlite3.Connection, repository_id: str) -> dict:
    """What every output records: the rule and how many of the repo's files it removes."""
    dirs = active_dirs()
    return {"path_exclusion": rule_label(dirs), "excluded_files": excluded_file_count(conn, repository_id, dirs)}
