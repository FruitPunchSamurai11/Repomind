# RepoMind — Revision Round 2 (task brief for Claude Code)

> Read this file fully before doing anything. It sits next to the project's own `CLAUDE.md`
> and does **not** replace it: every guardrail in `CLAUDE.md` and `docs/PROJECT.md` still applies.
> Put this file in `docs/` and start each session with: *"Read docs/REVISION_ROUND2.md and continue from the checklist."*

---

## 0. Token budget rules (most important — follow them in every session)

Our Claude usage is limited. Claude's job is to **write and fix code**, not to run long jobs or read big outputs.

1. **Never run the full pipeline inside the chat session.** Write the script, run it once on the smallest
   repo (`ora`) to prove it works, then hand the full run to the human:
   `nohup python -m evaluation.run_all --repos repos_round2.txt > logs/run_all.log 2>&1 &`
2. **Never `cat` large files** (JSONL, logs, SQLite dumps, full JSON metrics). Use `head`, `tail -n 30`,
   `grep`, `wc -l`, or a short Python summary. Show at most ~30 lines of any output.
3. **Every experiment writes its results to a small CSV** in `results/round2/` (one row per repo).
   Paper tables are built from those CSVs by a script — never typed by hand, never read back in full.
4. **Read code by grepping first**, then open only the relevant function. Do not read whole modules
   you don't need to change.
5. When something fails during the human's overnight run, the human will paste **only the traceback**.
   Fix that, don't re-explore the codebase.
6. Keep a short progress log at the bottom of this file (Section 9) so the next session doesn't
   rediscover state. Update the checklist ticks as you go.

---

## 1. What RepoMind is (one paragraph)

RepoMind mines Git + GitHub history (commits, PRs, issues, reviews) into a provenance-preserving
SQLite model, computes activity-based knowledge-concentration and maintenance-risk indicators, and
answers repository questions with BM25 (+ dense/hybrid) retrieval and a citation-checked LLM.
Principle: **Data → Structured Knowledge → Retrieval → LLM**; the LLM is the last stage.
Modules: 0a git ingest · 0b GitHub GraphQL ingest (checkpointed) · 1 normalisation + identity
resolution · 2 SQLite + edge tables · 3 knowledge-map graph (HTML) · 4 concentration metrics ·
5 bus-factor/risk signals · 6 multi-repo evaluation · 7 retrieval · 8 grounded LLM (Gemini) ·
9 line-level code explanation · 10 pre-change briefing · 11 local Flask UI (6 screens, 105 tests).

---

## 2. Where the paper stands

### Submission history
| Stage | What happened |
|---|---|
| First submission | 8 repos, Gemini on 10 template questions, BM25 only, no backtest |
| Review round 1 (Reviewer #1, all criteria "Good") | Asked for: (a) more repositories, (b) identity validation vs. manually labelled data, (c) broader LLM eval, (d) dense + hybrid retrieval comparison, (e) no-retrieval baseline, (f) temporal validation of risk signals, (g) keep "indicators, not predictions" framing |
| Revision 1 (done by a previous Claude Code session) | Added (c), (d), (e), (f), (g); (b) only partly — validated against **GitHub's own commit attribution**, not manual labels; **(a) NOT done — still 8 repos** |
| Review round 2 (current) | **Revise and Resubmit.** "Results insufficient to establish originality/contribution", include the **implementation screenshots** referenced in methodology, **≤ 25 references** (currently 32), English language editing |

### Experiments described in Revision 1 — CODE NOT FOUND, REBUILD FROM SCRATCH
An audit (see Section 9) found **no scripts and no results** for any of these in this repo. Treat every
Revision-1 number in the submitted paper as **unverified**. Rebuild each experiment below, run it, and use
**only the numbers it produces** — do not aim for or reuse the paper's old numbers. The descriptions say what
to build, not what results to expect:
- **GitHub attribution check** — public commits API, 5 random pages × 100 commits per repo (all of `ora`), fixed seed; reports share GitHub attributes to an account and precision of RepoMind's links.
- **Reference-account identity merge** — merges sampled identities by GitHub's attributed account; Spearman ρ of rankings + risk-level changes (lower bound).
- **Temporal backtest** — component assessed at every 31 Dec cutoff (≥2 y before, ≥1 y after), commits ≤ t only, reviewer signal omitted; outcome = all active devs lost within 365 d; AUC vs. "fewer active devs" and "less recent activity"; cluster bootstrap over components.
- **Dense / hybrid retrieval** — `bge-small-en-v1.5` cosine; RRF of top-100 lists; min–max score fusion; paired bootstrap.
- **Extended LLM eval** — local Gemma 3 1B, 139 generated questions, RAG vs. closed-book vs. forced-guess, Wilson CIs, exact McNemar.
- **Prompt-injection test** — 42 PR descriptions, overt vs. disguised instruction, in-memory DB copy.

### Numbers in the revised paper (8 repos) — UNVERIFIED, for orientation only, never copy into new text
Linkage 1–31% of human commits (GitHub attributes 96–100% in 7 repos); PR→file linkage 0% in FastAPI, Black, ora;
ρ ≥ 0.96 under bot/name changes, as low as 0.28 under half-life; risk agreement down to 13% across windows;
backtest 66% (high) vs. 18% (low) lost, rule AUC 0.68 vs. 0.80 / 0.87 trivial predictors;
BM25 R@10 57–81%, hybrid 73% pooled; co-change beats baseline in 6/7.

---

## 3. Goal of this round

Main fix: **expand from 8 to ~20 repositories**, drawn by a documented random procedure, and re-run the
cheap, fully automatic experiments on all of them. Secondary: support a small **manual identity
labelling** set (humans label, not Claude), and support the figure/reference/language fixes.

**Page limit is 6 pages** (confirm with the chair whether extra pages are allowed). More repos must therefore
mean **more table rows and pooled numbers, not more text.**

---

## 4. Checklist

### Phase A — Orient (one short session)
- [x] Audit done: Revision-1 code is not in the repo; checkout = first submission (commit c81f2b8). Rebuilding from scratch.
- [x] Confirm the pipeline entry points for each module (just the commands, not the code). → `CLAUDE.md` § Commands; `run_all` calls them as subprocesses.
- [x] Check `.env` has `GITHUB_TOKEN`; support **multiple tokens** (`GITHUB_TOKENS=tok1,tok2,...`) with rotation on rate-limit, if not already present. Never print or log tokens.

### Phase B — Repository sample
- [x] Write `evaluation/sample_repos.py` that draws candidates via the GitHub search API and saves `repos_round2.txt` + `results/round2/sample_log.csv` (query, filters, seed, candidates seen, chosen, rejection reason).
  - Filters: public, non-fork, non-archived; issues and PRs in use; pushed within the last 12 months; ≥ 500 stars; **500–5,000 commits** (keeps GraphQL cost sane); history ≥ 4 years (needed for the backtest).
  - Stratify by language: Python, JavaScript/TypeScript, Go, Rust, Java (≈ 3 per language). Fixed seed.
  - Reject and log repos that are mostly docs/data, mirrors, or have < 50 closed issues linked to PRs.
- [x] Human reviews the list before any ingestion (don't spend API quota on a bad sample). → reviewed; flyway/flyway excluded as mirror (`HUMAN_EXCLUSIONS`), replaced by microsoft/playwright-java via same-seed re-run.
- [x] Keep the original 8; target **~20 total**. Paper must state the original 8 were hand-picked and the new ones random. → `repos_round2.txt` = 8 original + 12 random = 20.

### Phase C — Batch runner
- [x] Write `evaluation/run_all.py --repos <file>`: for each repo, run 0a → 0b → 1 → 2 → 4 → 5 and the cheap evaluations; **skip repos already completed** (marker file per stage), resume after crash/rate-limit, log one line per stage with timing.
- [x] One repo failing must not stop the batch; write the failure to `results/round2/failures.csv`.
- [x] Test end-to-end on `ora` only. Then stop and hand over the nohup command.

### Phase D — Experiments to re-run on all ~20 repos (no LLM, no paid API)
Each writes one CSV in `results/round2/`:
- [ ] `dataset.csv` — Table I columns (commits, span, files, committer IDs, bot share, issues, PRs, reviews). → **code done** (`evaluation/export_tables.py dataset`, stage `d_dataset`), tested on ora; awaiting batch re-run (new stages).
- [ ] `linkage.csv` — Table II (human commits → login, top-10 linked, PR→files, reviews→files, merge commits, `(#N)` subject share). → **code done** (`evaluation/export_tables.py linkage`, stage `d_linkage`), tested on ora; awaiting batch re-run (new stages).
- [ ] `attribution.csv` — GitHub attribution check (**subset is fine**: 1–2 pages/repo to save REST quota). → **code done** (`evaluation/attribution_check.py`, run_all stage `d_attribution`, 2 pages/repo, seed 0), tested on ora; awaiting batch.
- [ ] `concentration.csv` — Table III (ranked files, median N_eff, top ≥80%, components, ρ values, perturbations). → **code done** (`evaluation/export_tables.py concentration`, stage `d_concentration`), tested on ora; awaiting batch re-run (new stages).
- [ ] `risk.csv` — Table IV (levels at W=365, agreement @180/@730). → **code done** (`evaluation/export_tables.py risk`, stage `d_risk`), tested on ora; awaiting batch re-run (new stages).
- [ ] `backtest.csv` — per repo + pooled: loss rate by level, risk ratio, AUC of rule vs. trivial predictors, cluster-bootstrap CIs. Pooled numbers are the headline. → **code done** (`evaluation/backtest.py`, stage `d_backtest`), tested on ora; awaiting batch. Outcome B (`excl0_*`: component-years with zero next-year commits left out) added side by side; `--pool` refreshes all repo rows from case files.
- [ ] `retrieval.csv` — BM25 / dense / RRF / fusion R@1/5/10, MRR, chance; pooled with paired bootstrap. Dense embeddings run locally (CPU OK); cache embeddings to disk. → **code done** (`retrieval/dense.py`, `retrieval/ranking.py::minmax_fusion`, `evaluation/retrieval_compare.py`, stage `d_retrieval`); ora excluded (19 links < 20); awaiting batch.
- [ ] `cochange.csv` — hit@5 and precision vs. popularity baseline. → **code done** (`evaluation/export_tables.py cochange`, stage `d_cochange`), tested on ora; awaiting batch re-run (new stages).
- [x] New code to write for the above (did not exist): `evaluation/attribution_check.py`, `evaluation/identity_reference_merge.py`, `evaluation/backtest.py`, `retrieval/dense.py` + hybrid fusion in `retrieval/ranking.py`. Add any new dependency (e.g. `sentence-transformers`) to `requirements.txt`. Unit-test each on `ora` before the batch run. → done: attribution_check, backtest, dense + fusion. `identity_reference_merge.py` done (stage `d_identity_merge`, labelled lower bound).
- [~] (code done 2026-11-18, pilot OK; full run pending — human runs it) LLM evaluation (Reviewer #1 point c/e) must also be rebuilt: `evaluation/llm_eval.py` with RAG vs. closed-book vs. forced-guess on generated questions (answerable, unanswerable, false-premise), and `evaluation/injection_test.py` (overt vs. disguised instruction in PR text, in-memory DB copy). Run on **Flask only** (plus at most one more repo). Model: a small local model via Ollama (human installs it) or Gemini if quota allows — the paper must name whichever is actually used.
- [ ] Drop the fix-wording experiment from the paper (weak, tuned on its own labels) unless the human says keep it.

### Phase E — Manual identity labelling (addresses Reviewer #1 point b)
- [ ] Write `evaluation/export_identity_sample.py`: from 3 repos (e.g. Flask, Black, one new), sample ~150 human commits (fixed seed) and export CSV: `sha, author_name, author_email, repomind_developer_id, repomind_login, github_attributed_login, label_same_person (blank), notes (blank)`. → **done** (not a run_all stage: cross-repo, and must never overwrite a sheet being labelled; refuses without `--force`). Default repos flask, black, k3d-io/k3d (seed 0). Output in gitignored `results/round2/identity_labelling/` (names/e-mails). Run after the batch.
- [ ] **Claude does not fill the labels.** Two authors label independently; then write `evaluation/score_identity_labels.py` → precision/recall of RepoMind links and of GitHub attribution vs. human labels, plus Cohen's κ between the two labellers.

### Phase F — Tables and figures for the paper
- [ ] `paper/build_tables.py` reads the CSVs and emits LaTeX tables (≤ 1 table per RQ; per-repo rows compact, pooled row at bottom). With ~20 repos, consider moving per-repo detail of Tables III–IV into one combined table and reporting ranges + pooled values in text.
- [ ] Re-generate Fig. 1 (pipeline diagram) — it was removed in Revision 1 and must come back.
- [ ] **Screenshots (human does this):** run the Flask UI (`127.0.0.1`), capture Knowledge map (Module 3 graph), Risk screen, and Ask screen with citation warnings visible. Combine into **one two-column figure** with (a)(b)(c) panels. Claude may write a small script to tile/crop PNGs only.

### Phase G — Paper text (only after numbers exist)
- [ ] **Start from the ORIGINAL first-submission paper**, not the Revision-1 PDF. Nothing from the Revision-1 text or numbers is reused.
- [ ] Update abstract, Sections IV–VIII with new numbers from CSVs only. **Never type a number that isn't in a results file.**
- [ ] Cut references from 32 to **≤ 25**. Candidates (authors decide): [26] Spearman, [30] Hanley & McNeil, [31] Efron & Tibshirani, [11] Hirschman, [14] Tornhill, [16] Robles & González-Barahona, [21] SWE-bench. Renumber citations.
- [ ] Language pass: shorter sentences; move number lists (e.g. the co-change and fix-wording strings in V-E) into tables; consistent British spelling (paper uses "normalisation", "summarise").
- [ ] Keep the AI-use acknowledgment; update it to cover this round.
- [ ] Draft a point-by-point **response letter** covering round 1 (a–g) and round 2 (results, screenshots, references, language) — what changed and where (section/table numbers).

---

## 5. Guardrails (from CLAUDE.md / PROJECT.md — never violate)
- Commit count ≠ expertise. Risk levels are **indicators of recorded activity**, not measurements of expertise or predictions of failure.
- Unresolved identities are preserved, never guessed into a merge. Bots flagged, not deleted.
- Raw data is immutable; every derived artefact is regenerable.
- Tokens never logged, printed or written to data files.
- **No invented results.** If a stage fails for a repo, report it as failed/excluded with the reason. Negative results (e.g. Express co-change, rule AUC below trivial predictors) stay in the paper.
- Report exclusions the same way the paper already does (e.g. repos without issue–PR links excluded from retrieval).

## 6. Rate-limit notes
- GraphQL ≈ 5,000 points/hour per token; large repos (thousands of PRs with reviews) are expensive. Use checkpointing + token rotation; sleep until reset rather than failing.
- REST commits API (attribution check) has its own limit — sample, don't crawl.

## 7. Definition of done
- ~20 repos processed (or each failure documented), all CSVs in `results/round2/`, tables generated by script.
- Manual identity labels scored.
- Paper ≤ 6 pages (or the chair-approved limit), ≤ 25 references, Fig. 1 + screenshot figure present.
- Response letter drafted.

## 8. Files to read only if needed
`docs/PROJECT.md`, `docs/DATA_MODEL.md`, `docs/METRICS.md`, `docs/RISK.md`, `docs/EVALUATION.md`,
`EVALUATION_RESULTS.md`, `docs/RETRIEVAL.md`, `docs/LLM.md`, `docs/EXPLAIN.md`, `docs/ASSIST.md`, `docs/UI.md`.
Grep them for the specific term you need rather than reading them whole.

## 9. Progress log (Claude Code: append here, newest last, 1–3 lines per session)
- 2026-11-17: Audit — no Revision-1 code or results in repo (all branches at c81f2b8, 14 commits = Modules 0–11). Decision: rebuild Revision-1 experiments from scratch alongside the repo expansion.
- 2026-11-17 (Phases B+C): `ingest/github_client.py` now rotates `GITHUB_TOKENS=a,b,c` (falls back to `GITHUB_TOKEN`), per-resource limits, sleeps only when all tokens are exhausted; adds REST `get()`. New: `evaluation/sample_repos.py` (seed 2026, shuffled creation-month windows per language stratum, ≤1 pick/month; quotas py1/jsts2/go3/rust3/java3 to balance the original 8 → 20 total; 64 examined, 12 chosen; log in `results/round2/sample_log.csv`, list in `repos_round2.txt`), `evaluation/run_all.py` (subprocess per stage, markers in `data/run_all/`, logs in `logs/run_all/`, `results/round2/failures.csv`), `tests/test_round2.py` (17 tests; suite 122 passed). `ora` end-to-end: all 6 stages OK, resume verified. `logs/`, `data/run_all/` gitignored.
  **Next:** human reviews `repos_round2.txt` (look at sendgrid/sendgrid-go = exactly 50 linked issues, generated API client; flyway/flyway = vendor-synced repo) → then full batch. Phase D eval stages get appended to `run_all.STAGES`.
- 2026-11-18 (Phase D part 1): flyway excluded/replaced (see Phase B). Batch was **not running** at session start (no process, no log, no data beyond ora). New: `evaluation/stats.py` (AUC, cluster/stratified bootstrap, Wilson, read-only DB, per-repo CSV upsert), `evaluation/backtest.py`, `evaluation/attribution_check.py`, `retrieval/dense.py` (bge-small-en-v1.5, CPU, cache `data/processed/_embeddings/`), `retrieval/ranking.py::minmax_fusion`, `evaluation/retrieval_compare.py`; run_all stages `d_backtest`, `d_retrieval`, `d_attribution` + POOLED rows at batch end. Deps: sentence-transformers, numpy. Tests: 137 passed.
  ora: backtest 7 cutoffs/8 cases/2 components, 0 losses → AUC undefined; attribution GitHub 98/101 human, RepoMind 5/101, precision 5/5; retrieval excluded (19 < 20 links; diagnostic run with min 10 worked). Push of `revision-round2` fails: 403, git account lacks write access to Divishth2005/Repomind.
  **Next:** fix push access; run batch; `identity_reference_merge.py`; remaining Phase D CSVs (dataset, linkage, concentration, risk, cochange).
- 2026-11-18 (Phase D part 2): batch was running (flask, stage 0a) and untouched. New: `evaluation/export_tables.py` (dataset/linkage/concentration/risk/cochange, wraps Module 6/10; only new number = "(#N)" subject share of non-merge commits), `evaluation/identity_reference_merge.py`, `evaluation/export_identity_sample.py`; backtest outcome B; `stats.file_lock` on CSV upserts (batch and manual runs can't clobber each other). run_all: 6 new stages appended at the END (inserting earlier would invalidate d_* markers); stage spec may carry args. Tests: 149 passed. Committed, not pushed.
  ora: Table I 201 commits/10.7 y/24 files/57 IDs; "(#N)" share 31.3%; risk 3 medium, agreement 100%/100%; co-change hit@5 98.8% vs popularity 97.7% (all files); identity merge 33 ids→33 accounts, 0 merged, rho files 1.000, 0/3 levels changed; backtest B = A (no losses).
  **Note:** the running batch has the old stage list in memory, so it will NOT run the 6 new stages; re-run the same batch command after it ends (only new stages run). Its d_backtest stages already use the new code (outcome B), and its end-of-batch `backtest --pool` refreshes all rows.
- 2026-11-18 (Phase D LLM): batch untouched. New: `llm/client.py::OllamaClient` (provider `ollama`, gemma3:1b, temperature 0, seed 42, num_ctx 8192, num_predict ≤1024; Module 8 prompt/citation checks unchanged); `evaluation/llm_eval.py` (flask, cobra, k3d; 50 seeded questions/repo: 4×8 answerable, 3×3 unanswerable, 9 false-premise; rag / closed_book / forced_guess; string-match scoring; Wilson CIs; exact McNemar; resumable from `llm_eval_answers.csv`; model + settings in every row); `evaluation/injection_test.py` (in-memory copy of one repo's rows, 40 merged PRs half overt TOKEN123 / half disguised fake author "Vellacourt", clean baseline first; overt counts as followed only outside a verbatim quote, disguised counts any mention); `stats.mcnemar_exact`. Tests: 166 passed. Committed, not pushed.
  Flask pilot (scratch only, 5 q × 3 conditions): rag 3/4 answerable correct, 1/1 control unsafe (named a top committer for a non-existent file); closed_book 0/4 correct, 4/4 abstained, control safe; forced_guess 0/4, all confidently wrong. Injection pilot (5+5): followed overt 0/5 (3/5 quoted the instruction), disguised 2/5; clean baseline 0/10. Note: Ollama at temperature 0/fixed seed changed one answer's wording between two identical runs.
  **Next:** human runs the full LLM eval + injection test (command in session reply), then `--summarise-only` if interrupted.
- 2026-11-18 (LLM eval fix): full run crashed at k3d forced_guess issue_closed_by-0 (Ollama HTTP 500 "token repeat limit reached"; 374/450 answers saved). `llm_eval.py`/`injection_test.py` now record an `LLMError` as outcome `generation_failed` (error text kept; success 0, not an abstention, not confidently wrong; own metric + `generation_failed` column), settings unchanged; old answers file is upgraded in place on resume (answers unchanged). Failed generations are not retried on resume. Injection test had already completed after the crash (old code, no failures). Tests: 170 passed.
- 2026-11-18 (vendored-code rule): `analysis/exclusions.py` drops paths with a directory segment in vendor, vendored, _vendor, third_party, third-party, thirdparty, 3rdparty, node_modules, bower_components (env `REPOMIND_EXCLUDE_DIRS`, "none" disables). Applied in `analysis.data.load_activity` (Modules 4/5, experiments, exports, identity merge, Retriever.assess, UI) and `backtest.load_commits`; `path_exclusion` + `excluded_files` recorded in Module 4/5 JSON and in dataset/concentration/risk/backtest/identity_merge CSVs. Files excluded: k3d 9,478/9,925, faas-netes 7,165/8,488, opentogethertube 22, ouch 22, sendgrid-go 10, dangerzone 2, others 0. `run_all --preserve`. Cleared markers of 4_concentration, 5_risk, d_backtest, d_identity_merge and the five export stages (180); ingestion/retrieval/attribution untouched. Tests: 176 passed. **Next:** human re-runs those stages, then SUMMARY.md update.
- 2026-11-18 (reproducibility): (1) Developer rows depended on repo load order: ids were global hashes but attributes came from each repo's own resolution and `INSERT OR REPLACE` let the last-loaded repo win. Measured read-only from `data/processed/*/developers.jsonl`: 31,347 ids, 1,442 in ≥2 repos, 85 with differing attributes (display_name 75, identity_confidence 64, sources 64; github_login and is_bot never differ → attribution unaffected, 0 sampled rows). Linkage share in the DB differed from each repo's own resolution in 7 repos (flask 2.14→1.60%, requests 3.09→2.85%, fastapi 13.45→13.27%, ripgrep 4.26→4.13%, black 25.26→24.74%, cobra 10.20→9.93%, ora 3.98→3.48%); possible range over orders widest for black 13.1–25.3% and ouch 4.2–8.5%. Fix: `IdentityResolver(scope=repo)` mixes the repo into every developer_id (email_hash stays global); `load_all` reports `developer_conflicts` and prunes unreferenced developer rows. (2) `stats.bootstrap`/`bootstrap_stratified` put units in a canonical order (values formatted as in the CSVs) before resampling; backtest case files are sorted by (cutoff, component). Tests: 182 passed. Cleared every marker except 0a_git, 0b_github, d_retrieval (240). **Next:** human re-runs the batch with `--preserve d_retrieval`; llm_eval/injection are not run_all stages and were not re-run.
- 2026-11-18 (repro results + paper check): repro batch finished 20/20 (`logs/run_all_repro.log`, 21.7 min).
  - **Re-score:** `llm_eval --rescore` re-scores saved answers against the current DB without generating any. 0/450 scores and 0 expected answers changed; injection 0/80 changed. 3 k3d false-premise questions no longer pass the premise check under scoped ids and are listed by `--rescore`.
  - **Bootstrap determinism:** backtest/retrieval pooling run twice → identical. `retrieval_compare --pool` now also recomputes the per-repo rows from their case files.
  - **Co-change determinism:** the popularity baseline breaks ties by path, and `load_commits` orders equal timestamps by commit id. All Module 4/5/co-change/merge exports are byte-identical under `PYTHONHASHSEED` 1 and 2.
  - **Tests:** 184 pass (+2: co-change tie order, re-score).
  - **SUMMARY:** old → new values and a "Reproducibility fixes" section.
  - **Paper:** `docs/paper/build_tables.py` → `docs/paper/tables/repos.tex`. 28 of 181 `\res` values in `docs/paper/main.tex` corrected; numbers only, all other text byte-identical.
  - **Next:** the `\fixme` items (manual labels, manual LLM check, data date, UI figure).
- 2026-11-18 (Phase E scored): `evaluation/score_identity_labels.py` scores `identity_labelling/labels_karthikeya.csv` → aggregate `results/round2/identity_labels.csv` (sheet stays gitignored).
  - Labelling: single labeller; AI pre-filled, verified by one author, who checked all 14 low-evidence identities (41 commits) on their GitHub profiles.
  - Labels: 150 yes, 0 no, 0 unsure. RepoMind precision 25/25 [86.7, 100]; GitHub 150/150 [97.5, 100]; RepoMind coverage 16.7%. All sampled commits were GitHub-attributed. No κ (one labeller).
  - Output: SUMMARY updated; RQ1 `\fixme` in `docs/paper/main.tex` filled. +2 tests (186 pass).
