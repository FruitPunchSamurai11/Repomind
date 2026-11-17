"""Phase D (round 2): how much do the concentration rankings and risk levels
depend on identity resolution? Merge identities by GitHub's attributed
account and compare.

Reference: the attribution stage's per-commit files
(results/round2/attribution_cases/<repo>.csv). For every sampled human
commit that GitHub attributes to an account, RepoMind's developer id is
mapped to that account (the most frequent one if GitHub attributes the
same id's commits to several accounts; such ids are counted as conflicts).
Every developer whose own linked login equals a reference account joins
the same merged identity. All other identities stay as RepoMind resolved
them (conservative: unresolved identities are never guessed into a merge).

Compared with the unmerged data, using the existing Module 4/5 code:
  * Spearman rho of file-level (and depth-2 component) HHI rankings on
    commits (>= 5 commits), as in Module 6's sensitivity experiments;
  * how many depth-2 components change risk level at W = 365 d.

LOWER BOUND: only the 1-2 sampled pages of commits reveal GitHub's
attribution, so most identities are left unmerged. Full attribution could
only merge more, so the changes reported here are a lower bound on the
sensitivity to identity resolution, not an estimate of it.

Output: results/round2/identity_merge.csv (one row per repo)
Usage:  python -m evaluation.identity_reference_merge --repo owner/name
"""
import argparse
from collections import Counter, defaultdict

from analysis.bus_factor import assess_component
from analysis.data import load_activity
from analysis.features import build_features
from evaluation.experiments import _hhi, _rho
from evaluation.stats import RESULTS, connect_ro, read_rows, slug, upsert_row

OUT_CSV = RESULTS / "identity_merge.csv"
ATTRIBUTION_CASES = RESULTS / "attribution_cases"
BOUND = "lower bound: only sampled commits are merged"
FIELDS = ["repo", "status", "attributed_human_commits", "developer_ids_mapped", "reference_accounts",
          "developer_ids_merged_away", "conflicting_developer_ids", "rho_files_hhi", "files_compared",
          "rho_components_hhi", "components_compared", "risk_components", "risk_levels_changed",
          "risk_level_changes", "bound"]


def reference_mapping(cases: list[dict], dev_logins: dict[str, str | None]) -> tuple[dict, int]:
    """developer_id -> merged identity key, and the number of conflicting ids."""
    seen = defaultdict(Counter)
    for c in cases:
        if int(c["in_database"]) and not int(c["is_bot"]) and c["github_login"] and c["repomind_developer_id"]:
            seen[c["repomind_developer_id"]][c["github_login"].lower()] += 1
    mapping = {dev: f"gh:{logins.most_common(1)[0][0]}" for dev, logins in seen.items()}
    conflicts = sum(1 for logins in seen.values() if len(logins) > 1)
    accounts = set(mapping.values())
    for dev, login in dev_logins.items():  # identities already linked to a reference account join it
        if login and f"gh:{login.lower()}" in accounts:
            mapping.setdefault(dev, f"gh:{login.lower()}")
    return mapping, conflicts


def remap_activity(activity: dict, mapping: dict) -> dict:
    def remap(rows, idx):
        return [tuple(mapping.get(v, v) if i == idx else v for i, v in enumerate(row)) for row in rows]
    return {"commits": remap(activity["commits"], 1), "pr_authored": remap(activity["pr_authored"], 0),
            "reviewed": remap(activity["reviewed"], 0), "issues_resolved": remap(activity["issues_resolved"], 0)}


def compare(activity: dict, merged: dict) -> dict:
    out = {}
    for depth, name in ((None, "files"), (2, "components")):
        base, _ = build_features(activity, depth)
        alt, _ = build_features(merged, depth)
        a, b = _hhi(base, "commit_count", 5), _hhi(alt, "commit_count", 5)
        out[f"rho_{name}_hhi"] = _rho(a, b)
        out[f"{name}_compared"] = len(set(a) & set(b))
        if depth == 2:
            lv_base = {c: assess_component(c, d)["level"] for c, d in base.items()}
            lv_alt = {c: assess_component(c, d)["level"] for c, d in alt.items()}
            changes = Counter(f"{lv_base[c]}->{lv_alt[c]}" for c in lv_base if lv_alt.get(c) != lv_base[c])
            out["risk_components"] = len(lv_base)
            out["risk_levels_changed"] = sum(changes.values())
            out["risk_level_changes"] = "; ".join(f"{k}:{v}" for k, v in sorted(changes.items()))
    return out


def run(repo: str) -> dict:
    row = {"repo": repo, "bound": BOUND}
    case_file = ATTRIBUTION_CASES / f"{slug(repo)}.csv"
    if not case_file.exists():
        row["status"] = "no attribution cases (run the d_attribution stage first)"
        upsert_row(OUT_CSV, FIELDS, row)
        return row
    cases = read_rows(case_file)
    conn = connect_ro()
    try:
        dev_logins = {r["developer_id"]: r["github_login"] for r in conn.execute(
            """SELECT DISTINCT d.developer_id, d.github_login FROM developer d
               JOIN commit_ c ON c.author_developer_id = d.developer_id WHERE c.repository_id = ?""", (repo,))}
        activity = load_activity(conn, repo)
    finally:
        conn.close()
    mapping, conflicts = reference_mapping(cases, dev_logins)
    merged_ids = {mapping.get(d, d) for d in dev_logins}
    row.update(attributed_human_commits=sum(1 for c in cases if int(c["in_database"]) and not int(c["is_bot"])
                                            and c["github_login"]),
               developer_ids_mapped=len(mapping), reference_accounts=len(set(mapping.values())),
               developer_ids_merged_away=len(dev_logins) - len(merged_ids), conflicting_developer_ids=conflicts)
    row.update(compare(activity, remap_activity(activity, mapping)))
    row["status"] = "ok"
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def main():
    p = argparse.ArgumentParser(description="Identity merge by GitHub's attributed account (lower bound)")
    p.add_argument("--repo", required=True)
    row = run(p.parse_args().repo)
    show = lambda v: f"{v:.3f}" if isinstance(v, float) else ("-" if v is None else str(v))
    print(f"identity merge {row['repo']}: {row['status']} ({BOUND})")
    if row["status"] == "ok":
        print(f"  {row['developer_ids_mapped']} ids mapped to {row['reference_accounts']} accounts; "
              f"{row['developer_ids_merged_away']} merged away; {row['conflicting_developer_ids']} conflicting")
        print(f"  rho files {show(row['rho_files_hhi'])} ({row['files_compared']} files), "
              f"components {show(row['rho_components_hhi'])} ({row['components_compared']})")
        print(f"  risk levels changed: {row['risk_levels_changed']}/{row['risk_components']} "
              f"{row['risk_level_changes'] or ''}")
    print(f"  -> {OUT_CSV}")


if __name__ == "__main__":
    main()
