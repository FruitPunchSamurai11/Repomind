"""Phase E (round 2): export a blank identity-labelling sheet for humans.

Draws ~150 human commits (fixed seed, split evenly across the repos) from
the attribution stage's per-commit files, so every row carries GitHub's
attributed account (blank if GitHub attributes none) next to RepoMind's
resolution. Author name and e-mail come from the raw Git data
(data/raw/<name>/commits.jsonl, read only).

The label columns are left BLANK on purpose: two people label
independently; nothing here fills, guesses or pre-suggests a label.

Default repos: pallets/flask, psf/black and one of the randomly sampled
repos in repos_round2.txt, chosen with the same seed.

Output: results/round2/identity_labelling/identity_sample.csv (gitignored:
it contains names and e-mail addresses). An existing file is never
overwritten without --force, so labels already entered cannot be lost.

Usage:
  python -m evaluation.export_identity_sample [--repos a/b,c/d,e/f] [--n 150] [--seed 0]
"""
import argparse
import json
import random

from evaluation.stats import RESULTS, ROOT, slug, read_rows, write_rows

OUT = RESULTS / "identity_labelling" / "identity_sample.csv"
ATTRIBUTION_CASES = RESULTS / "attribution_cases"
FIXED_REPOS = ["pallets/flask", "psf/black"]
FIELDS = ["repo", "sha", "author_name", "author_email", "repomind_developer_id", "repomind_login",
          "github_attributed_login", "label_same_person", "notes"]


def default_repos(seed: int, repos_file=ROOT / "repos_round2.txt") -> list[str]:
    new = sorted(line.split("#")[0].strip() for line in repos_file.read_text(encoding="utf-8").splitlines()
                 if "# random:" in line)
    return FIXED_REPOS + [random.Random(seed).choice(new)]


def raw_authors(repo: str, shas: set[str]) -> dict[str, dict]:
    path = ROOT / "data" / "raw" / repo.split("/")[1] / "commits.jsonl"
    found = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["sha"] in shas:
                found[d["sha"]] = d.get("author") or {}
                if len(found) == len(shas):
                    break
    return found


def sample_rows(repo: str, k: int, seed: int) -> list[dict]:
    case_file = ATTRIBUTION_CASES / f"{slug(repo)}.csv"
    if not case_file.exists():
        raise SystemExit(f"{repo}: no attribution cases yet ({case_file}); run its d_attribution stage first")
    human = [c for c in read_rows(case_file) if int(c["in_database"]) and not int(c["is_bot"])]
    human.sort(key=lambda c: c["sha"])
    picked = random.Random(f"{seed}-{repo}").sample(human, min(k, len(human)))
    authors = raw_authors(repo, {c["sha"] for c in picked})
    return [{"repo": repo, "sha": c["sha"],
             "author_name": authors.get(c["sha"], {}).get("name", ""),
             "author_email": authors.get(c["sha"], {}).get("email", ""),
             "repomind_developer_id": c["repomind_developer_id"], "repomind_login": c["repomind_login"],
             "github_attributed_login": c["github_login"], "label_same_person": "", "notes": ""}
            for c in sorted(picked, key=lambda c: c["sha"])]


def main():
    p = argparse.ArgumentParser(description="Export a blank identity-labelling CSV (labels are filled by humans)")
    p.add_argument("--repos", default=None, help="comma-separated; default flask, black + one random new repo")
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--force", action="store_true", help="overwrite an existing sheet (destroys any labels in it)")
    a = p.parse_args()
    from pathlib import Path
    out = Path(a.out)
    if out.exists() and not a.force:
        raise SystemExit(f"{out} already exists (it may hold labels); pass --force to overwrite")
    repos = a.repos.split(",") if a.repos else default_repos(a.seed)
    per_repo = a.n // len(repos)
    rows = []
    for i, repo in enumerate(repos):
        rows += sample_rows(repo, per_repo + (1 if i < a.n % len(repos) else 0), a.seed)
    write_rows(out, FIELDS, rows)
    print(f"identity sample: {len(rows)} commits from {', '.join(repos)} (seed {a.seed}); labels left blank")
    print(f"  with GitHub attribution: {sum(1 for r in rows if r['github_attributed_login'])}, "
          f"RepoMind-linked: {sum(1 for r in rows if r['repomind_login'])}")
    print(f"  -> {out}")


if __name__ == "__main__":
    main()
