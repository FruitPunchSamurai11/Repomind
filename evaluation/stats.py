"""Small shared helpers for the round-2 experiments (pure Python, no numpy):
AUC, percentile bootstrap, read-only DB access, per-repo CSV upserts."""
import csv
import random
import sqlite3
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results" / "round2"
DB_PATH = ROOT / "data" / "repomind.sqlite"
POOLED = "POOLED"


def connect_ro(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Read-only connection: experiments never write to the shared database."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def auc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    """ROC AUC = P(score of a positive > score of a negative), ties count 1/2
    (Mann-Whitney U with average ranks). None if only one class is present."""
    n1 = sum(1 for y in labels if y)
    n0 = len(labels) - n1
    if n1 == 0 or n0 == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, labels) if y)
    return (rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    pos = q * (len(v) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def bootstrap(units: list, stat: Callable[[list], dict], n_boot: int = 1000, seed: int = 0) -> dict:
    """Percentile bootstrap. `units` are resampled with replacement (clusters
    for a cluster bootstrap, queries for a paired bootstrap); `stat` maps a
    resampled list of units to {name: value or None}. Returns
    {name: (lo, hi)} 95% intervals, ignoring resamples where a value is None."""
    rng = random.Random(seed)
    draws: dict[str, list[float]] = {}
    for _ in range(n_boot):
        sample = [units[rng.randrange(len(units))] for _ in range(len(units))]
        for k, v in stat(sample).items():
            if v is not None:
                draws.setdefault(k, []).append(v)
    return {k: (percentile(v, 0.025), percentile(v, 0.975)) for k, v in draws.items()}


def bootstrap_stratified(groups: list[list], stat: Callable[[list], dict], n_boot: int = 1000,
                         seed: int = 0) -> dict:
    """Like `bootstrap`, but resamples within each group (e.g. queries within
    each repo) so every repo keeps its size in every resample."""
    rng = random.Random(seed)
    draws: dict[str, list[float]] = {}
    for _ in range(n_boot):
        sample = [g[rng.randrange(len(g))] for g in groups for _ in range(len(g))]
        for k, v in stat(sample).items():
            if v is not None:
                draws.setdefault(k, []).append(v)
    return {k: (percentile(v, 0.025), percentile(v, 0.975)) for k, v in draws.items()}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score interval for a proportion k/n."""
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.4f}"
    return str(x)


def upsert_row(path: Path, fields: list[str], row: dict, key: str = "repo"):
    """Write `row` into a one-row-per-repo CSV, replacing any previous row for the same repo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get(key) != row[key]]
    rows.append({k: fmt(row.get(k)) for k in fields})
    rows.sort(key=lambda r: (r[key] == POOLED, r[key].lower()))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_rows(path: Path, fields: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: fmt(r.get(k)) for k in fields} for r in rows)


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
