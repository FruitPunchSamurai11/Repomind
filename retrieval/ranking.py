"""Rank fusion. Reciprocal Rank Fusion combines ranked lists whose raw
scores are not comparable (BM25 scores from different corpora, recency
order, structured matches) using only each item's rank."""


def rrf(rank_lists: list[list[str]], weights: list[float] | None = None, k: int = 60) -> list[tuple[str, float]]:
    weights = weights or [1.0] * len(rank_lists)
    scores: dict[str, float] = {}
    for ranks, w in zip(rank_lists, weights):
        for position, key in enumerate(ranks, start=1):
            scores[key] = scores.get(key, 0.0) + w / (k + position)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def minmax_fusion(score_lists: list[dict[str, float]], weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Score fusion: each list's raw scores are min-max scaled to [0, 1] over
    that list's own candidates (a list with one distinct score scales to 1),
    an item missing from a list contributes 0 for it, and the scaled scores
    are combined as a weighted sum (equal weights by default)."""
    weights = weights or [1.0 / len(score_lists)] * len(score_lists)
    fused: dict[str, float] = {}
    for scores, w in zip(score_lists, weights):
        if not scores:
            continue
        lo, hi = min(scores.values()), max(scores.values())
        for key, s in scores.items():
            scaled = (s - lo) / (hi - lo) if hi > lo else 1.0
            fused[key] = fused.get(key, 0.0) + w * scaled
    return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
