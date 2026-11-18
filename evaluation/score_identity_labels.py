"""Phase E (round 2): score manual identity labels.

Reads a labelled sheet from results/round2/identity_labelling/ (gitignored: it
holds names and e-mails) and writes only aggregate counts to
results/round2/identity_labels.csv, one row per repo plus POOLED.

Each row of the sheet is one sampled human commit with RepoMind's linked login
(blank if RepoMind links none) and GitHub's attributed login (blank if none).
`label_same_person` is the labeller's verdict that the commit's author is the
account it was matched to: yes / no / unsure.

  RepoMind precision = links labelled yes / links labelled yes or no
  GitHub precision   = attributions labelled yes / attributions labelled yes or no
  RepoMind coverage  = RepoMind-linked among commits GitHub attributes and the labeller confirmed

When a RepoMind login and a GitHub login differ, the label cannot judge both;
such rows are counted (`login_disagreements`) and left out of both precisions.
"unsure" rows are left out of precision and reported separately. With one
labeller no inter-rater agreement can be computed.

Usage:
  python -m evaluation.score_identity_labels --labels results/round2/identity_labelling/labels_karthikeya.csv \
      --labeller karthikeya --note "AI pre-filled, verified by one author"
"""
import argparse
import csv
from pathlib import Path

from evaluation.stats import RESULTS, fmt, wilson, write_rows

OUT = RESULTS / "identity_labels.csv"
POOLED = "POOLED"
FIELDS = ["repo", "labeller", "labelled", "yes", "no", "unsure", "blank", "login_disagreements",
          "repomind_linked", "repomind_correct", "repomind_precision", "repomind_precision_lo", "repomind_precision_hi",
          "github_attributed", "github_correct", "github_precision", "github_precision_lo", "github_precision_hi",
          "confirmed_attributed", "repomind_coverage", "note"]
VALID = {"yes", "no", "unsure", ""}


def read_labels(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_label"] = r["label_same_person"].strip().lower()
        if r["_label"] not in VALID:
            raise ValueError(f"unknown label {r['label_same_person']!r} (sha {r['sha'][:10]})")
    return rows


def score(repo: str, rows: list[dict], labeller: str, note: str = "") -> dict:
    lab = lambda r: r["_label"]
    rm = lambda r: r["repomind_login"].strip().lower()
    gh = lambda r: r["github_attributed_login"].strip().lower()
    disagree = [r for r in rows if rm(r) and gh(r) and rm(r) != gh(r)]
    usable = [r for r in rows if r not in disagree]
    rm_judged = [r for r in usable if rm(r) and lab(r) in ("yes", "no")]
    gh_judged = [r for r in usable if gh(r) and lab(r) in ("yes", "no")]
    rm_ok = sum(lab(r) == "yes" for r in rm_judged)
    gh_ok = sum(lab(r) == "yes" for r in gh_judged)
    confirmed = [r for r in usable if gh(r) and lab(r) == "yes"]
    rp, gp = wilson(rm_ok, len(rm_judged)), wilson(gh_ok, len(gh_judged))
    ratio = lambda k, n: fmt(k / n) if n else ""
    return {
        "repo": repo, "labeller": labeller, "labelled": sum(lab(r) != "" for r in rows),
        "yes": sum(lab(r) == "yes" for r in rows), "no": sum(lab(r) == "no" for r in rows),
        "unsure": sum(lab(r) == "unsure" for r in rows), "blank": sum(lab(r) == "" for r in rows),
        "login_disagreements": len(disagree),
        "repomind_linked": sum(bool(rm(r)) for r in rows), "repomind_correct": rm_ok,
        "repomind_precision": ratio(rm_ok, len(rm_judged)), "repomind_precision_lo": fmt(rp[0]),
        "repomind_precision_hi": fmt(rp[1]),
        "github_attributed": sum(bool(gh(r)) for r in rows), "github_correct": gh_ok,
        "github_precision": ratio(gh_ok, len(gh_judged)), "github_precision_lo": fmt(gp[0]),
        "github_precision_hi": fmt(gp[1]),
        "confirmed_attributed": len(confirmed),
        "repomind_coverage": ratio(sum(bool(rm(r)) for r in confirmed), len(confirmed)),
        "note": note,
    }


def main():
    p = argparse.ArgumentParser(description="Score manual identity labels (aggregate output only)")
    p.add_argument("--labels", required=True)
    p.add_argument("--labeller", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--out", default=str(OUT))
    a = p.parse_args()
    rows = read_labels(Path(a.labels))
    out = [score(repo, [r for r in rows if r["repo"] == repo], a.labeller, a.note)
           for repo in sorted({r["repo"] for r in rows})]
    out.append(score(POOLED, rows, a.labeller, a.note))
    write_rows(Path(a.out), FIELDS, out)
    for r in out:
        print(f"{r['repo']:16s} labelled {r['labelled']} (yes {r['yes']}, no {r['no']}, unsure {r['unsure']}); "
              f"RepoMind {r['repomind_correct']}/{r['repomind_linked']} precision {r['repomind_precision']}; "
              f"GitHub {r['github_correct']}/{r['github_attributed']} precision {r['github_precision']}")
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
