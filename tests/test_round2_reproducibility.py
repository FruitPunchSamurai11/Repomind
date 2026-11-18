"""Reproducibility: results must not depend on repository load order or on case order."""
import json
import random

from database.connection import get_connection, init_schema
from database.load import load_all
from evaluation import backtest as bt
from evaluation import stats
from normalization.cli import _write_jsonl
from normalization.git_normalizer import normalize_git
from normalization.identity import IdentityResolver


def _commit(sha, name, email, date, path):
    return {"sha": sha, "parents": [], "author": {"name": name, "email": email},
            "committer": {"name": name, "email": email}, "authored_date": date, "committed_date": date,
            "subject": "s", "body": "", "files": [{"path": path, "insertions": 1, "deletions": 0, "lines": 1}],
            "is_merge": False}


# The same two people appear in both repositories, resolved differently:
#   carol: same e-mail, different display name in each repo
#   dave:  a noreply (linked, "high") committer in repo a; only a GitHub login ("medium") in repo b
RAW = {
    "o/a": [_commit("a1", "Carol A", "carol@x.org", "2024-01-01T00:00:00+00:00", "src/x.py"),
            _commit("a2", "dave", "dave@users.noreply.github.com", "2024-02-01T00:00:00+00:00", "src/x.py")],
    "o/b": [_commit("b1", "C. Smith", "carol@x.org", "2024-01-05T00:00:00+00:00", "lib/y.py")],
}
GITHUB_ONLY = {"o/b": ["dave"]}  # e.g. a PR author in repo b


def _processed(tmp_path, repo, scoped):
    owner, name = repo.split("/")
    raw = tmp_path / "raw" / name / "commits.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("".join(json.dumps(c) + "\n" for c in RAW[repo]), encoding="utf-8")
    resolver = IdentityResolver(scope=repo if scoped else None)
    git = normalize_git(raw, owner, name, resolver)
    for login in GITHUB_ONLY.get(repo, []):
        resolver.resolve_github_login(login)
    out = tmp_path / ("scoped" if scoped else "unscoped") / name
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "repository.jsonl", [git["repository"]])
    _write_jsonl(out / "developers.jsonl", list(resolver.developers.values()))
    for key, fname in (("files", "files"), ("commits", "commits"), ("commit_files", "commit_files")):
        _write_jsonl(out / f"{fname}.jsonl", git[key])
    for fname in ("issues", "pull_requests", "reviews", "labels"):
        _write_jsonl(out / f"{fname}.jsonl", [])
    return out


def _load(dirs):
    conn = get_connection(":memory:")
    init_schema(conn)
    reports = [load_all(conn, d) for d in dirs]
    return conn, reports


def _snapshot(conn):
    tables = ["developer", "commit_", "file", "commit_file"]
    data = {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}")) for t in tables}
    linked = {}
    for repo in RAW:  # share of human commits per identity confidence, as in Table II
        rows = conn.execute("SELECT d.identity_confidence FROM commit_ c JOIN developer d "
                            "ON d.developer_id = c.author_developer_id WHERE c.repository_id = ? AND d.is_bot = 0",
                            (repo,)).fetchall()
        linked[repo] = {lv: sum(r[0] == lv for r in rows) / len(rows) for lv in {r[0] for r in rows}}
    names = {repo: sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT d.display_name FROM commit_ c JOIN developer d ON d.developer_id = c.author_developer_id "
        "WHERE c.repository_id = ?", (repo,))) for repo in RAW}
    return data, linked, names


def test_two_repos_loaded_in_both_orders_give_identical_results(tmp_path):
    a, b = _processed(tmp_path, "o/a", True), _processed(tmp_path, "o/b", True)
    ab, rep_ab = _load([a, b])
    ba, rep_ba = _load([b, a])
    assert _snapshot(ab) == _snapshot(ba)
    assert all(r["developer_conflicts"] == 0 for r in rep_ab + rep_ba)
    _, linked, names = _snapshot(ab)
    assert linked["o/a"].get("high") == 0.5                      # dave stays linked in repo a
    assert names == {"o/a": ["Carol A", "dave"], "o/b": ["C. Smith"]}


def test_unscoped_ids_reproduce_the_load_order_bug(tmp_path):
    """Documents the old behaviour the scoping fixes: the last-loaded repo's resolution wins."""
    a, b = _processed(tmp_path, "o/a", False), _processed(tmp_path, "o/b", False)
    ab, rep_ab = _load([a, b])
    ba, _ = _load([b, a])
    assert _snapshot(ab) != _snapshot(ba)
    assert rep_ab[1]["developer_conflicts"] == 2                   # carol's name and dave's confidence
    assert _snapshot(ab)[1]["o/a"].get("high", 0.0) == 0.0        # repo a lost dave's link after b loaded
    assert _snapshot(ba)[1]["o/a"].get("high") == 0.5


def test_reloading_a_repo_prunes_rows_from_an_old_id_scheme(tmp_path):
    conn, _ = _load([_processed(tmp_path, "o/a", False)])
    old_ids = {r[0] for r in conn.execute("SELECT developer_id FROM developer")}
    report = load_all(conn, _processed(tmp_path, "o/a", True))
    new_ids = {r[0] for r in conn.execute("SELECT developer_id FROM developer")}
    assert report["orphan_developers_pruned"] == len(old_ids) and not (old_ids & new_ids)
    referenced = {r[0] for r in conn.execute("SELECT author_developer_id FROM commit_")}
    assert referenced <= new_ids


def test_scoped_ids_differ_per_repo_but_are_stable():
    r1, r2, r3 = IdentityResolver("o/a"), IdentityResolver("o/b"), IdentityResolver("o/a")
    ids = [r.resolve_git_identity("Carol", "carol@x.org") for r in (r1, r2, r3)]
    assert ids[0] != ids[1] and ids[0] == ids[2]
    assert r1.developers[ids[0]]["email_hash"] == r2.developers[ids[1]]["email_hash"]   # e-mail hash is global


# ---------- bootstrap order independence ----------

def _cases():
    rows = []
    for i in range(60):
        rows.append({"repo": "o/r" if i % 2 else "o/s", "component": f"c{i % 9}", "cutoff": f"{2015 + i % 4}-12-31",
                     "level": ("high", "medium", "low")[i % 3], "active_devs": 1 + i % 3,
                     "recent_commits": 5 + i, "next_year_commits": 0 if i % 4 == 0 else 3,
                     "lost": 1 if i % 4 == 0 or i % 7 == 0 else 0})
    return rows


def test_backtest_cis_do_not_depend_on_case_order():
    cases = _cases()
    shuffled = cases[:]
    random.Random(1).shuffle(shuffled)
    as_csv = [{k: str(v) for k, v in c.items()} for c in reversed(cases)]   # what --pool reads back from disk
    a = bt.summarise("POOLED", cases, n_boot=50, seed=0)
    assert a == bt.summarise("POOLED", shuffled, n_boot=50, seed=0)
    b = bt.summarise("POOLED", as_csv, n_boot=50, seed=0)
    assert {k: v for k, v in a.items() if k.endswith(("_lo", "_hi"))} == \
           {k: v for k, v in b.items() if k.endswith(("_lo", "_hi"))}


def test_stratified_bootstrap_does_not_depend_on_group_or_member_order():
    groups = [[{"q": i, "d": (i * 7) % 5 - 2} for i in range(g * 10, g * 10 + 10)] for g in range(3)]
    stat = lambda s: {"mean": sum(x["d"] for x in s) / len(s)}
    base = stats.bootstrap_stratified(groups, stat, 200, 0)
    mixed = [list(reversed(g)) for g in reversed(groups)]
    assert stats.bootstrap_stratified(mixed, stat, 200, 0) == base
    flat = [x for g in groups for x in g]
    assert stats.bootstrap(flat, stat, 200, 0) == stats.bootstrap(list(reversed(flat)), stat, 200, 0)


# ---------- co-change: ties broken by path / commit id, not by set or SQL order ----------

def test_cochange_popularity_baseline_breaks_ties_by_path(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from assist import evaluate as ev
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # training: 10 files changed 7 times each (all tied); test: pairs of those files
    train = [(t0 + timedelta(days=d), frozenset({f"f{d % 10}.py"})) for d in range(70)]
    test = [(t0 + timedelta(days=100 + d), frozenset({f"f{d % 4}.py", f"f{(d + 1) % 4}.py"})) for d in range(30)]  # only f0-f3
    results = []
    for tr in (train, list(reversed(train))):   # same history, different first-seen order of files
        monkeypatch.setattr(ev, "load_commits", lambda *a, _c=tr + test, **k: _c)
        results.append(ev.cochange_eval(None, "o/r", k=3, min_support=1))
    assert results[0] == results[1]


def test_rescore_keeps_answers_and_reports_invalid_questions():
    from evaluation import llm_eval as le
    row = {"repo": "o/r", "condition": "rag", "qid": "pr_about-0", "question_type": "pr_about",
           "question": "What is pull request #7 about?", "expected": "Fix the parser", "answer": "It fixes the parser.",
           "llm_called": "1", "generation_failed": "0", "correct": "1", "abstained": "0", "safe": "0",
           "confidently_wrong": "0", "success": "1", "outcome": "correct"}

    class Conn:
        def __init__(self, title): self.title = title
        def execute(self, *a): return type("C", (), {"fetchone": lambda s: (self.title,) if self.title else None})()
        def close(self): pass

    new, changes = le.rescore([row], conn_for=lambda: Conn("Fix the parser"))
    assert new[0]["answer"] == row["answer"] and new[0]["success"] == "1" and changes == []
    new, changes = le.rescore([row], conn_for=lambda: Conn(None))
    assert changes and changes[0]["validity"] == "PR missing"
