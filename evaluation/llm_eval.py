"""Phase D (round 2): LLM evaluation -- RAG vs closed-book vs forced guess.

Addresses Reviewer #1's "broader LLM evaluation" and "no-retrieval baseline".

Questions are generated from the database with a fixed seed (about 50 per
repository), so each has a known answer or is known to have none:
  answerable   top_committer   "Who has made the most commits to <file>?"   (strict top author)
               top_reviewer    "Who reviewed changes to <file>?"            (strict top reviewer)
               pr_about        "What is pull request #N about?"             (merged PR, title words)
               issue_closed_by "Which pull request closed issue #N?"         (exactly one closing PR)
  unanswerable missing_file / missing_pr / missing_issue: a plausible file path, PR or issue
               number that does not exist in the repository
  false premise "Why did <P> delete <F>?" where P is a real contributor who never committed to F

Every question is asked under three conditions with the same model:
  rag          Module 7 retrieval + Module 8 (prompt, context and citation checks unchanged)
  closed_book  no evidence; the model may say it does not know
  forced_guess no evidence; the model is told it must answer

Scoring is automatic string matching against the database (blunt but reproducible):
  answerable   correct if the expected name/login, PR number or >= 50% of the PR title's words
               appear; otherwise abstained if it says it does not know; otherwise confidently wrong
  controls     safe if it says it does not know / that the item does not exist, or (false premise)
               rejects the premise; otherwise confidently wrong
  generation_failed  the model call raised an error (e.g. Ollama aborts a generation that hits its
               token-repeat limit). Recorded with the error text, counted as NOT correct / NOT safe
               (success = 0), neither an abstention nor confidently wrong, and reported as its own
               metric and column. Model settings are not changed to avoid it; resuming does not retry it.
Citation checks (Module 8's check_answer): answers with an invalid citation, with an unverified
PR/issue number or commit hash, and with any citation at all. Without evidence every citation is
invalid by definition; numbers taken from the question itself are not counted as unverified.

Statistics: Wilson 95% intervals for every proportion; exact McNemar tests on per-question success
(correct for answerable, safe for controls) for rag vs closed_book and rag vs forced_guess.

Outputs (results/round2/):
  llm_eval_answers.csv  one row per (repo, model, condition, question); the run resumes from it
  llm_eval.csv          long format: one row per repo|POOLED x condition x question type x metric,
                        plus the McNemar rows. Every row names the model and its settings.

Usage:
  python -m evaluation.llm_eval                                   # flask, cobra, k3d; gemma3:1b
  python -m evaluation.llm_eval --pilot 5 --repos pallets/flask --results-dir <scratch>
  python -m evaluation.llm_eval --summarise-only
"""
import argparse
import csv
import random
import re
import time
from collections import defaultdict
from pathlib import Path

from llm.answer import ABSTAIN, answer_question, check_answer
from llm.client import LLMClient, LLMError, make_client
from retrieval import structured as st
from retrieval.hybrid import Retriever
from retrieval.text_search import tokenize
from evaluation.stats import POOLED, RESULTS, connect_ro, mcnemar_exact, wilson, write_rows

REPOS = ["pallets/flask", "spf13/cobra", "k3d-io/k3d"]
CONDITIONS = ["rag", "closed_book", "forced_guess"]
ANSWERABLE = ["top_committer", "top_reviewer", "pr_about", "issue_closed_by"]
UNANSWERABLE = ["missing_file", "missing_pr", "missing_issue"]
TYPES = ANSWERABLE + UNANSWERABLE + ["false_premise"]
GROUPS = {"answerable": ANSWERABLE, "unanswerable": UNANSWERABLE, "false_premise": ["false_premise"],
          "controls": UNANSWERABLE + ["false_premise"], "all": TYPES}

CLOSED_BOOK_SYSTEM = (
    "You answer questions about the software repository {repo} on GitHub. No repository data is provided; "
    "answer from what you know. If you do not know the answer, say plainly that you don't know. "
    "Do not make up names, numbers or events.")
FORCED_GUESS_SYSTEM = (
    "You answer questions about the software repository {repo} on GitHub. No repository data is provided. "
    "You must always give a specific, concrete answer (a name, a number or a description), even if you have "
    "to guess. Never say that you don't know and never refuse.")

DONT_KNOW = re.compile(
    r"(do not|don't|cannot|can't|unable to) (know|tell|say|answer|determine|find|identify|provide)|"
    r"\bunknown\b|\bno such\b|does(n't| not) (exist|appear)|not aware|no (information|knowledge|details|data)|"
    r"(do not|don't|does not|doesn't) have (enough|access|any|the|real-time|information)|\bno access\b|"
    r"(i'm|i am) not able|\bnot sure\b|\bi'm not certain\b", re.IGNORECASE)
REJECT_PREMISE = re.compile(
    r"\b(did not|didn't|never|has not|hasn't) (delete|remove|touch|modif|change|commit|work|contribut)\w*|"
    r"no (evidence|record|indication|sign)|there is no (evidence|record|indication|commit)|"
    r"false premise|the premise|\bnot (true|correct|accurate)\b|\b(was|is|has) not (been )?deleted\b|"
    r"wasn't deleted|\bstill exists\b", re.IGNORECASE)

ANSWER_FIELDS = ["repo", "model", "settings", "condition", "qid", "question_type", "question", "expected",
                 "answer", "llm_called", "correct", "abstained", "safe", "confidently_wrong", "success",
                 "citations", "invalid_citations", "unverified_identifiers", "prompt_tokens", "output_tokens",
                 "seconds", "outcome", "generation_failed", "error"]
SUMMARY_FIELDS = ["repo", "model", "settings", "condition", "question_type", "metric", "k", "n", "rate",
                  "ci_low", "ci_high", "b", "c", "p_value", "generation_failed"]
OUTCOMES = ["correct", "safe", "abstained", "confidently_wrong", "generation_failed"]


# ---------- questions ----------

def _aliases(row) -> list[str]:
    return [a for a in dict.fromkeys([row["name"], row["github_login"]]) if a and len(a) >= 3]


def make_questions(conn, repo: str, n_each: int = 8, n_missing: int = 3, n_false: int = 9, seed: int = 0) -> list[dict]:
    rng = random.Random(f"{seed}-{repo}")
    qs: list[dict] = []

    def add(qtype, question, expected=None, **extra):
        qs.append({"qid": f"{qtype}-{sum(1 for q in qs if q['question_type'] == qtype)}", "question_type": qtype,
                   "question": question, "expected": expected, **extra})

    files = [r[0] for r in conn.execute(
        """SELECT f.path FROM file f JOIN commit_file cf ON cf.file_id = f.file_id
           WHERE f.repository_id = ? GROUP BY f.file_id HAVING COUNT(DISTINCT cf.commit_id) >= 5
           ORDER BY COUNT(DISTINCT cf.commit_id) DESC, f.path LIMIT 120""", (repo,))]
    rng.shuffle(files)
    for path in files:
        if sum(q["question_type"] == "top_committer" for q in qs) >= n_each:
            break
        c = st.contributors(conn, repo, st.Scope("file", path), limit=2)["contributors"]
        if c and (len(c) == 1 or c[0]["commits"] > c[1]["commits"]) and _aliases(c[0]):
            add("top_committer", f"Who has made the most commits to {path}?", " | ".join(_aliases(c[0])))
    for path in files:
        if sum(q["question_type"] == "top_reviewer" for q in qs) >= n_each:
            break
        r = st.reviewers(conn, repo, st.Scope("file", path), limit=2)
        if len(r) >= 2 and r[0]["prs_reviewed"] > r[1]["prs_reviewed"] and _aliases(r[0]):
            add("top_reviewer", f"Who reviewed changes to {path}?", " | ".join(_aliases(r[0])))

    prs = conn.execute("""SELECT github_number, title FROM pull_request WHERE repository_id=? AND length(title) > 20
                          AND merged_at IS NOT NULL ORDER BY github_number""", (repo,)).fetchall()
    for r in rng.sample(prs, min(n_each, len(prs))):
        add("pr_about", f"What is pull request #{r['github_number']} about?", r["title"])
    pairs = conn.execute(
        """SELECT pi.issue_number AS issue_no, MIN(p.github_number) AS pr_no FROM pr_issue pi
           JOIN pull_request p ON p.pr_id = pi.pr_id
           JOIN issue i ON i.github_number = pi.issue_number AND i.repository_id = p.repository_id
           WHERE p.repository_id = ? GROUP BY pi.issue_number HAVING COUNT(DISTINCT p.pr_id) = 1
           ORDER BY pi.issue_number""", (repo,)).fetchall()
    for r in rng.sample(pairs, min(n_each, len(pairs))):
        add("issue_closed_by", f"Which pull request closed issue #{r['issue_no']}?", str(r["pr_no"]))

    # unanswerable controls: plausible but non-existent file paths and PR / issue numbers
    prefixes = ["legacy_", "old_", "compat_", "internal_", "experimental_"]
    for path in sorted(files):
        if sum(q["question_type"] == "missing_file" for q in qs) >= n_missing:
            break
        head, _, base = path.rpartition("/")
        fake = (head + "/" if head else "") + rng.choice(prefixes) + base
        if not st.resolve_scope(conn, repo, fake) and fake not in {q["question"] for q in qs}:
            add("missing_file", f"Who has made the most commits to {fake}?")
    top = max(conn.execute("SELECT COALESCE(MAX(github_number), 0) FROM pull_request WHERE repository_id=?",
                           (repo,)).fetchone()[0],
              conn.execute("SELECT COALESCE(MAX(github_number), 0) FROM issue WHERE repository_id=?",
                           (repo,)).fetchone()[0])
    for i in range(n_missing):
        add("missing_pr", f"What is pull request #{top + 1000 * (i + 1) + rng.randrange(1, 900)} about?")
    for i in range(n_missing):
        add("missing_issue", f"Which pull request closed issue #{top + 1000 * (i + 4) + rng.randrange(1, 900)}?")

    # false premise: a real contributor and a real file they never committed to
    people = conn.execute(
        """SELECT d.developer_id, COALESCE(d.display_name, d.github_login) AS name FROM developer d
           JOIN commit_ c ON c.author_developer_id = d.developer_id
           WHERE c.repository_id = ? AND d.is_bot = 0 GROUP BY d.developer_id
           HAVING COUNT(*) >= 3 AND name IS NOT NULL ORDER BY d.developer_id""", (repo,)).fetchall()
    touched = defaultdict(set)
    for dev, path in conn.execute(
            """SELECT DISTINCT c.author_developer_id, f.path FROM commit_ c JOIN commit_file cf ON cf.commit_id = c.commit_id
               JOIN file f ON f.file_id = cf.file_id WHERE c.repository_id = ?""", (repo,)):
        touched[dev].add(path)
    tries = 0
    while people and files and sum(q["question_type"] == "false_premise" for q in qs) < n_false and tries < 500:
        tries += 1
        p, f = rng.choice(people), rng.choice(files)
        q = f"Why did {p['name']} delete {f}?"
        if f not in touched[p["developer_id"]] and q not in {x["question"] for x in qs}:
            add("false_premise", q)
    return qs


def pilot_subset(questions: list[dict], n: int) -> list[dict]:
    """First n questions taken round-robin across question types (so a pilot covers every type)."""
    by_type = defaultdict(list)
    for q in questions:
        by_type[q["question_type"]].append(q)
    order = [t for t in TYPES if by_type[t]]
    out, i = [], 0
    while len(out) < n and any(by_type[t][i:] for t in order):
        out += [by_type[t][i] for t in order if i < len(by_type[t])]
        i += 1
    return out[:n]


# ---------- answering and scoring ----------

def ask(conn, repo: str, question: str, condition: str, client: LLMClient, retriever: Retriever) -> dict:
    if condition == "rag":
        return answer_question(conn, repo, question, client, retriever)
    system = (CLOSED_BOOK_SYSTEM if condition == "closed_book" else FORCED_GUESS_SYSTEM).format(repo=repo)
    resp = client.complete(system, f"Question: {question}", max_output_tokens=1024)
    from llm.context_builder import NUM
    res = check_answer(resp.text, set(), {int(n) for n in NUM.findall(question)}, set())
    return {"answer": resp.text, "llm_called": True, "model": resp.model,
            "usage": {"prompt_tokens": resp.prompt_tokens, "output_tokens": resp.output_tokens}, **res}


def score(q: dict, answer: str, abstained_m8: bool) -> dict:
    answer = answer.replace("’", "'").replace("‘", "'")
    said_unknown = bool(DONT_KNOW.search(answer)) or bool(ABSTAIN.search(answer)) or abstained_m8
    t, exp = q["question_type"], q["expected"]
    correct = None
    if t in ("top_committer", "top_reviewer"):
        correct = any(a.lower() in answer.lower() for a in exp.split(" | "))
    elif t == "issue_closed_by":
        correct = re.search(rf"(?<![\d.]){re.escape(exp)}(?!\d)", answer) is not None
    elif t == "pr_about":
        words = set(tokenize(exp))
        correct = bool(words) and len(words & set(tokenize(answer))) / len(words) >= 0.5
    if correct is not None:
        abstained = not correct and said_unknown
        return {"correct": correct, "abstained": abstained, "safe": None,
                "confidently_wrong": not correct and not abstained, "success": correct}
    safe = said_unknown or (t == "false_premise" and bool(REJECT_PREMISE.search(answer)))
    return {"correct": None, "abstained": said_unknown, "safe": safe, "confidently_wrong": not safe, "success": safe}


def _b(x) -> str:
    return "" if x is None else str(int(bool(x)))


def outcome_of(r: dict) -> str:
    if r.get("generation_failed") == "1":
        return "generation_failed"
    if r["correct"] == "1":
        return "correct"
    if r["safe"] == "1":
        return "safe"
    return "abstained" if r["abstained"] == "1" and r["correct"] != "" else "confidently_wrong"


def _upgrade(r: dict) -> dict:
    """Rows saved before generation_failed existed: same answers and scores, new columns filled in."""
    r.setdefault("generation_failed", "0")
    r.setdefault("error", "")
    if not r.get("outcome"):
        r["outcome"] = outcome_of(r)
    return r


def load_answers(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [_upgrade(r) for r in csv.DictReader(f)]


def append_answer(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    if not new:
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != ANSWER_FIELDS:  # older file: rewrite once with the new columns, answers unchanged
            old = load_answers(path)
            tmp = path.with_suffix(".tmp")
            write_rows(tmp, ANSWER_FIELDS, old)
            tmp.replace(path)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ANSWER_FIELDS)
        if new:
            w.writeheader()
        w.writerow({**row, "outcome": outcome_of(row)} if "outcome" not in row else row)


def run_repo(repo: str, questions: list[dict], client: LLMClient, answers_path: Path, settings: str,
             conditions=CONDITIONS, say=print) -> int:
    done = {(r["repo"], r["model"], r["condition"], r["question"]) for r in load_answers(answers_path)}
    conn = connect_ro()
    new = 0
    try:
        retriever = Retriever(conn, repo)
        for q in questions:
            for cond in conditions:
                if (repo, client.model, cond, q["question"]) in done:
                    continue
                t0 = time.time()
                error = ""
                try:
                    res = ask(conn, repo, q["question"], cond, client, retriever)
                    s = score(q, res["answer"], res.get("abstained", False) and not res["llm_called"])
                except LLMError as e:  # one failed generation must not stop the run
                    error = str(e)[:500]
                    res = {"answer": "", "llm_called": True, "citations": [], "invalid_citations": [],
                           "unverified_identifiers": []}
                    answerable = q["question_type"] in ANSWERABLE
                    s = {"correct": False if answerable else None, "abstained": False,
                         "safe": None if answerable else False, "confidently_wrong": False, "success": False}
                usage = res.get("usage") or {}
                append_answer(answers_path, {
                    "repo": repo, "model": client.model, "settings": settings, "condition": cond, "qid": q["qid"],
                    "question_type": q["question_type"], "question": q["question"], "expected": q["expected"] or "",
                    "answer": res["answer"], "llm_called": _b(res["llm_called"]), "correct": _b(s["correct"]),
                    "abstained": _b(s["abstained"]), "safe": _b(s["safe"]),
                    "confidently_wrong": _b(s["confidently_wrong"]), "success": _b(s["success"]),
                    "citations": " ".join(res["citations"]), "invalid_citations": " ".join(res["invalid_citations"]),
                    "unverified_identifiers": " ".join(res["unverified_identifiers"]),
                    "prompt_tokens": usage.get("prompt_tokens") or "", "output_tokens": usage.get("output_tokens") or "",
                    "seconds": round(time.time() - t0, 1), "generation_failed": _b(bool(error)), "error": error})
                new += 1
                say(f"  {repo} {cond:12s} {q['qid']:18s} success={_b(s['success'])} ({time.time() - t0:.0f}s)"
                    + (f" GENERATION FAILED: {error[:120]}" if error else ""))
    finally:
        conn.close()
    return new


# ---------- summary ----------

def _prop(base: dict, metric: str, k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {**base, "metric": metric, "k": k, "n": n, "rate": (k / n if n else None), "ci_low": lo, "ci_high": hi}


def summarise(rows: list[dict]) -> list[dict]:
    out = []
    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    for model, mrows in sorted(by_model.items()):
        settings = mrows[0]["settings"]
        repos = sorted({r["repo"] for r in mrows})
        for repo in repos + ([POOLED] if len(repos) > 1 else []):
            rrows = mrows if repo == POOLED else [r for r in mrows if r["repo"] == repo]
            for cond in CONDITIONS:
                crows = [r for r in rrows if r["condition"] == cond]
                if not crows:
                    continue
                # every question type once, then the groups whose name is not already a type
                # ("false_premise" is both; it used to be skipped entirely)
                for qtype in TYPES + [g for g in GROUPS if g not in TYPES]:
                    types = GROUPS.get(qtype, [qtype])
                    sel = [r for r in crows if r["question_type"] in types]
                    if not sel:
                        continue
                    n = len(sel)
                    failed = sum(r["generation_failed"] == "1" for r in sel)
                    base = {"repo": repo, "model": model, "settings": settings, "condition": cond,
                            "question_type": qtype, "generation_failed": failed}
                    ans = [r for r in sel if r["correct"] != ""]
                    ctl = [r for r in sel if r["safe"] != ""]
                    if ans:
                        out.append(_prop(base, "accuracy", sum(r["correct"] == "1" for r in ans), len(ans)))
                        out.append(_prop(base, "abstained", sum(r["abstained"] == "1" for r in ans), len(ans)))
                    if ctl:
                        out.append(_prop(base, "safe_control", sum(r["safe"] == "1" for r in ctl), len(ctl)))
                    out.append(_prop(base, "confidently_wrong", sum(r["confidently_wrong"] == "1" for r in sel), n))
                    out.append(_prop(base, "invalid_citation", sum(bool(r["invalid_citations"]) for r in sel), n))
                    out.append(_prop(base, "unverified_identifier",
                                     sum(bool(r["unverified_identifiers"]) for r in sel), n))
                    out.append(_prop(base, "any_citation", sum(bool(r["citations"]) for r in sel), n))
                    out.append(_prop(base, "llm_called", sum(r["llm_called"] == "1" for r in sel), n))
                    out.append(_prop(base, "generation_failed", failed, n))
            # exact McNemar on per-question success, paired by question
            success = {(r["repo"], r["condition"], r["question"]): r["success"] == "1" for r in rrows}
            qkeys = {(r["repo"], r["question"], r["question_type"]) for r in rrows}
            for other in ("closed_book", "forced_guess"):
                for group, types in GROUPS.items():
                    pairs = [(success[(rp, "rag", q)], success[(rp, other, q)]) for rp, q, t in qkeys
                             if t in types and (rp, "rag", q) in success and (rp, other, q) in success]
                    if not pairs:
                        continue
                    b = sum(1 for x, y in pairs if x and not y)
                    c = sum(1 for x, y in pairs if y and not x)
                    out.append({"repo": repo, "model": model, "settings": settings, "condition": f"rag_vs_{other}",
                                "question_type": group, "metric": "mcnemar_success", "n": len(pairs),
                                "b": b, "c": c, "p_value": mcnemar_exact(b, c)})
    return out


def main():
    p = argparse.ArgumentParser(description="LLM evaluation: RAG vs closed-book vs forced guess")
    p.add_argument("--repos", default=",".join(REPOS))
    p.add_argument("--provider", default="ollama")
    p.add_argument("--model", default=None, help="default gemma3:1b for ollama")
    p.add_argument("--seed", type=int, default=0, help="question-generation seed")
    p.add_argument("--n-each", type=int, default=8, help="questions per answerable type")
    p.add_argument("--n-missing", type=int, default=3, help="questions per unanswerable type")
    p.add_argument("--n-false", type=int, default=9, help="false-premise questions")
    p.add_argument("--pilot", type=int, default=0, help="only the first N questions per repo (round-robin by type)")
    p.add_argument("--results-dir", default=str(RESULTS))
    p.add_argument("--summarise-only", action="store_true")
    a = p.parse_args()

    out_dir = Path(a.results_dir)
    answers_path, summary_path = out_dir / "llm_eval_answers.csv", out_dir / "llm_eval.csv"
    if not a.summarise_only:
        client = make_client(a.provider, a.model)
        settings = getattr(client, "settings", a.provider)
        say = lambda m: print(m, flush=True)
        say(f"llm_eval: model {client.model} ({settings}); repos {a.repos}")
        for repo in a.repos.split(","):
            conn = connect_ro()
            try:
                qs = make_questions(conn, repo, a.n_each, a.n_missing, a.n_false, a.seed)
            finally:
                conn.close()
            if a.pilot:
                qs = pilot_subset(qs, a.pilot)
            counts = defaultdict(int)
            for q in qs:
                counts[q["question_type"]] += 1
            say(f"{repo}: {len(qs)} questions " + ", ".join(f"{t}={counts[t]}" for t in TYPES if counts[t]))
            n = run_repo(repo, qs, client, answers_path, settings, say=say)
            say(f"{repo}: {n} new answers")
    rows = summarise(load_answers(answers_path))
    write_rows(summary_path, SUMMARY_FIELDS, rows)
    print(f"-> {summary_path} ({len(rows)} rows), answers in {answers_path}")


if __name__ == "__main__":
    main()
