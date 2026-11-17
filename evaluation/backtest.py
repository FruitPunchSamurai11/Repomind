"""Phase D (round 2): temporal backtest of the Module 5 risk rule.

Question: when the rule flagged a component in the past, did that
component then lose all of its active developers?

Procedure (per repository):
  * Cutoffs t = 31 Dec 23:59:59 UTC of every year with >= 2 years of
    history before t and >= 1 year of history after t (history = first to
    last non-bot commit in the database).
  * At each t, every depth-2 component is assessed with
    analysis.bus_factor.assess_component using ONLY commits <= t (bots
    excluded, same thresholds as risk_cli). "Recent"/"active" = committed to
    the component in (t - 365 d, t]. The reviewer-diversity signal is
    omitted (review data cannot be reliably cut at t), so a level is the
    count of the two remaining signals: 2 -> high, 1 -> medium, 0 -> low.
    Components that are dormant or have < 5 commits at t are not cases.
  * Outcome: "lost" = none of the developers active on the component at t
    commits to that component in (t, t + 365 d]. This includes components
    that receive no commits at all in that year.
  * Predictors of "lost" (higher = more risk): the rule's level
    (high 2 / medium 1 / low 0); "fewer active devs" (-#active devs);
    "less recent activity" (-#commits in the 365 d before t).
  * Statistics: loss rate per level, risk ratio high vs low, ROC AUC of
    each predictor and AUC differences, with 95% percentile intervals from
    a cluster bootstrap that resamples components (all cutoffs of one
    component move together; pooled: clusters are repo x component).

Two outcome definitions are reported side by side:
  A (columns without prefix)  all cases, as above;
  B (columns prefixed excl0_) leaves out component-years with zero commits
    in the following year, so "lost" means the component kept changing but
    by other people; it separates hand-over from the component going quiet.

Caveats: file renames move a path to a new component; the same component
appears at several cutoffs (hence the cluster bootstrap); levels are
indicators of recorded activity, not predictions.

Outputs:
  results/round2/backtest.csv                one row per repo, plus POOLED
  results/round2/backtest_cases/<repo>.csv   one row per (component, cutoff)

Usage:
  python -m evaluation.backtest --repo owner/name
  python -m evaluation.backtest --pool        # refresh every repo row + POOLED from the case files
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from analysis.bus_factor import assess_component
from analysis.data import parse_ts
from analysis.features import component_of
from evaluation.stats import (POOLED, RESULTS, auc, bootstrap, connect_ro, read_rows, slug,
                              upsert_row, write_rows)

OUT_CSV = RESULTS / "backtest.csv"
CASES_DIR = RESULTS / "backtest_cases"
DEPTH = 2
WINDOW = timedelta(days=365)
LEVEL_SCORE = {"high": 2, "medium": 1, "low": 0}
PREDICTORS = ("rule", "fewer_active", "less_recent")

CASE_FIELDS = ["repo", "component", "cutoff", "level", "active_devs", "recent_commits",
               "total_commits", "next_year_commits", "lost"]
VARIANT = "excl0_"  # outcome B: component-years with zero commits in the next year left out
STAT_KEYS = (["cases", "components", "base_loss_rate", "n_high", "n_medium", "n_low"]
             + [f"loss_{l}{s}" for l in ("high", "medium", "low") for s in ("", "_lo", "_hi")]
             + ["rr_high_low", "rr_high_low_lo", "rr_high_low_hi"]
             + [f"auc_{p}{s}" for p in PREDICTORS for s in ("", "_lo", "_hi")]
             + [f"diff_rule_minus_{p}{s}" for p in PREDICTORS[1:] for s in ("", "_lo", "_hi")])
REPO_KEYS = ["cutoffs", "first_cutoff", "last_cutoff", "skipped_dormant", "skipped_insufficient"]
FIELDS = (["repo", "status"] + REPO_KEYS + STAT_KEYS + [VARIANT + k for k in STAT_KEYS]
          + ["bootstrap", "seed"])


def load_commits(conn, repo: str) -> list[tuple[datetime, str, set[str]]]:
    rows = conn.execute(
        """SELECT c.commit_id, c.author_developer_id, c.timestamp, f.path
           FROM commit_file cf JOIN commit_ c ON c.commit_id = cf.commit_id
           JOIN file f ON f.file_id = cf.file_id
           JOIN developer d ON d.developer_id = c.author_developer_id
           WHERE c.repository_id = ? AND d.is_bot = 0""", (repo,)).fetchall()
    by_commit: dict[str, list] = {}
    for cid, dev, ts, path in rows:
        entry = by_commit.setdefault(cid, [parse_ts(ts), dev, set()])
        entry[2].add(component_of(path, DEPTH))
    return sorted((tuple(v) for v in by_commit.values()), key=lambda c: c[0])


def cutoffs(first: datetime, last: datetime) -> list[datetime]:
    out = []
    for year in range(first.year, last.year + 1):
        t = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        if t - first >= 2 * WINDOW and last - t >= WINDOW:
            out.append(t)
    return out


def cases_for_repo(repo: str, commits) -> tuple[list[dict], dict]:
    info = {"cutoffs": [], "skipped_dormant": 0, "skipped_insufficient": 0}
    if not commits:
        return [], info
    info["cutoffs"] = cuts = cutoffs(commits[0][0], commits[-1][0])
    cases = []
    for t in cuts:
        devs = defaultdict(lambda: defaultdict(lambda: {"commit_count": 0, "recent_commits": 0, "review_count": 0}))
        after = defaultdict(set)
        after_commits = defaultdict(int)
        for when, dev, comps in commits:
            if when <= t:
                for c in comps:
                    f = devs[c][dev]
                    f["commit_count"] += 1
                    if when > t - WINDOW:
                        f["recent_commits"] += 1
            elif when <= t + WINDOW:
                for c in comps:
                    after[c].add(dev)
                    after_commits[c] += 1
        for comp, d in devs.items():
            # reviewer signal omitted: min_reviews can never be reached
            r = assess_component(comp, d, min_reviews=10 ** 9)
            if r["level"] == "dormant":
                info["skipped_dormant"] += 1
                continue
            if r["level"] not in LEVEL_SCORE:
                info["skipped_insufficient"] += 1
                continue
            active = {dev for dev, f in d.items() if f["recent_commits"] > 0}
            cases.append({
                "repo": repo, "component": comp, "cutoff": t.date().isoformat(), "level": r["level"],
                "active_devs": len(active), "recent_commits": r["recent_commits"],
                "total_commits": r["total_commits"], "next_year_commits": after_commits[comp],
                "lost": int(not (active & after[comp])),
            })
    return cases, info


def _scores(cases: list[dict], predictor: str) -> list[float]:
    if predictor == "rule":
        return [LEVEL_SCORE[c["level"]] for c in cases]
    if predictor == "fewer_active":
        return [-int(c["active_devs"]) for c in cases]
    return [-int(c["recent_commits"]) for c in cases]


def point_stats(cases: list[dict]) -> dict:
    out = {}
    labels = [bool(int(c["lost"])) for c in cases]
    for level in LEVEL_SCORE:
        group = [bool(int(c["lost"])) for c in cases if c["level"] == level]
        out[f"loss_{level}"] = sum(group) / len(group) if group else None
    hi, lo = out["loss_high"], out["loss_low"]
    out["rr_high_low"] = hi / lo if hi is not None and lo else None
    for p in PREDICTORS:
        out[f"auc_{p}"] = auc(_scores(cases, p), labels)
    for p in PREDICTORS[1:]:
        a, b = out["auc_rule"], out[f"auc_{p}"]
        out[f"diff_rule_minus_{p}"] = a - b if a is not None and b is not None else None
    return out


def stats_block(cases: list[dict], n_boot: int, seed: int) -> dict:
    row = {"cases": len(cases)}
    if not cases:
        return row
    clusters = defaultdict(list)
    for c in cases:
        clusters[(c["repo"], c["component"])].append(c)
    row["components"] = len(clusters)
    row["base_loss_rate"] = sum(int(c["lost"]) for c in cases) / len(cases)
    for level in LEVEL_SCORE:
        row[f"n_{level}"] = sum(1 for c in cases if c["level"] == level)
    row.update(point_stats(cases))
    units = list(clusters.values())
    ci = bootstrap(units, lambda s: point_stats([c for u in s for c in u]), n_boot, seed)
    for k, (lo, hi) in ci.items():
        row[f"{k}_lo"], row[f"{k}_hi"] = lo, hi
    return row


def summarise(repo: str, cases: list[dict], n_boot: int, seed: int) -> dict:
    row = {"repo": repo, "cases": len(cases), "bootstrap": n_boot, "seed": seed}
    if not cases:
        return {**row, "status": "no cases"}
    row.update(stats_block(cases, n_boot, seed))
    active_next_year = [c for c in cases if int(c["next_year_commits"]) > 0]
    row.update({VARIANT + k: v for k, v in stats_block(active_next_year, n_boot, seed).items()})
    row["status"] = "ok"
    return row


def run_repo(repo: str, n_boot: int, seed: int) -> dict:
    conn = connect_ro()
    try:
        commits = load_commits(conn, repo)
    finally:
        conn.close()
    cases, info = cases_for_repo(repo, commits)
    write_rows(CASES_DIR / f"{slug(repo)}.csv", CASE_FIELDS, cases)
    row = summarise(repo, cases, n_boot, seed)
    cuts = info["cutoffs"]
    row.update(cutoffs=len(cuts), first_cutoff=cuts[0].date().isoformat() if cuts else "",
               last_cutoff=cuts[-1].date().isoformat() if cuts else "",
               skipped_dormant=info["skipped_dormant"], skipped_insufficient=info["skipped_insufficient"])
    if not commits:
        row["status"] = "no commits in database"
    elif not cuts:
        row["status"] = "history too short for a cutoff (needs >= 2 y before and >= 1 y after)"
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def refresh_repo_rows(n_boot: int, seed: int) -> int:
    """Recompute every repo's statistics from its case file (keeps the
    repo-level fields such as cutoffs from the existing row), so rows written
    by an older version of this script gain any newly added columns."""
    existing = {r["repo"]: r for r in read_rows(OUT_CSV)} if OUT_CSV.exists() else {}
    n = 0
    for f in sorted(CASES_DIR.glob("*.csv")):
        cases = read_rows(f)
        if not cases:
            continue
        repo = cases[0]["repo"]
        old = existing.get(repo, {})
        row = summarise(repo, cases, n_boot, seed)
        cuts = sorted({c["cutoff"] for c in cases})
        derived = {"cutoffs": len(cuts), "first_cutoff": cuts[0], "last_cutoff": cuts[-1]}
        for k in REPO_KEYS:
            row[k] = old.get(k) or derived.get(k, "")
        upsert_row(OUT_CSV, FIELDS, row)
        n += 1
    return n


def run_pool(n_boot: int, seed: int) -> dict:
    refresh_repo_rows(n_boot, seed)
    cases = []
    for f in sorted(CASES_DIR.glob("*.csv")):
        cases += read_rows(f)
    row = summarise(POOLED, cases, n_boot, seed)
    row["cutoffs"] = len({(c["repo"], c["cutoff"]) for c in cases})
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def main():
    p = argparse.ArgumentParser(description="Temporal backtest of the Module 5 risk rule")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo")
    g.add_argument("--pool", action="store_true", help="refresh every repo row and the POOLED row from the case files")
    p.add_argument("--bootstrap", type=int, default=1000)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    row = run_pool(a.bootstrap, a.seed) if a.pool else run_repo(a.repo, a.bootstrap, a.seed)
    f = lambda k: row.get(k) if row.get(k) is not None else "-"
    r = lambda k: f"{row[k]:.2f}" if isinstance(row.get(k), float) else "-"
    ci = lambda k: f"{r(k)} [{r(k + '_lo')}, {r(k + '_hi')}]"
    print(f"backtest {row['repo']}: {row['status']}; {f('cutoffs')} cutoffs, {f('cases')} cases "
          f"from {f('components')} components")
    if row["status"] == "ok":
        print(f"  n high/medium/low: {row['n_high']}/{row['n_medium']}/{row['n_low']}; base loss rate {r('base_loss_rate')}")
        print(f"  loss rate high {ci('loss_high')}  medium {ci('loss_medium')}  low {ci('loss_low')}")
        print(f"  risk ratio high/low {ci('rr_high_low')}")
        print(f"  AUC rule {ci('auc_rule')}  fewer-active {ci('auc_fewer_active')}  less-recent {ci('auc_less_recent')}")
        v = VARIANT
        print(f"  outcome B (excl. zero-commit next years): {f(v + 'cases')} cases; loss high {ci(v + 'loss_high')} "
              f"low {ci(v + 'loss_low')}; AUC rule {ci(v + 'auc_rule')}")
    print(f"  -> {OUT_CSV}")


if __name__ == "__main__":
    main()
