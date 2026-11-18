"""Path-exclusion rule for vendored / third-party code (Modules 4 and 5)."""
from types import SimpleNamespace

from analysis import exclusions as ex
from analysis.data import load_activity
from evaluation import backtest as bt
from evaluation import run_all
from tests.test_analysis import _fixture_db


def test_directory_segments_match_but_file_names_do_not(monkeypatch):
    monkeypatch.delenv(ex.ENV, raising=False)
    assert ex.is_excluded("vendor/github.com/x/y.go")
    assert ex.is_excluded("pkg/Third_Party/lib.c")
    assert ex.is_excluded("web/node_modules/a/index.js")
    assert not ex.is_excluded("src/vendor.py")            # a file named vendor is the repo's own code
    assert not ex.is_excluded("docs/vendoring.md")
    assert not ex.is_excluded("")


def test_rule_is_configurable_and_labelled(monkeypatch):
    monkeypatch.delenv(ex.ENV, raising=False)
    assert ex.active_dirs() == ex.DEFAULT_DIRS and ex.rule_label().startswith("exclude dirs: vendor,")
    monkeypatch.setenv(ex.ENV, "none")
    assert ex.active_dirs() == () and ex.rule_label() == "none" and not ex.is_excluded("vendor/a.go")
    monkeypatch.setenv(ex.ENV, "Gen, build")
    assert ex.active_dirs() == ("gen", "build") and ex.is_excluded("x/gen/a.go") and not ex.is_excluded("vendor/a.go")


def _with_vendor_file(tmp_path):
    conn, repo = _fixture_db(tmp_path)
    conn.execute("INSERT INTO file (file_id, repository_id, path) VALUES ('f9', ?, 'vendor/lib/x.go')", (repo,))
    conn.execute("INSERT INTO commit_file VALUES ('9', 'c3', 'f9', 50, 0)")
    return conn, repo


def test_load_activity_drops_excluded_paths_and_describe_counts_them(tmp_path, monkeypatch):
    monkeypatch.delenv(ex.ENV, raising=False)
    conn, repo = _with_vendor_file(tmp_path)
    paths = {r[3] for r in load_activity(conn, repo)["commits"]}
    assert "vendor/lib/x.go" not in paths and "src/a.py" in paths
    assert "vendor/lib/x.go" in {r[3] for r in load_activity(conn, repo, exclude_dirs=())["commits"]}
    assert not any(ex.is_excluded(r[2]) for r in load_activity(conn, repo)["pr_authored"])
    assert ex.describe(conn, repo) == {"path_exclusion": ex.rule_label(), "excluded_files": 1}
    monkeypatch.setenv(ex.ENV, "none")
    assert ex.describe(conn, repo) == {"path_exclusion": "none", "excluded_files": 0}


def test_backtest_loader_applies_the_rule(tmp_path, monkeypatch):
    monkeypatch.delenv(ex.ENV, raising=False)
    conn, repo = _with_vendor_file(tmp_path)
    comps = set().union(*(c[2] for c in bt.load_commits(conn, repo)))
    assert not any(c.startswith("vendor") for c in comps) and comps
    monkeypatch.setenv(ex.ENV, "none")
    assert any(c.startswith("vendor") for c in set().union(*(c[2] for c in bt.load_commits(conn, repo))))


def test_run_all_preserve_keeps_independent_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(run_all, "MARKER_DIR", tmp_path / "m")
    monkeypatch.setattr(run_all, "LOG_DIR", tmp_path / "l")
    monkeypatch.setattr(run_all, "ROOT", tmp_path)
    monkeypatch.setattr(run_all.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0))
    (tmp_path / "m" / "o__r").mkdir(parents=True)
    for s in ("d_retrieval", "d_attribution", "d_backtest"):
        run_all.marker("o/r", s).write_text("{}")
    stages = [s for s in run_all.STAGES if s[0] == "4_concentration"]
    assert run_all.run_repo("o/r", stages, 0, None, frozenset({"d_retrieval", "d_attribution"}))
    assert run_all.marker("o/r", "d_retrieval").exists() and run_all.marker("o/r", "d_attribution").exists()
    assert not run_all.marker("o/r", "d_backtest").exists()        # dependent stage still invalidated
    assert run_all.marker("o/r", "4_concentration").exists()
