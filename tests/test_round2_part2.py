"""Offline tests for Phase D part 2: table exporters, reference-account
identity merge, backtest outcome B, the CSV lock, and the Phase E export."""
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from analysis.data import load_activity
from evaluation import backtest as bt
from evaluation import export_identity_sample as eis
from evaluation import export_tables as et
from evaluation import identity_reference_merge as irm
from evaluation import run_all, stats
from tests.test_analysis import _fixture_db


# ---------- table exporters (wrap Module 6 / 10) ----------

def test_dataset_and_linkage_exports(tmp_path):
    conn, repo = _fixture_db(tmp_path)
    conn.execute("UPDATE commit_ SET subject='Fix spinner (#12)' WHERE commit_id='c1'")
    conn.execute("UPDATE commit_ SET subject='Mention (#3) mid-subject only' WHERE commit_id='c2'")
    d = et.export_dataset(conn, repo)
    assert d["commits"] == 4 and d["bot_identities"] == 1 and d["span_years"] == pytest.approx(2.42, abs=0.01)
    link = et.export_linkage(conn, repo)
    assert link["non_merge_commits"] == 4 and link["squash_subject_share"] == 0.25
    assert set(et.TABLES["linkage"]) >= set(link)


def test_concentration_risk_and_cochange_exports_have_table_columns(tmp_path):
    conn, repo = _fixture_db(tmp_path)
    for name, fn in (("concentration", et.export_concentration), ("risk", et.export_risk),
                     ("cochange", et.export_cochange)):
        row = fn(conn, repo)
        assert set(row) <= set(et.TABLES[name]), name
    risk = et.export_risk(conn, repo)
    assert risk["components"] == sum(risk[k] for k in ("high", "medium", "low", "dormant", "insufficient_evidence"))
    assert "too little history" in et.export_cochange(conn, repo)["status"]


# ---------- identity merge ----------

def _case(dev, gh, bot=0, in_db=1):
    return {"in_database": str(in_db), "is_bot": str(bot), "github_login": gh, "repomind_developer_id": dev}


def test_reference_mapping_majority_conflicts_and_linked_logins_join():
    cases = [_case("d1", "Alice"), _case("d1", "alice"), _case("d1", "mallory"),   # conflict, majority alice
             _case("d2", "alice"), _case("d3", ""), _case("d4", "bot", bot=1)]
    mapping, conflicts = irm.reference_mapping(cases, {"d1": None, "d2": None, "d5": "ALICE", "d6": "carol"})
    assert mapping == {"d1": "gh:alice", "d2": "gh:alice", "d5": "gh:alice"}
    assert conflicts == 1


def test_compare_detects_merge_effect_on_rankings_and_risk(tmp_path):
    conn, repo = _fixture_db(tmp_path)
    activity = load_activity(conn, repo)
    same = irm.compare(activity, activity)
    assert same["risk_levels_changed"] == 0
    merged = irm.compare(activity, irm.remap_activity(activity, {"alice": "gh:x", "bob": "gh:x"}))
    assert merged["risk_components"] == same["risk_components"]
    assert merged["risk_levels_changed"] >= 0 and isinstance(merged["risk_level_changes"], str)


def test_identity_merge_without_attribution_cases_reports_status(tmp_path, monkeypatch):
    monkeypatch.setattr(irm, "ATTRIBUTION_CASES", tmp_path / "none")
    monkeypatch.setattr(irm, "OUT_CSV", tmp_path / "identity_merge.csv")
    row = irm.run("o/r")
    assert row["status"].startswith("no attribution cases") and "lower bound" in row["bound"]


# ---------- backtest outcome B ----------

def _bt_cases():
    rows = []
    for i in range(24):
        rows.append({"repo": "o/r", "component": f"c{i % 6}", "cutoff": f"{2015 + i % 3}-12-31",
                     "level": ("high", "medium", "low")[i % 3], "active_devs": 1 + i % 3,
                     "recent_commits": 5 + i, "next_year_commits": 0 if i % 4 == 0 else 3,
                     "lost": 1 if i % 4 == 0 or i % 5 == 0 else 0})
    return rows


def test_outcome_b_excludes_zero_commit_next_years():
    row = bt.summarise("o/r", _bt_cases(), n_boot=20, seed=0)
    assert row["cases"] == 24 and row[bt.VARIANT + "cases"] == 18
    assert row[bt.VARIANT + "base_loss_rate"] < row["base_loss_rate"]
    assert all(bt.VARIANT + k in bt.FIELDS for k in bt.STAT_KEYS)


def test_pool_refreshes_rows_and_keeps_repo_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(bt, "OUT_CSV", tmp_path / "backtest.csv")
    stats.write_rows(bt.CASES_DIR / "o__r.csv", bt.CASE_FIELDS, _bt_cases())
    old_fields = [f for f in bt.FIELDS if not f.startswith(bt.VARIANT)]   # a row written by the old version
    stats.upsert_row(bt.OUT_CSV, old_fields, {"repo": "o/r", "status": "ok", "cutoffs": 3, "skipped_dormant": 7})
    bt.run_pool(20, 0)
    rows = {r["repo"]: r for r in stats.read_rows(bt.OUT_CSV)}
    assert rows["o/r"]["skipped_dormant"] == "7" and rows["o/r"][bt.VARIANT + "cases"] == "18"
    assert rows["POOLED"]["cases"] == "24"


# ---------- CSV lock ----------

def test_upsert_breaks_a_stale_lock(tmp_path):
    path = tmp_path / "x.csv"
    lock = tmp_path / "x.csv.lock"
    lock.write_text("")
    old = time.time() - 1000
    os.utime(lock, (old, old))
    stats.upsert_row(path, ["repo", "v"], {"repo": "a", "v": 1})
    assert stats.read_rows(path) == [{"repo": "a", "v": "1"}] and not lock.exists()


def test_fresh_lock_blocks_until_timeout(tmp_path):
    lock = tmp_path / "y.csv.lock"
    lock.write_text("")
    with pytest.raises(TimeoutError):
        with stats.file_lock(tmp_path / "y.csv", timeout=0.3):
            pass


# ---------- Phase E export ----------

@pytest.fixture
def sample_env(tmp_path, monkeypatch):
    cases_dir = tmp_path / "att"
    rows = [{"repo": "o/r", "page": 1, "sha": f"s{i:02d}", "in_database": 1, "is_bot": int(i == 0),
             "github_login": "alice" if i % 2 else "", "repomind_login": "", "repomind_developer_id": f"d{i}"}
            for i in range(10)]
    stats.write_rows(cases_dir / "o__r.csv", ["repo", "page", "sha", "in_database", "is_bot", "github_login",
                                              "repomind_login", "repomind_developer_id"], rows)
    raw = tmp_path / "data" / "raw" / "r"
    raw.mkdir(parents=True)
    raw.joinpath("commits.jsonl").write_text("".join(
        json.dumps({"sha": f"s{i:02d}", "author": {"name": f"N{i}", "email": f"e{i}@x"}}) + "\n" for i in range(10)),
        encoding="utf-8")
    monkeypatch.setattr(eis, "ATTRIBUTION_CASES", cases_dir)
    monkeypatch.setattr(eis, "ROOT", tmp_path)
    return tmp_path


def test_identity_sample_has_blank_labels_and_no_bots(sample_env):
    rows = eis.sample_rows("o/r", 5, seed=0)
    assert len(rows) == 5 and all(r["label_same_person"] == "" and r["notes"] == "" for r in rows)
    assert "s00" not in {r["sha"] for r in rows}                       # the bot commit is excluded
    assert all(r["author_name"].startswith("N") and r["author_email"].endswith("@x") for r in rows)
    assert rows == eis.sample_rows("o/r", 5, seed=0)                  # fixed seed


def test_identity_sample_never_overwrites_without_force(sample_env, monkeypatch):
    out = sample_env / "sheet.csv"
    out.write_text("labels already entered")
    monkeypatch.setattr(sys, "argv", ["x", "--repos", "o/r", "--n", "3", "--out", str(out)])
    with pytest.raises(SystemExit):
        eis.main()
    assert out.read_text() == "labels already entered"


# ---------- runner wiring ----------

def test_new_stages_are_appended_after_part1_and_pass_args(tmp_path, monkeypatch):
    names = [s for s, _ in run_all.STAGES]
    assert names.index("d_attribution") < min(names.index(s) for s in
                                              ("d_dataset", "d_linkage", "d_concentration", "d_risk",
                                               "d_cochange", "d_identity_merge"))
    seen = []
    monkeypatch.setattr(run_all, "MARKER_DIR", tmp_path / "m")
    monkeypatch.setattr(run_all, "LOG_DIR", tmp_path / "l")
    monkeypatch.setattr(run_all, "ROOT", tmp_path)
    monkeypatch.setattr(run_all.subprocess, "run", lambda cmd, **kw: (seen.append(cmd[1:]), SimpleNamespace(returncode=0))[1])
    (tmp_path / "m" / "o__r").mkdir(parents=True)
    assert run_all.run_stage("o/r", "d_dataset", "evaluation.export_tables dataset", 0, None)
    assert seen == [["-m", "evaluation.export_tables", "dataset", "--repo", "o/r"]]
