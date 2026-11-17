"""Phase C (Revision round 2): resumable batch runner over many repositories.

For each repo in --repos, runs the existing module CLIs as subprocesses:
    0a git ingest -> 0b GitHub ingest -> 1 normalise -> 2 database
    -> 4 concentration -> 5 risk   (+ Phase D evaluation stages, appended to STAGES)

  * A stage that succeeds writes a marker data/run_all/<owner>__<name>/<stage>.done;
    marked stages are skipped on the next run, so the batch resumes after a
    crash, a reboot or a rate-limit failure (0b also resumes mid-way from its
    own cursor checkpoint).
  * When a stage actually runs, markers of all later stages of that repo are
    removed first, so downstream results are never stale.
  * A failing stage is retried (--retries), then logged to
    results/round2/failures.csv; that repo's later stages are skipped and the
    batch moves on to the next repo.
  * One line per stage goes to stdout; each stage's full output goes to
    logs/run_all/<owner>__<name>/<stage>.log.
  * GitHub tokens come from .env (GITHUB_TOKENS=a,b,c or GITHUB_TOKEN) and are
    rotated on rate limits inside ingest/github_client.py; this script never
    reads or prints them.

Usage:
    python -m evaluation.run_all --repos repos_round2.txt
    python -m evaluation.run_all --repos repos_round2.txt --only sindresorhus/ora
    python -m evaluation.run_all --repos repos_round2.txt --stages 0a_git,1_normalize
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER_DIR = ROOT / "data" / "run_all"
LOG_DIR = ROOT / "logs" / "run_all"
FAILURES_CSV = ROOT / "results" / "round2" / "failures.csv"
FAILURE_FIELDS = ["time", "repo", "stage", "exit_code", "seconds", "attempts", "error", "log"]

# (stage name, module). Each is run as: python -m <module> --repo owner/name
# Phase D experiments are added here as they are written.
STAGES: list[tuple[str, str]] = [
    ("0a_git", "ingest.cli"),
    ("0b_github", "ingest.github_cli"),
    ("1_normalize", "normalization.cli"),
    ("2_database", "database.cli"),
    ("4_concentration", "analysis.cli"),
    ("5_risk", "analysis.risk_cli"),
]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def say(msg: str):
    print(f"{now()} {msg}", flush=True)


def read_repos(path: Path) -> list[str]:
    repos = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.count("/") != 1:
            raise SystemExit(f"{path}: not an owner/name entry: {line!r}")
        repos.append(line)
    names: dict[str, str] = {}
    for r in repos:  # data/raw/<name> is keyed by the short name (case-insensitive on Windows)
        short = r.split("/")[1].lower()
        if short in names and names[short] != r:
            raise SystemExit(f"{path}: {r} and {names[short]} share the short name '{short}', "
                             "which would overwrite each other's data/raw/<name>")
        names[short] = r
    return list(dict.fromkeys(repos))


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def marker(repo: str, stage: str) -> Path:
    return MARKER_DIR / slug(repo) / f"{stage}.done"


def last_error_line(log: Path) -> str:
    try:
        lines = [l.strip() for l in log.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except OSError:
        return ""
    return (lines[-1] if lines else "")[:300]


def record_failure(row: dict):
    FAILURES_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not FAILURES_CSV.exists()
    with FAILURES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FAILURE_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def run_stage(repo: str, stage: str, module: str, retries: int, timeout: float | None) -> bool:
    log = LOG_DIR / slug(repo) / f"{stage}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    t0 = time.time()
    code, error = None, ""
    for attempt in range(1, retries + 2):
        with log.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {now()} {repo} {stage} attempt {attempt} =====\n")
            f.flush()
            try:
                code = subprocess.run([sys.executable, "-m", module, "--repo", repo], cwd=ROOT,
                                      stdout=f, stderr=subprocess.STDOUT, env=env, timeout=timeout).returncode
                error = "" if code == 0 else last_error_line(log)
            except subprocess.TimeoutExpired:
                code, error = -1, f"timed out after {timeout}s"
        if code == 0:
            secs = time.time() - t0
            marker(repo, stage).write_text(json.dumps({"finished_at": now(), "seconds": round(secs, 1),
                                                       "attempts": attempt}), encoding="utf-8")
            say(f"{repo:35s} {stage:16s} OK    {secs:8.1f}s")
            return True
        if attempt <= retries:
            say(f"{repo:35s} {stage:16s} retry {attempt}/{retries} after error: {error}")
            time.sleep(60)
    secs = time.time() - t0
    say(f"{repo:35s} {stage:16s} FAIL  {secs:8.1f}s exit={code}: {error}")
    record_failure({"time": now(), "repo": repo, "stage": stage, "exit_code": code,
                    "seconds": round(secs, 1), "attempts": retries + 1, "error": error,
                    "log": str(log.relative_to(ROOT))})
    return False


def run_repo(repo: str, stages: list[tuple[str, str]], retries: int, timeout: float | None) -> bool:
    (MARKER_DIR / slug(repo)).mkdir(parents=True, exist_ok=True)
    all_names = [s for s, _ in STAGES]
    for stage, module in stages:
        if marker(repo, stage).exists():
            say(f"{repo:35s} {stage:16s} SKIP  (already done)")
            continue
        for later in all_names[all_names.index(stage) + 1:]:  # invalidate downstream results
            marker(repo, later).unlink(missing_ok=True)
        if not run_stage(repo, stage, module, retries, timeout):
            say(f"{repo:35s} later stages skipped for this repo")
            return False
    return True


def main():
    p = argparse.ArgumentParser(description="Resumable RepoMind batch runner (Revision round 2)")
    p.add_argument("--repos", required=True, help="file with one owner/name per line (# comments ok)")
    p.add_argument("--only", default=None, help="comma-separated subset of repos from the file")
    p.add_argument("--stages", default=None, help=f"comma-separated subset of {[s for s, _ in STAGES]}")
    p.add_argument("--retries", type=int, default=1, help="retries per failing stage (60 s apart)")
    p.add_argument("--stage-timeout", type=float, default=None, help="seconds; default no limit")
    args = p.parse_args()

    repos = read_repos(Path(args.repos))
    if args.only:
        wanted = [r.strip() for r in args.only.split(",")]
        missing = [r for r in wanted if r not in repos]
        if missing:
            raise SystemExit(f"not in {args.repos}: {missing}")
        repos = wanted
    stages = STAGES
    if args.stages:
        wanted = args.stages.split(",")
        unknown = set(wanted) - {s for s, _ in STAGES}
        if unknown:
            raise SystemExit(f"unknown stage(s): {sorted(unknown)}")
        stages = [s for s in STAGES if s[0] in wanted]

    say(f"run_all: {len(repos)} repo(s), stages {[s for s, _ in stages]}")
    t0 = time.time()
    ok, failed = [], []
    for repo in repos:
        try:
            (ok if run_repo(repo, stages, args.retries, args.stage_timeout) else failed).append(repo)
        except Exception as e:  # never let one repo stop the batch
            say(f"{repo:35s} runner error: {e!r}")
            record_failure({"time": now(), "repo": repo, "stage": "runner", "exit_code": "",
                            "seconds": "", "attempts": 1, "error": repr(e)[:300], "log": ""})
            failed.append(repo)
    say(f"run_all finished in {(time.time() - t0) / 60:.1f} min: {len(ok)} complete, {len(failed)} failed"
        + (f" ({', '.join(failed)}); see {FAILURES_CSV.relative_to(ROOT)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
