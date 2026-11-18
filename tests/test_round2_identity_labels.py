"""Scoring of manual identity labels (aggregate only)."""
import pytest

from evaluation.score_identity_labels import score


def _r(label, rm="", gh=""):
    return {"repo": "o/r", "sha": "abc", "repomind_login": rm, "github_attributed_login": gh,
            "label_same_person": label, "_label": label}


def test_precision_counts_exclude_unsure_and_login_disagreements():
    rows = [_r("yes", "a", "a"), _r("yes", "", "b"), _r("no", "", "c"), _r("unsure", "d", "d"),
            _r("yes", "e", "f"), _r("no", "g", "")]
    s = score("o/r", rows, "x")
    assert (s["yes"], s["no"], s["unsure"], s["login_disagreements"]) == (3, 2, 1, 1)
    assert (s["repomind_correct"], s["repomind_precision"]) == (1, "0.5000")      # a yes, g no
    assert (s["github_correct"], s["github_precision"]) == (2, "0.6667")          # a, b yes; c no
    assert s["confirmed_attributed"] == 2 and s["repomind_coverage"] == "0.5000"


def test_unknown_label_is_rejected(tmp_path):
    from evaluation.score_identity_labels import read_labels
    p = tmp_path / "l.csv"
    p.write_text("repo,sha,repomind_login,github_attributed_login,label_same_person\no/r,abc,,x,maybe\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_labels(p)
