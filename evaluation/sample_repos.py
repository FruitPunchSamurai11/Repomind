"""Phase B (Revision round 2): draw a documented random sample of GitHub
repositories to add to the original 8 (which were hand-picked).

Procedure (reproducible given the seed and GitHub's state on the run date;
every candidate examined is logged with its decision):

  1. Strata by primary language: Python, JavaScript/TypeScript, Go, Rust, Java,
     with a quota per stratum (default tops up the original 8 towards balance).
  2. Within a stratum, the creation months from 2008-01 up to (today - 4 years)
     are shuffled with a stratum-specific seeded RNG. Each month is one search:
         language:L stars:>=500 fork:false archived:false mirror:false
         is:public pushed:>=<today-12mo> created:<month>
     (for JS/TS the language of each window is drawn at random from the two).
     If more than 100 repos match, one page of the first 1,000 is drawn at random.
  3. Results are sorted by name, then shuffled with the RNG; up to
     --checks-per-window candidates are examined in that order and at most one
     is accepted per month (spreads the sample over project ages).
  4. A candidate is accepted only if it passes every filter:
       - not one of the original 8; short name does not collide with an
         already-chosen repo (data/raw/<name> is keyed by short name)
       - not a docs/list/tutorial/dataset repo (name/description/topic keywords)
       - not a mirror (GitHub's isMirror flag, or listed in HUMAN_EXCLUSIONS
         after human review); issues enabled; >= 50 pull requests
       - the stratum language is >= 50% of the repo's code bytes
       - 500-5,000 commits on the default branch
       - history >= 4 years (a commit exists before today - 4 years)
       - >= 50 closed issues linked to a PR (search qualifier linked:pr)
     Note: months are sampled uniformly, so repos from sparse months are
     somewhat over-represented relative to uniform sampling over repos.

Outputs:
  repos_round2.txt                   original 8 + chosen repos (input to run_all)
  results/round2/sample_log.csv      every window and every candidate examined
  results/round2/sample_chosen.csv   one row per chosen repo

Usage:
  python -m evaluation.sample_repos [--seed 2026] [--quota python=1,jsts=2,go=3,rust=3,java=3]
"""
import argparse
import csv
import math
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from evaluation.datasets import repo_names as original_repos
from ingest.github_client import GitHubGraphQLClient

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "results" / "round2"
REPOS_FILE = ROOT / "repos_round2.txt"

STRATA = {
    "python": ["Python"],
    "jsts": ["JavaScript", "TypeScript"],
    "go": ["Go"],
    "rust": ["Rust"],
    "java": ["Java"],
}
DEFAULT_QUOTA = "python=1,jsts=2,go=3,rust=3,java=3"

MIN_STARS = 500
MIN_COMMITS, MAX_COMMITS = 500, 5000
MIN_HISTORY_YEARS = 4
PUSHED_WITHIN_DAYS = 365
MIN_LINKED_ISSUES = 50
MIN_PRS = 50
MIN_LANG_SHARE = 0.5

# Exclusions decided on human review of an earlier draw, applied as filters so a
# re-run with the same seed reproduces the final sample.
HUMAN_EXCLUSIONS = {
    "flyway/flyway": "mirror: GitHub copy synced from the vendor's internal repository (human review, 2026-11-17)",
}

DOC_WORDS = re.compile(
    r"\b(awesome|curated|tutorials?|courses?|interview|cheat-?sheets?|roadmap|leetcode|"
    r"datasets?|books?|examples|homework|exercises|mirror|list of)\b", re.I)

CANDIDATE_QUERY = """
query($owner: String!, $name: String!, $since: GitTimestamp!) {
  rateLimit { remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner isMirror hasIssuesEnabled createdAt pushedAt stargazerCount
    pullRequests { totalCount }
    languages(first: 20) { totalSize edges { size node { name } } }
    defaultBranchRef { target { ... on Commit {
      all: history(first: 1) { totalCount }
      old: history(first: 1, until: $since) { totalCount }
    } } }
  }
}"""

LOG_FIELDS = ["stratum", "window", "query", "seed", "total_count", "repo", "stars",
              "commits", "decision", "reason"]
CHOSEN_FIELDS = ["repo", "stratum", "language", "stars", "commits", "created",
                 "closed_issues_linked_to_pr", "window"]


def months(first: date, last: date) -> list[str]:
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_range(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return f"{ym}-01..{(nxt - timedelta(days=1)).isoformat()}"


def parse_quota(text: str) -> dict[str, int]:
    quota = {}
    for part in text.split(","):
        k, v = part.split("=")
        if k.strip() not in STRATA:
            raise SystemExit(f"unknown stratum {k!r}; use {list(STRATA)}")
        quota[k.strip()] = int(v)
    return quota


def doc_reason(item: dict) -> str | None:
    text = " ".join([item["name"].replace("-", " ").replace("_", " "),
                     item.get("description") or "", " ".join(item.get("topics") or [])])
    m = DOC_WORDS.search(text)
    return f"docs/list/data repo (keyword '{m.group(0).lower()}')" if m else None


class Sampler:
    def __init__(self, client, seed: int, today: date, checks_per_window: int, max_windows: int):
        self.client = client
        self.seed = seed
        self.today = today
        self.checks_per_window = checks_per_window
        self.max_windows = max_windows
        self.originals = {r.lower() for r in original_repos()}
        self.taken_names = {r.split("/")[1].lower() for r in self.originals}
        self.log: list[dict] = []
        self.chosen: list[dict] = []
        self.history_cutoff = today.replace(year=today.year - MIN_HISTORY_YEARS)
        self.pushed_since = today - timedelta(days=PUSHED_WITHIN_DAYS)

    def _log(self, **row):
        row.setdefault("seed", self.seed)
        self.log.append({k: row.get(k, "") for k in LOG_FIELDS})

    def search(self, rng, language: str, window: str) -> tuple[str, int, list[dict]]:
        q = (f"language:{language} stars:>={MIN_STARS} fork:false archived:false mirror:false "
             f"is:public pushed:>={self.pushed_since.isoformat()} created:{month_range(window)}")
        params = {"q": q, "per_page": 100, "page": 1}
        data = self.client.get("/search/repositories", params).json()
        total = data.get("total_count", 0)
        items = data.get("items", [])
        pages = math.ceil(min(total, 1000) / 100)
        if pages > 1:
            page = rng.randint(1, pages)
            if page != 1:
                items = self.client.get("/search/repositories", {**params, "page": page}).json().get("items", [])
        return q, total, items

    def examine(self, item: dict, stratum: str) -> tuple[str | None, dict]:
        """Return (rejection reason or None, facts)."""
        full = item["full_name"]
        facts = {"stars": item.get("stargazers_count", "")}
        if full.lower() in self.originals:
            return "in original 8", facts
        if full.lower() in HUMAN_EXCLUSIONS:
            return f"excluded on human review: {HUMAN_EXCLUSIONS[full.lower()]}", facts
        if item["name"].lower() in self.taken_names:
            return "short name collides with an already-chosen repo", facts
        r = doc_reason(item)
        if r:
            return r, facts

        owner, name = full.split("/")
        since = datetime.combine(self.history_cutoff, datetime.min.time(), timezone.utc)
        repo = self.client.query(CANDIDATE_QUERY, {"owner": owner, "name": name,
                                                   "since": since.isoformat().replace("+00:00", "Z")})["repository"]
        if repo is None:
            return "not found via GraphQL", facts
        target = (repo.get("defaultBranchRef") or {}).get("target") or {}
        commits = (target.get("all") or {}).get("totalCount", 0)
        facts.update(commits=commits, created=repo["createdAt"][:10], stars=repo["stargazerCount"])
        langs = {e["node"]["name"]: e["size"] for e in repo["languages"]["edges"]}
        total_bytes = repo["languages"]["totalSize"] or 1
        share = sum(langs.get(l, 0) for l in STRATA[stratum]) / total_bytes
        facts["language"] = max(STRATA[stratum], key=lambda l: langs.get(l, 0))

        if repo["isMirror"]:
            return "mirror", facts
        if not repo["hasIssuesEnabled"]:
            return "issues disabled", facts
        if repo["pullRequests"]["totalCount"] < MIN_PRS:
            return f"only {repo['pullRequests']['totalCount']} PRs (< {MIN_PRS})", facts
        if share < MIN_LANG_SHARE:
            return f"{'/'.join(STRATA[stratum])} is {share:.0%} of code (< {MIN_LANG_SHARE:.0%}; mostly other/docs/data)", facts
        if not MIN_COMMITS <= commits <= MAX_COMMITS:
            return f"{commits} commits (outside {MIN_COMMITS}-{MAX_COMMITS})", facts
        if (target.get("old") or {}).get("totalCount", 0) == 0:
            return f"history < {MIN_HISTORY_YEARS} years (no commit before {self.history_cutoff})", facts

        linked = self.client.get("/search/issues", {
            "q": f"repo:{full} is:issue is:closed linked:pr", "per_page": 1}).json().get("total_count", 0)
        facts["linked"] = linked
        if linked < MIN_LINKED_ISSUES:
            return f"{linked} closed issues linked to PRs (< {MIN_LINKED_ISSUES})", facts
        return None, facts

    def run_stratum(self, stratum: str, quota: int):
        rng = random.Random(f"{self.seed}-{stratum}")
        windows = months(date(2008, 1, 1), self.history_cutoff)
        rng.shuffle(windows)
        got = 0
        for window in windows[: self.max_windows]:
            if got >= quota:
                break
            language = rng.choice(STRATA[stratum])
            q, total, items = self.search(rng, language, window)
            self._log(stratum=stratum, window=window, query=q, total_count=total,
                      decision="window", reason=f"{len(items)} candidates returned")
            items = sorted(items, key=lambda it: it["full_name"].lower())
            rng.shuffle(items)
            for item in items[: self.checks_per_window]:
                reason, facts = self.examine(item, stratum)
                self._log(stratum=stratum, window=window, repo=item["full_name"],
                          stars=facts.get("stars", ""), commits=facts.get("commits", ""),
                          decision="rejected" if reason else "chosen", reason=reason or "passes all filters")
                if reason is None:
                    self.taken_names.add(item["name"].lower())
                    self.chosen.append({
                        "repo": item["full_name"], "stratum": stratum, "language": facts["language"],
                        "stars": facts["stars"], "commits": facts["commits"], "created": facts["created"],
                        "closed_issues_linked_to_pr": facts["linked"], "window": window})
                    got += 1
                    print(f"  [{stratum}] chose {item['full_name']} ({got}/{quota})", flush=True)
                    break
        if got < quota:
            self._log(stratum=stratum, decision="shortfall",
                      reason=f"only {got}/{quota} found within {self.max_windows} windows")
            print(f"  [{stratum}] WARNING: only {got}/{quota} found", flush=True)


def write_csv(path: Path, fields: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_repos_file(path: Path, chosen: list[dict], seed: int, today: date):
    lines = [f"# RepoMind round-2 sample. Generated {today} by evaluation/sample_repos.py, seed {seed}.",
             "# Original 8 were hand-picked (first submission); the rest are a random stratified sample.",
             "# Lines starting with # are ignored by evaluation/run_all.py."]
    lines += [f"{r}  # original" for r in original_repos()]
    lines += [f"{c['repo']}  # random:{c['stratum']}" for c in chosen]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--quota", default=DEFAULT_QUOTA, help=f"per-stratum counts (default {DEFAULT_QUOTA})")
    p.add_argument("--checks-per-window", type=int, default=5)
    p.add_argument("--max-windows", type=int, default=40)
    p.add_argument("--today", default=None, help="YYYY-MM-DD; defaults to today (UTC)")
    p.add_argument("--out", default=str(REPOS_FILE))
    p.add_argument("--log-dir", default=str(OUT_DIR), help="where sample_log.csv / sample_chosen.csv go")
    args = p.parse_args()

    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    sampler = Sampler(GitHubGraphQLClient(), args.seed, today, args.checks_per_window, args.max_windows)
    t0 = time.time()
    for stratum, n in parse_quota(args.quota).items():
        sampler.run_stratum(stratum, n)

    log_dir = Path(args.log_dir)
    write_csv(log_dir / "sample_log.csv", LOG_FIELDS, sampler.log)
    write_csv(log_dir / "sample_chosen.csv", CHOSEN_FIELDS, sampler.chosen)
    write_repos_file(Path(args.out), sampler.chosen, args.seed, today)

    examined = sum(1 for r in sampler.log if r["decision"] in ("chosen", "rejected"))
    print(f"\nexamined {examined} candidates in {time.time() - t0:.0f}s; chose {len(sampler.chosen)}")
    print(f"{'repo':40s} {'language':11s} {'commits':>7s} {'stars':>7s}")
    for c in sampler.chosen:
        print(f"{c['repo']:40s} {c['language']:11s} {c['commits']:>7} {c['stars']:>7}")
    print(f"\nwrote {args.out}, {log_dir / 'sample_log.csv'}, {log_dir / 'sample_chosen.csv'}")


if __name__ == "__main__":
    main()
