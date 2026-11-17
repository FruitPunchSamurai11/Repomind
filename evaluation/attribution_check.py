"""Phase D (round 2): check RepoMind's commit -> GitHub-account links against
GitHub's own commit attribution.

GitHub's REST commits API returns, for each commit, the account GitHub
attributes it to (`author.login`, matched by GitHub from verified e-mails)
or null. RepoMind links a commit author to a login only from evidence in
the data (noreply e-mail, PR authorship); it never guesses.

Sampling: commits on the default branch, 100 per page; `--pages` pages
(default 2) are drawn at random with a seed fixed per repo, so every repo
costs at most 1 + pages REST calls. Repos with <= `--pages` pages are read
in full.

Per human commit (bots by RepoMind's flag or a GitHub "[bot]" login are
excluded) we record GitHub's login and RepoMind's login. Reported:
  github_attributed_share  share of sampled human commits GitHub attributes to an account
  repomind_linked_share    share RepoMind links to a login
  precision                of commits where both give a login, share where they agree
  coverage                 of GitHub-attributed commits, share RepoMind links to the same login
with 95% Wilson intervals. GitHub's attribution is a reference, not ground
truth (it misses commits made with unverified e-mails).

Outputs:
  results/round2/attribution.csv               one row per repo, plus POOLED
  results/round2/attribution_cases/<repo>.csv  sha + logins only (no names or e-mails)

Usage:
  python -m evaluation.attribution_check --repo owner/name [--pages 2] [--seed 0]
  python -m evaluation.attribution_check --pool
"""
import argparse
import random
from urllib.parse import parse_qs, urlparse

from evaluation.stats import POOLED, RESULTS, connect_ro, read_rows, slug, upsert_row, wilson, write_rows
from normalization.identity import is_bot_name

OUT_CSV = RESULTS / "attribution.csv"
CASES_DIR = RESULTS / "attribution_cases"
CASE_FIELDS = ["repo", "page", "sha", "in_database", "is_bot", "github_login", "repomind_login",
               "repomind_developer_id"]
FIELDS = ["repo", "status", "total_pages", "pages_sampled", "sampled", "in_database", "human",
          "github_attributed", "github_attributed_share", "github_attributed_lo", "github_attributed_hi",
          "repomind_linked", "repomind_linked_share", "both_linked", "agree",
          "precision", "precision_lo", "precision_hi", "coverage", "coverage_lo", "coverage_hi",
          "repomind_linked_github_null", "seed"]


def last_page(resp) -> int:
    last = resp.links.get("last", {}).get("url")
    return int(parse_qs(urlparse(last).query)["page"][0]) if last else 1


def fetch_sample(client, repo: str, pages: int, seed: int) -> tuple[int, list[int], list[tuple[int, dict]]]:
    first = client.get(f"/repos/{repo}/commits", {"per_page": 100, "page": 1})
    total = last_page(first)
    rng = random.Random(f"{seed}-{repo}")
    chosen = sorted(rng.sample(range(1, total + 1), min(pages, total)))
    items = []
    for p in chosen:
        data = first.json() if p == 1 else client.get(f"/repos/{repo}/commits", {"per_page": 100, "page": p}).json()
        items += [(p, it) for it in data]
    return total, chosen, items


def build_cases(repo: str, items, db_rows: dict) -> list[dict]:
    cases = []
    for page, it in items:
        gh_login = (it.get("author") or {}).get("login")
        db = db_rows.get(it["sha"])
        bot = bool((db and db["is_bot"]) or (gh_login and (gh_login.endswith("[bot]") or is_bot_name(gh_login))))
        cases.append({"repo": repo, "page": page, "sha": it["sha"], "in_database": int(db is not None),
                      "is_bot": int(bot), "github_login": gh_login or "",
                      "repomind_login": (db["github_login"] or "") if db else "",
                      "repomind_developer_id": db["developer_id"] if db else ""})
    return cases


def summarise(repo: str, cases: list[dict]) -> dict:
    human = [c for c in cases if int(c["in_database"]) and not int(c["is_bot"])]
    gh = [c for c in human if c["github_login"]]
    rm = [c for c in human if c["repomind_login"]]
    both = [c for c in human if c["github_login"] and c["repomind_login"]]
    agree = [c for c in both if c["github_login"].lower() == c["repomind_login"].lower()]
    covered = [c for c in gh if c["repomind_login"].lower() == c["github_login"].lower()]
    n = len(human)
    row = {"repo": repo, "sampled": len(cases), "in_database": sum(int(c["in_database"]) for c in cases),
           "human": n, "github_attributed": len(gh), "repomind_linked": len(rm), "both_linked": len(both),
           "agree": len(agree), "repomind_linked_github_null": len(rm) - len(both),
           "github_attributed_share": len(gh) / n if n else None,
           "repomind_linked_share": len(rm) / n if n else None,
           "precision": len(agree) / len(both) if both else None,
           "coverage": len(covered) / len(gh) if gh else None}
    row["github_attributed_lo"], row["github_attributed_hi"] = wilson(len(gh), n)
    row["precision_lo"], row["precision_hi"] = wilson(len(agree), len(both))
    row["coverage_lo"], row["coverage_hi"] = wilson(len(covered), len(gh))
    row["status"] = "ok" if n else "no human commits matched the database"
    return row


def run_repo(repo: str, pages: int, seed: int) -> dict:
    from ingest.github_client import GitHubGraphQLClient  # tokens are loaded only there
    total, chosen, items = fetch_sample(GitHubGraphQLClient(), repo, pages, seed)
    conn = connect_ro()
    try:
        rows = conn.execute(
            """SELECT c.commit_id, d.developer_id, d.github_login, d.is_bot FROM commit_ c
               JOIN developer d ON d.developer_id = c.author_developer_id
               WHERE c.repository_id = ?""", (repo,)).fetchall()
    finally:
        conn.close()
    cases = build_cases(repo, items, {r["commit_id"]: r for r in rows})
    write_rows(CASES_DIR / f"{slug(repo)}.csv", CASE_FIELDS, cases)
    row = summarise(repo, cases)
    row.update(total_pages=total, pages_sampled=" ".join(map(str, chosen)), seed=seed)
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def run_pool() -> dict:
    cases = []
    for f in sorted(CASES_DIR.glob("*.csv")):
        cases += read_rows(f)
    row = summarise(POOLED, cases)
    row["pages_sampled"] = len({(c["repo"], c["page"]) for c in cases})
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def main():
    p = argparse.ArgumentParser(description="RepoMind identity links vs GitHub commit attribution")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo")
    g.add_argument("--pool", action="store_true")
    p.add_argument("--pages", type=int, default=2)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    row = run_pool() if a.pool else run_repo(a.repo, a.pages, a.seed)
    pct = lambda k: f"{row[k]:.1%}" if row.get(k) is not None else "-"
    print(f"attribution {row['repo']}: {row['status']}; pages {row.get('pages_sampled')} of {row.get('total_pages', '-')}, "
          f"{row['sampled']} sampled, {row['in_database']} in DB, {row['human']} human")
    print(f"  GitHub attributes {row['github_attributed']}/{row['human']} = {pct('github_attributed_share')} "
          f"[{pct('github_attributed_lo')}, {pct('github_attributed_hi')}]")
    print(f"  RepoMind links {row['repomind_linked']}/{row['human']} = {pct('repomind_linked_share')}; "
          f"linked but GitHub null: {row['repomind_linked_github_null']}")
    print(f"  precision {row['agree']}/{row['both_linked']} = {pct('precision')} [{pct('precision_lo')}, {pct('precision_hi')}]; "
          f"coverage {pct('coverage')}")
    print(f"  -> {OUT_CSV}")


if __name__ == "__main__":
    main()
