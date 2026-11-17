"""Offline tests for Revision-round-2 tooling: token rotation, the repo
sampler's filters, and the batch runner's resume/failure behaviour."""
import random
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from evaluation import run_all, sample_repos
from ingest.github_client import GitHubAuthError, GitHubGraphQLClient, TokenPool, load_tokens


# ---------- token pool ----------

def test_load_tokens_combines_and_dedupes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKENS", "a, b,,a")
    monkeypatch.setenv("GITHUB_TOKEN", "b")
    assert load_tokens() == ["a", "b"]
    monkeypatch.setenv("GITHUB_TOKENS", "")
    monkeypatch.setenv("GITHUB_TOKEN", "paste_your_token_here")
    assert load_tokens() == []


def test_pool_requires_a_token():
    with pytest.raises(GitHubAuthError):
        TokenPool([])


def test_pool_rotates_then_sleeps_until_earliest_reset():
    clock = [1000.0]
    slept = []
    pool = TokenPool(["t1", "t2"], sleep=lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)),
                     clock=lambda: clock[0])
    pool.exhaust("graphql", 1500)
    assert pool.current == "t2" and not slept
    pool.exhaust("graphql", 1200)            # both blocked -> sleep until t2's reset (earliest)
    assert slept and 200 <= slept[0] <= 205 and pool.current == "t2"
    pool.exhaust("search", 1100)             # resources are tracked separately
    assert pool.current == "t1"


def test_pool_label_never_contains_token():
    pool = TokenPool(["ghp_secret"])
    assert "secret" not in pool.label()


class FakeResp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers, self.text = status, payload or {}, headers or {}, ""

    def json(self):
        return self._payload


def test_client_rotates_on_403_rate_limit():
    client = GitHubGraphQLClient(tokens=["t1", "t2"], sleep=lambda s: None)
    used = []
    responses = [FakeResp(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}),
                 FakeResp(200, {"data": {"ok": 1}})]

    def fake_request(method, url, headers, **kw):
        used.append(headers["Authorization"])
        return responses.pop(0)

    client.session = SimpleNamespace(request=fake_request)
    assert client.query("q", {}) == {"ok": 1}
    assert used == ["Bearer t1", "Bearer t2"]


# ---------- sampler ----------

def test_months_and_month_range():
    assert sample_repos.months(date(2021, 11, 1), date(2022, 2, 3)) == ["2021-11", "2021-12", "2022-01", "2022-02"]
    assert sample_repos.month_range("2020-02") == "2020-02-01..2020-02-29"


def test_doc_reason_flags_lists_but_not_code():
    assert sample_repos.doc_reason({"name": "awesome-go", "description": "", "topics": []})
    assert sample_repos.doc_reason({"name": "x", "description": "A curated list of things", "topics": []})
    assert sample_repos.doc_reason({"name": "notebook-tool", "description": "Fast notebook runner", "topics": []}) is None


def _candidate(**over):
    repo = {"isMirror": False, "hasIssuesEnabled": True, "createdAt": "2015-01-01T00:00:00Z",
            "stargazerCount": 900, "pullRequests": {"totalCount": 300},
            "languages": {"totalSize": 100, "edges": [{"size": 90, "node": {"name": "Go"}}]},
            "defaultBranchRef": {"target": {"all": {"totalCount": 1200}, "old": {"totalCount": 1}}}}
    repo.update(over)
    return repo


class FakeClient:
    def __init__(self, repo, linked=80):
        self.repo, self.linked = repo, linked

    def query(self, q, v):
        return {"repository": self.repo}

    def get(self, path, params):
        return FakeResp(200, {"total_count": self.linked})


def _sampler(client):
    return sample_repos.Sampler(client, 1, date(2026, 11, 17), 5, 3)


ITEM = {"full_name": "acme/widget", "name": "widget", "description": "", "topics": [], "stargazers_count": 900}


def test_examine_accepts_good_candidate():
    reason, facts = _sampler(FakeClient(_candidate())).examine(ITEM, "go")
    assert reason is None and facts["commits"] == 1200 and facts["linked"] == 80


@pytest.mark.parametrize("over,linked,expect", [
    ({"defaultBranchRef": {"target": {"all": {"totalCount": 9000}, "old": {"totalCount": 1}}}}, 80, "commits"),
    ({"defaultBranchRef": {"target": {"all": {"totalCount": 900}, "old": {"totalCount": 0}}}}, 80, "history"),
    ({"languages": {"totalSize": 100, "edges": [{"size": 20, "node": {"name": "Go"}}]}}, 80, "of code"),
    ({"isMirror": True}, 80, "mirror"),
    ({}, 10, "linked"),
])
def test_examine_rejections(over, linked, expect):
    reason, _ = _sampler(FakeClient(_candidate(**over), linked)).examine(ITEM, "go")
    assert reason and expect in reason


def test_examine_rejects_originals_and_name_collisions():
    s = _sampler(FakeClient(_candidate()))
    assert "original" in s.examine({**ITEM, "full_name": "pallets/flask", "name": "flask"}, "python")[0]
    assert "collides" in s.examine({**ITEM, "full_name": "someone/flask", "name": "flask"}, "python")[0]


# ---------- batch runner ----------

def test_read_repos_skips_comments_and_rejects_name_clash(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("# header\na/x  # original\n\nb/y\n", encoding="utf-8")
    assert run_all.read_repos(f) == ["a/x", "b/y"]
    f.write_text("a/x\nb/X\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_all.read_repos(f)


@pytest.fixture
def runner_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_all, "MARKER_DIR", tmp_path / "markers")
    monkeypatch.setattr(run_all, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(run_all, "FAILURES_CSV", tmp_path / "failures.csv")
    monkeypatch.setattr(run_all, "ROOT", tmp_path)
    monkeypatch.setattr(run_all.time, "sleep", lambda s: None)
    return tmp_path


def _fake_stages(monkeypatch, fail: set):
    calls = []

    def fake_run(cmd, **kw):
        stage = cmd[2]
        calls.append(stage)
        kw["stdout"].write("boom: something broke\n" if stage in fail else "fine\n")
        kw["stdout"].flush()
        return SimpleNamespace(returncode=1 if stage in fail else 0)

    monkeypatch.setattr(run_all.subprocess, "run", fake_run)
    stages = [("s1", "s1"), ("s2", "s2"), ("s3", "s3")]
    monkeypatch.setattr(run_all, "STAGES", stages)
    return calls, stages


def test_runner_records_failure_and_resumes(runner_dirs, monkeypatch):
    calls, stages = _fake_stages(monkeypatch, fail={"s2"})
    assert run_all.run_repo("o/r", stages, retries=1, timeout=None) is False
    assert calls == ["s1", "s2", "s2"]                      # s2 retried once, s3 never run
    text = (runner_dirs / "failures.csv").read_text(encoding="utf-8")
    assert "o/r,s2,1" in text and "boom: something broke" in text

    calls2, _ = _fake_stages(monkeypatch, fail=set())        # next run: s1 skipped, resumes at s2
    assert run_all.run_repo("o/r", stages, retries=1, timeout=None) is True
    assert calls2 == ["s2", "s3"]


def test_rerunning_a_stage_invalidates_later_markers(runner_dirs, monkeypatch):
    calls, stages = _fake_stages(monkeypatch, fail=set())
    run_all.run_repo("o/r", stages, 0, None)
    run_all.marker("o/r", "s2").unlink()
    calls.clear()
    run_all.run_repo("o/r", stages, 0, None)
    assert calls == ["s2", "s3"]
