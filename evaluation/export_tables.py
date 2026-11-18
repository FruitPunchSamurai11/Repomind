"""Phase D (round 2): export the paper's Tables I-IV and the co-change
result, one CSV row per repository, by calling the existing Module 4/5/6/10
code. No new methods: the only number computed here that Module 6 does not
already produce is the "(#N)" subject share, a plain count.

  dataset        -> results/round2/dataset.csv        Table I   (evaluation.metrics.dataset_stats)
  linkage        -> results/round2/linkage.csv        Table II  (dataset_stats + "(#N)" subject share)
  concentration  -> results/round2/concentration.csv  Table III (evaluation.experiments: metric
                    behaviour + recency / bot / identity perturbations)
  risk           -> results/round2/risk.csv           Table IV  (experiments.risk_stability: levels at
                    W=365, agreement with W=180 and W=730)
  cochange       -> results/round2/cochange.csv       Module 10 (assist.evaluate.cochange_eval, both
                    variants: all files / without changelog-CI boilerplate)

Usage:
  python -m evaluation.export_tables <table> --repo owner/name
"""
import argparse
import re
from datetime import datetime

from analysis.data import load_activity
from analysis.exclusions import describe as describe_exclusion
from assist.evaluate import cochange_eval
from evaluation import experiments as ex
from evaluation.metrics import dataset_stats
from evaluation.stats import RESULTS, ROOT, connect_ro, upsert_row

SQUASH_SUBJECT = re.compile(r"\(#\d+\)\s*$")

TABLES = {
    "dataset": ["repo", "commits", "first_commit", "last_commit", "span_years", "files",
                "committer_identities", "bot_identities", "bot_commit_share", "issues", "pull_requests",
                "reviews", "validation_ok", "validation_issue_count", "path_exclusion", "excluded_files"],
    "linkage": ["repo", "human_commits_linked_share", "top10_committers_linked", "pr_file_link_rate",
                "review_file_link_rate", "merge_commit_share", "commits_without_files_share",
                "non_merge_commits", "squash_subject_share"],
    "concentration": ["repo", "ranked_files", "median_neff_files", "files_top_share_ge80",
                      "rho_commits_vs_recency_files", "rho_commits_vs_multisignal_files",
                      "ranked_components", "median_neff_components", "components_top_share_ge80",
                      "rho_halflife_90_vs_365", "rho_halflife_365_vs_1095", "rho_halflife_90_vs_1095",
                      "rho_with_vs_without_bots", "files_top_committer_changes_with_bots", "files_compared_bots",
                      "rho_conservative_vs_name_merged", "identities_merged_away_by_name",
                      "path_exclusion", "excluded_files"],
    "risk": ["repo", "components", "assessable_components", "high", "medium", "low", "dormant",
             "insufficient_evidence", "agreement_with_180d", "agreement_with_730d", "high_at_180d", "high_at_730d",
             "path_exclusion", "excluded_files"],
    "cochange": ["repo", "status"] + [f"{k}_{v}" for v in ("all", "noboiler")
                                       for k in ("cases", "coverage", "hit5_cochange", "hit5_popularity",
                                                 "precision_cochange", "precision_popularity")] + ["k"],
}


def export_dataset(conn, repo: str) -> dict:
    d = dataset_stats(conn, repo, ROOT / "data" / "processed" / repo.split("/")[1])
    span = None
    if d["first_commit"] and d["last_commit"]:
        span = (datetime.fromisoformat(d["last_commit"]) - datetime.fromisoformat(d["first_commit"])).days / 365.25
    return {"repo": repo, **{k: d.get(k) for k in TABLES["dataset"] if k in d}, "span_years": span}


def squash_subject_share(conn, repo: str) -> tuple[int, float | None]:
    subjects = [r[0] or "" for r in conn.execute(
        "SELECT subject FROM commit_ WHERE repository_id=? AND is_merge=0", (repo,))]
    hits = sum(1 for s in subjects if SQUASH_SUBJECT.search(s))
    return len(subjects), (hits / len(subjects) if subjects else None)


def export_linkage(conn, repo: str) -> dict:
    d = dataset_stats(conn, repo, ROOT / "data" / "processed" / repo.split("/")[1])
    n, share = squash_subject_share(conn, repo)
    return {"repo": repo,
            "human_commits_linked_share": d["human_commit_share_by_identity_confidence"].get("high", 0.0),
            "top10_committers_linked": d["top10_committers_linked_to_github_login"],
            "pr_file_link_rate": d["pr_file_link_rate"], "review_file_link_rate": d["review_file_link_rate"],
            "merge_commit_share": d["merge_commit_share"],
            "commits_without_files_share": d["commits_without_files_share"],
            "non_merge_commits": n, "squash_subject_share": share}


def export_concentration(conn, repo: str) -> dict:
    activity = load_activity(conn, repo)
    files = ex.metric_behavior(activity, None)
    comps = ex.metric_behavior(activity, 2)
    rec = ex.recency_sensitivity(activity, None)
    bots = ex.bot_sensitivity(conn, repo, None)
    ident = ex.identity_sensitivity(conn, repo, None)
    return {"repo": repo,
            "ranked_files": files.get("ranked_units"), "median_neff_files": files.get("median_effective_contributors"),
            "files_top_share_ge80": files.get("share_top_commit_share_ge_80"),
            "rho_commits_vs_recency_files": files.get("rho_A_vs_D"),
            "rho_commits_vs_multisignal_files": files.get("rho_A_vs_E"),
            "ranked_components": comps.get("ranked_units"),
            "median_neff_components": comps.get("median_effective_contributors"),
            "components_top_share_ge80": comps.get("share_top_commit_share_ge_80"),
            "rho_halflife_90_vs_365": rec.get("rho_halflife_90_vs_365"),
            "rho_halflife_365_vs_1095": rec.get("rho_halflife_365_vs_1095"),
            "rho_halflife_90_vs_1095": rec.get("rho_halflife_90_vs_1095"),
            "rho_with_vs_without_bots": bots.get("rho_with_vs_without_bots"),
            "files_top_committer_changes_with_bots": bots.get("units_whose_top_committer_changes"),
            "files_compared_bots": bots.get("units_compared"),
            "rho_conservative_vs_name_merged": ident.get("rho_conservative_vs_name_merged"),
            "identities_merged_away_by_name": ident.get("identities_merged_away")}


def export_risk(conn, repo: str) -> dict:
    r = ex.risk_stability(load_activity(conn, repo), 2)
    counts = r["level_counts_365"]
    return {"repo": repo, "components": sum(counts.values()), "assessable_components": r["assessable_components"],
            **{lv: counts.get(lv, 0) for lv in ("high", "medium", "low", "dormant", "insufficient_evidence")},
            "agreement_with_180d": r.get("agreement_with_180d"), "agreement_with_730d": r.get("agreement_with_730d"),
            "high_at_180d": r.get("high_at_180d"), "high_at_730d": r.get("high_at_730d")}


def export_cochange(conn, repo: str) -> dict:
    row = {"repo": repo, "k": 5}
    statuses = []
    for variant, skip in (("all", False), ("noboiler", True)):
        e = cochange_eval(conn, repo, skip_boilerplate=skip)
        row[f"cases_{variant}"] = e["cases"]
        if not e["cases"]:
            statuses.append(f"{variant}: too little history ({e['commits']} commits)")
            continue
        row.update({f"coverage_{variant}": e["coverage"],
                    f"hit5_cochange_{variant}": e["hit@5_cochange"], f"hit5_popularity_{variant}": e["hit@5_popularity"],
                    f"precision_cochange_{variant}": e["precision_cochange"],
                    f"precision_popularity_{variant}": e["precision_popularity"]})
    row["status"] = "; ".join(statuses) or "ok"
    return row


EXPORTERS = {"dataset": export_dataset, "linkage": export_linkage, "concentration": export_concentration,
             "risk": export_risk, "cochange": export_cochange}


def run(table: str, repo: str) -> dict:
    conn = connect_ro()
    try:
        if not conn.execute("SELECT 1 FROM repository WHERE repository_id=?", (repo,)).fetchone():
            raise SystemExit(f"{repo} is not in the database (run stages 0a-2 first)")
        row = EXPORTERS[table](conn, repo)
        if "path_exclusion" in TABLES[table]:  # tables built on Module 4/5 activity record the rule
            row.update(describe_exclusion(conn, repo))
    finally:
        conn.close()
    upsert_row(RESULTS / f"{table}.csv", TABLES[table], row)
    return row


def main():
    p = argparse.ArgumentParser(description="Export Tables I-IV and co-change results (one row per repo)")
    p.add_argument("table", choices=list(EXPORTERS))
    p.add_argument("--repo", required=True)
    a = p.parse_args()
    row = run(a.table, a.repo)
    show = lambda v: f"{v:.3f}" if isinstance(v, float) else str(v)
    print(f"{a.table} {a.repo}: " + ", ".join(f"{k}={show(row.get(k))}" for k in TABLES[a.table][1:]))
    print(f"  -> {RESULTS / (a.table + '.csv')}")


if __name__ == "__main__":
    main()
