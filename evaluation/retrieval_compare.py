"""Phase D (round 2): BM25 vs dense vs hybrid retrieval.

Task (same as retrieval/evaluate.py): given an issue TITLE, find the pull
request that closed it. Ground truth = GitHub "closes #N" links (pr_issue).
Corpus = all PRs of the repo, text = title + body (as BM25 indexes it).
Repos with < 20 linked issues are excluded; at most 400 queries per repo
(random, seed 0, as in Module 7's evaluation).

Methods (each produces a ranking; rank of the first relevant PR is recorded,
cut at 100):
  bm25    retrieval.text_search.BM25Index, top 100
  dense   bge-small-en-v1.5 cosine (retrieval/dense.py), top 100
  rrf     Reciprocal Rank Fusion (k = 60) of the two top-100 lists
  minmax  min-max scaled score fusion of the two top-100 lists, equal weights

Reported: Recall@1/5/10, MRR (reciprocal rank, 0 beyond 100), chance
Recall@10 (10 x #relevant / #PRs, averaged), and paired-bootstrap 95%
intervals for the differences in R@10 and MRR (resampling queries; the
POOLED row resamples queries within each repo).

Outputs:
  results/round2/retrieval.csv                one row per repo, plus POOLED
  results/round2/retrieval_cases/<repo>.csv   one row per query (ranks only)

Usage:
  python -m evaluation.retrieval_compare --repo owner/name
  python -m evaluation.retrieval_compare --pool
"""
import argparse
import random
from collections import defaultdict

from evaluation.stats import (POOLED, RESULTS, bootstrap, bootstrap_stratified, connect_ro, read_rows,
                              slug, upsert_row, write_rows)
from retrieval.dense import CACHE_DIR, MODEL_NAME, DenseIndex, EmbeddingCache
from retrieval.evaluate import issue_pr_pairs
from retrieval.ranking import minmax_fusion, rrf
from retrieval.text_search import BM25Index, build_documents

OUT_CSV = RESULTS / "retrieval.csv"
CASES_DIR = RESULTS / "retrieval_cases"
METHODS = ("bm25", "dense", "rrf", "minmax")
DEPTH = 100
MIN_QUERIES = 20
COMPARISONS = [("dense", "bm25"), ("rrf", "bm25"), ("minmax", "bm25"), ("rrf", "dense"), ("minmax", "dense")]

CASE_FIELDS = ["repo", "issue", "n_relevant", "corpus_prs"] + [f"rank_{m}" for m in METHODS]
FIELDS = (["repo", "status", "queries", "corpus_prs", "chance_r10"]
          + [f"{m}_{k}" for m in METHODS for k in ("r1", "r5", "r10", "mrr")]
          + [f"d_{a}_{b}_{k}{s}" for a, b in COMPARISONS for k in ("r10", "mrr") for s in ("", "_lo", "_hi")]
          + ["model", "bootstrap", "seed"])


def first_rank(ranking: list[str], relevant: set[str]) -> int | None:
    for i, key in enumerate(ranking[:DEPTH], 1):
        if key in relevant:
            return i
    return None


def rank_queries(pairs: list[dict], docs, dense: DenseIndex) -> list[dict]:
    bm25 = BM25Index(docs)
    dense_hits = dense.search_many([p["title"] for p in pairs], limit=DEPTH)
    out = []
    for p, d_hits in zip(pairs, dense_hits):
        b_hits = [(doc.key, s) for doc, s in bm25.search(p["title"], limit=DEPTH)]
        rankings = {
            "bm25": [k for k, _ in b_hits],
            "dense": [k for k, _ in d_hits],
            "rrf": [k for k, _ in rrf([[k for k, _ in b_hits], [k for k, _ in d_hits]])],
            "minmax": [k for k, _ in minmax_fusion([dict(b_hits), dict(d_hits)])],
        }
        out.append({"issue": p["issue"], "n_relevant": len(p["relevant"]), "corpus_prs": len(docs),
                    **{f"rank_{m}": first_rank(r, p["relevant"]) for m, r in rankings.items()}})
    return out


def _rank(c: dict, m: str) -> int | None:
    v = c[f"rank_{m}"]
    return int(v) if v not in (None, "") else None


def metrics(cases: list[dict]) -> dict:
    n = len(cases)
    out = {}
    for m in METHODS:
        ranks = [_rank(c, m) for c in cases]
        for k in (1, 5, 10):
            out[f"{m}_r{k}"] = sum(1 for r in ranks if r is not None and r <= k) / n
        out[f"{m}_mrr"] = sum(1 / r for r in ranks if r) / n
    for a, b in COMPARISONS:
        out[f"d_{a}_{b}_r10"] = out[f"{a}_r10"] - out[f"{b}_r10"]
        out[f"d_{a}_{b}_mrr"] = out[f"{a}_mrr"] - out[f"{b}_mrr"]
    return out


def summarise(repo: str, cases: list[dict], n_boot: int, seed: int, stratified: bool = False) -> dict:
    row = {"repo": repo, "queries": len(cases), "model": MODEL_NAME, "bootstrap": n_boot, "seed": seed}
    row["chance_r10"] = sum(min(1.0, 10 * int(c["n_relevant"]) / int(c["corpus_prs"])) for c in cases) / len(cases)
    row.update(metrics(cases))
    diff_only = lambda s: {k: v for k, v in metrics(s).items() if k.startswith("d_")}
    if stratified:
        groups = defaultdict(list)
        for c in cases:
            groups[c["repo"]].append(c)
        ci = bootstrap_stratified(list(groups.values()), diff_only, n_boot, seed)
    else:
        ci = bootstrap(cases, diff_only, n_boot, seed)
    for k, (lo, hi) in ci.items():
        row[f"{k}_lo"], row[f"{k}_hi"] = lo, hi
    row["status"] = "ok"
    return row


def run_repo(repo: str, n_boot: int, seed: int, max_queries: int = 400, min_queries: int = MIN_QUERIES) -> dict:
    conn = connect_ro()
    try:
        pairs = issue_pr_pairs(conn, repo)
        docs = build_documents(conn, repo, "pr", include_body=True)
    finally:
        conn.close()
    if len(pairs) < min_queries:
        row = {"repo": repo, "status": f"excluded: {len(pairs)} issues linked to a closing PR (< {min_queries})",
               "queries": len(pairs), "corpus_prs": len(docs), "model": MODEL_NAME}
        upsert_row(OUT_CSV, FIELDS, row)
        return row
    if len(pairs) > max_queries:
        pairs = random.Random(0).sample(pairs, max_queries)
    cache = EmbeddingCache(CACHE_DIR / f"{slug(repo)}.npz")
    dense = DenseIndex([d.key for d in docs], [f"{d.title}\n{d.text}".strip() for d in docs], cache)
    cases = [{"repo": repo, **c} for c in rank_queries(pairs, docs, dense)]
    write_rows(CASES_DIR / f"{slug(repo)}.csv", CASE_FIELDS, cases)
    row = summarise(repo, cases, n_boot, seed)
    row["corpus_prs"] = len(docs)
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def run_pool(n_boot: int, seed: int) -> dict:
    cases = []
    for f in sorted(CASES_DIR.glob("*.csv")):
        cases += read_rows(f)
    if not cases:
        row = {"repo": POOLED, "status": "no cases (no repo passed the inclusion rule yet)", "queries": 0}
        upsert_row(OUT_CSV, FIELDS, row)
        return row
    row = summarise(POOLED, cases, n_boot, seed, stratified=True)
    upsert_row(OUT_CSV, FIELDS, row)
    return row


def main():
    p = argparse.ArgumentParser(description="BM25 vs dense vs hybrid retrieval (issue title -> closing PR)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo")
    g.add_argument("--pool", action="store_true")
    p.add_argument("--bootstrap", type=int, default=1000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--min-queries", type=int, default=MIN_QUERIES,
                   help="diagnostics only; the paper uses the default")
    p.add_argument("--results-dir", default=None, help="diagnostics only: write CSVs here instead")
    a = p.parse_args()
    global OUT_CSV, CASES_DIR
    if a.results_dir:
        from pathlib import Path
        OUT_CSV, CASES_DIR = Path(a.results_dir) / "retrieval.csv", Path(a.results_dir) / "retrieval_cases"
    row = run_pool(a.bootstrap, a.seed) if a.pool else run_repo(a.repo, a.bootstrap, a.seed, min_queries=a.min_queries)
    print(f"retrieval {row['repo']}: {row['status']}; {row['queries']} queries, corpus {row.get('corpus_prs', '-')} PRs")
    if row["status"] != "ok":
        return
    print(f"  {'method':7s} {'R@1':>6s} {'R@5':>6s} {'R@10':>6s} {'MRR':>6s}   (chance R@10 {row['chance_r10']:.3f})")
    for m in METHODS:
        print(f"  {m:7s} " + " ".join(f"{row[f'{m}_{k}']:6.3f}" for k in ("r1", "r5", "r10", "mrr")))
    for a_, b_ in COMPARISONS[:3]:
        k = f"d_{a_}_{b_}_r10"
        print(f"  R@10 {a_}-{b_}: {row[k]:+.3f} [{row[k + '_lo']:+.3f}, {row[k + '_hi']:+.3f}]")
    print(f"  -> {OUT_CSV}")


if __name__ == "__main__":
    main()
