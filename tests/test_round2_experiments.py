"""Offline tests for the Phase D experiments (no network, no model download)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evaluation import attribution_check as att
from evaluation import backtest as bt
from evaluation import retrieval_compare as rc
from evaluation import run_all
from evaluation.stats import auc, bootstrap, bootstrap_stratified, wilson
from retrieval.ranking import minmax_fusion

UTC = timezone.utc


# ---------- stats ----------

def test_auc_perfect_reverse_ties_and_undefined():
    assert auc([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert auc([4, 3, 2, 1], [False, False, True, True]) == 0.0
    assert auc([1, 1, 1, 1], [False, True, False, True]) == 0.5
    assert auc([1, 2], [True, True]) is None


def test_wilson_interval_contains_estimate():
    lo, hi = wilson(5, 5)
    assert 0.5 < lo < 1.0 and hi == 1.0
    assert wilson(0, 0) == (None, None)


def test_bootstrap_is_deterministic_and_brackets_mean():
    units = list(range(20))
    stat = lambda s: {"mean": sum(s) / len(s)}
    a, b = bootstrap(units, stat, 200, 1), bootstrap(units, stat, 200, 1)
    assert a == b and a["mean"][0] < 9.5 < a["mean"][1]
    s = bootstrap_stratified([[0, 0], [1, 1, 1]], lambda x: {"n": float(len(x))}, 50, 0)
    assert s["n"] == (5.0, 5.0)  # every resample keeps group sizes


# ---------- backtest ----------

def _c(y, m, dev, *comps):
    return (datetime(y, m, 1, tzinfo=UTC), dev, set(comps))


def test_cutoffs_need_two_years_before_and_one_after():
    cuts = bt.cutoffs(datetime(2015, 6, 1, tzinfo=UTC), datetime(2020, 3, 1, tzinfo=UTC))
    assert [c.year for c in cuts] == [2017, 2018]


def test_cases_use_only_past_commits_and_label_loss():
    commits = []
    for y in (2015, 2016, 2017):          # component a: solo dev who then leaves
        commits += [_c(y, m, "alice", "src/a") for m in (2, 5, 8)]
    for y in (2015, 2016, 2017, 2018, 2019, 2020):   # component b: two devs who keep going
        commits += [_c(y, m, d, "src/b") for m in (3, 9) for d in ("bob", "carol")]
    commits.sort(key=lambda c: c[0])
    cases, info = bt.cases_for_repo("o/r", commits)
    at_2017 = {c["component"]: c for c in cases if c["cutoff"] == "2017-12-31"}
    assert at_2017["src/a"]["lost"] == 1 and at_2017["src/a"]["active_devs"] == 1
    assert at_2017["src/b"]["lost"] == 0 and at_2017["src/b"]["active_devs"] == 2
    assert at_2017["src/a"]["total_commits"] == 9      # nothing after the cutoff is counted
    assert at_2017["src/a"]["level"] in ("medium", "high")
    assert at_2017["src/b"]["level"] == "low"
    # after alice leaves, src/a is dormant at the 2018 cutoff -> not a case
    assert not any(c["component"] == "src/a" and c["cutoff"] == "2018-12-31" for c in cases)
    assert info["skipped_dormant"] >= 1


def test_point_stats_rates_rr_and_auc():
    cases = ([{"level": "high", "active_devs": 1, "recent_commits": 2, "lost": 1}] * 3
             + [{"level": "high", "active_devs": 1, "recent_commits": 2, "lost": 0}]
             + [{"level": "low", "active_devs": 4, "recent_commits": 30, "lost": 1}]
             + [{"level": "low", "active_devs": 4, "recent_commits": 30, "lost": 0}] * 3)
    s = bt.point_stats(cases)
    assert s["loss_high"] == 0.75 and s["loss_low"] == 0.25 and s["rr_high_low"] == 3.0
    assert s["auc_rule"] == s["auc_fewer_active"] == 0.75


def test_summarise_reports_ci_and_components():
    cases = [{"repo": "o/r", "component": f"c{i % 4}", "level": "high" if i % 2 else "low", "next_year_commits": i % 3,
              "active_devs": 1 + i % 3, "recent_commits": i, "lost": i % 2} for i in range(16)]
    row = bt.summarise("o/r", cases, n_boot=50, seed=0)
    assert row["status"] == "ok" and row["components"] == 4
    assert row["auc_rule_lo"] <= row["auc_rule"] <= row["auc_rule_hi"]


# ---------- attribution ----------

def test_last_page_parses_link_header():
    resp = SimpleNamespace(links={"last": {"url": "https://api.github.com/repos/o/r/commits?per_page=100&page=7"}})
    assert att.last_page(resp) == 7
    assert att.last_page(SimpleNamespace(links={})) == 1


def test_fetch_sample_draws_seeded_pages_and_reuses_page_one():
    calls = []

    class Client:
        def get(self, path, params):
            calls.append(params["page"])
            return SimpleNamespace(links={"last": {"url": "x?page=5"}},
                                   json=lambda: [{"sha": f"p{params['page']}", "author": None}])

    total, chosen, items = att.fetch_sample(Client(), "o/r", 2, seed=0)
    total2, chosen2, _ = att.fetch_sample(Client(), "o/r", 2, seed=0)
    assert total == 5 and len(chosen) == 2 and chosen == chosen2
    assert {p for p, _ in items} == set(chosen)


def test_attribution_precision_coverage_and_bot_exclusion():
    db = {"s1": {"developer_id": "d1", "github_login": "alice", "is_bot": 0},
          "s2": {"developer_id": "d2", "github_login": "bob", "is_bot": 0},
          "s3": {"developer_id": "d3", "github_login": None, "is_bot": 0},
          "s4": {"developer_id": "d4", "github_login": "dependabot[bot]", "is_bot": 1},
          "s5": {"developer_id": "d5", "github_login": "carol", "is_bot": 0}}
    items = [(1, {"sha": "s1", "author": {"login": "Alice"}}),     # agree (case-insensitive)
             (1, {"sha": "s2", "author": {"login": "robert"}}),    # disagree
             (1, {"sha": "s3", "author": {"login": "dave"}}),      # GitHub only
             (1, {"sha": "s4", "author": {"login": "dependabot[bot]"}}),  # bot, excluded
             (1, {"sha": "s5", "author": None}),                   # RepoMind only
             (1, {"sha": "zz", "author": {"login": "x"}})]         # not in DB
    cases = att.build_cases("o/r", items, db)
    row = att.summarise("o/r", cases)
    assert row["human"] == 4 and row["github_attributed"] == 3 and row["repomind_linked"] == 3
    assert row["both_linked"] == 2 and row["agree"] == 1 and row["precision"] == 0.5
    assert row["coverage"] == pytest.approx(1 / 3) and row["repomind_linked_github_null"] == 1
    assert "e-mail" not in " ".join(att.CASE_FIELDS) and "name" not in att.CASE_FIELDS


# ---------- retrieval ----------

def test_minmax_fusion_scales_each_list_and_handles_missing():
    fused = dict(minmax_fusion([{"a": 10.0, "b": 0.0}, {"b": 0.9, "c": 0.1}]))
    assert fused["a"] == pytest.approx(0.5) and fused["b"] == pytest.approx(0.5) and fused["c"] == 0.0
    assert minmax_fusion([{"x": 3.0}, {}])[0] == ("x", 0.5)


def test_first_rank_and_metrics():
    assert rc.first_rank(["a", "b", "c"], {"c"}) == 3
    assert rc.first_rank(["a"], {"z"}) is None
    cases = [{f"rank_{m}": r for m in rc.METHODS} for r in (1, 5, None, 20)]
    m = rc.metrics(cases)
    assert m["bm25_r1"] == 0.25 and m["bm25_r5"] == 0.5 and m["bm25_r10"] == 0.5
    assert m["bm25_mrr"] == pytest.approx((1 + 0.2 + 0.05) / 4)
    assert m["d_dense_bm25_r10"] == 0.0


def test_rank_queries_with_toy_encoder(tmp_path):
    pytest.importorskip("numpy")
    from retrieval.dense import DenseIndex, EmbeddingCache
    from retrieval.text_search import Document

    vocab = ["crash", "spinner", "color", "windows", "docs"]

    def encoder(texts):  # bag-of-words unit vectors, a stand-in for the model
        import numpy as np
        vecs = np.array([[t.lower().count(w) for w in vocab] for t in texts], dtype="float32") + 1e-3
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

    docs = [Document("pr", "1", "pr:#1", "Fix crash on windows", ""),
            Document("pr", "2", "pr:#2", "Add spinner color option", ""),
            Document("pr", "3", "pr:#3", "Update docs", "")]
    cache = EmbeddingCache(tmp_path / "e.npz", encoder=encoder)
    dense = DenseIndex([d.key for d in docs], [d.title for d in docs], cache)
    pairs = [{"issue": 10, "title": "spinner color is wrong", "relevant": {"2"}},
             {"issue": 11, "title": "crash on windows", "relevant": {"1"}}]
    cases = rc.rank_queries(pairs, docs, dense)
    assert all(c["rank_dense"] == 1 and c["rank_rrf"] == 1 and c["rank_minmax"] == 1 for c in cases)
    # the cache persists and is reused without calling the encoder again
    reloaded = EmbeddingCache(tmp_path / "e.npz", encoder=lambda t: pytest.fail("re-encoded"))
    reloaded.embed([d.title for d in docs])


# ---------- batch runner wiring ----------

def test_run_all_includes_phase_d_stages_after_risk():
    names = [s for s, _ in run_all.STAGES]
    assert names.index("5_risk") < names.index("d_backtest")
    assert {"d_backtest", "d_retrieval", "d_attribution"} <= set(names)
    assert len(run_all.POOL_COMMANDS) == 3


def test_pooling_with_no_case_files_does_not_crash(tmp_path, monkeypatch):
    for mod, name in ((rc, "retrieval.csv"), (bt, "backtest.csv"), (att, "attribution.csv")):
        monkeypatch.setattr(mod, "CASES_DIR", tmp_path / f"{name}_cases")
        monkeypatch.setattr(mod, "OUT_CSV", tmp_path / name)
    assert rc.run_pool(10, 0)["status"].startswith("no cases")
    assert bt.run_pool(10, 0)["status"] == "no cases"
    assert att.run_pool()["human"] == 0
