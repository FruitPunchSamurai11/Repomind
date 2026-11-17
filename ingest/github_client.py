"""Thin GitHub client (GraphQL + a little REST): auth, retries, rate-limit
backoff and multi-token rotation.

Tokens come from the environment only (loaded from .env here and nowhere
else): GITHUB_TOKENS=tok1,tok2,... and/or GITHUB_TOKEN=tok. When a token hits
its rate limit the client switches to the next one; it sleeps only when every
token is exhausted, until the earliest reset. Tokens are never logged,
printed, or written into any dataset; messages refer to them by position
("token 2/3") only.
"""
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"
MAX_SLEEP = 3700  # a primary rate-limit window is one hour


class GitHubAuthError(RuntimeError):
    pass


def load_tokens() -> list[str]:
    """GITHUB_TOKENS (comma-separated) first, then GITHUB_TOKEN; de-duplicated."""
    tokens = [t.strip() for t in os.environ.get("GITHUB_TOKENS", "").split(",") if t.strip()]
    single = os.environ.get("GITHUB_TOKEN", "").strip()
    if single:
        tokens.append(single)
    return [t for t in dict.fromkeys(tokens) if not t.startswith("paste_your")]


class TokenPool:
    """Round-robin over tokens; each token is blocked per resource
    (graphql / core / search) until its reset time."""

    def __init__(self, tokens: list[str], sleep=time.sleep, clock=time.time):
        if not tokens:
            raise GitHubAuthError(
                "No GitHub token found. Put GITHUB_TOKEN=<token> (or "
                "GITHUB_TOKENS=<tok1>,<tok2>) in .env (see .env.example)."
            )
        self.tokens = tokens
        self.index = 0
        self.blocked: dict[str, list[float]] = {}
        self._sleep = sleep
        self._clock = clock

    def label(self, i: int | None = None) -> str:
        i = self.index if i is None else i
        return f"token {i + 1}/{len(self.tokens)}"

    @property
    def current(self) -> str:
        return self.tokens[self.index]

    def exhaust(self, resource: str, reset_epoch: float):
        """Mark the current token as limited for `resource`, then move to the
        next usable token, sleeping if none is usable."""
        until = self.blocked.setdefault(resource, [0.0] * len(self.tokens))
        until[self.index] = max(reset_epoch, self._clock() + 1)
        now = self._clock()
        for step in range(1, len(self.tokens) + 1):
            j = (self.index + step) % len(self.tokens)
            if until[j] <= now:
                self.index = j
                print(f"[github] {resource} rate limit: switching to {self.label()}", flush=True)
                return
        j = min(range(len(self.tokens)), key=lambda k: until[k])
        wait = min(until[j] - now + 2, MAX_SLEEP)
        print(f"[github] all {len(self.tokens)} token(s) rate-limited for {resource}; "
              f"sleeping {wait:.0f}s until reset", flush=True)
        self._sleep(wait)
        self.index = j


def _parse_iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class GitHubGraphQLClient:
    def __init__(self, token: str | None = None, max_retries: int = 5,
                 tokens: list[str] | None = None, sleep=time.sleep):
        self.pool = TokenPool(tokens or ([token] if token else load_tokens()), sleep=sleep)
        self.max_retries = max_retries
        self.session = requests.Session()
        self._sleep = sleep

    @property
    def token(self) -> str:  # kept for backward compatibility
        return self.pool.current

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.pool.current}", "Content-Type": "application/json"}

    def _handle_limit(self, resp, resource: str) -> bool:
        """True if `resp` was a rate-limit response that we handled (caller retries)."""
        h = resp.headers
        if resp.status_code in (403, 429):
            if h.get("x-ratelimit-remaining") == "0":
                self.pool.exhaust(resource, float(h.get("x-ratelimit-reset", time.time() + 60)))
                return True
            if "retry-after" in h:  # secondary limit: back off on the same token
                self._sleep(min(float(h["retry-after"]) + 1, MAX_SLEEP))
                return True
        return False

    def _request(self, method: str, url: str, resource: str, **kw) -> requests.Response:
        last_error = None
        attempt = 0
        limit_hits = 0
        while attempt < self.max_retries:
            try:
                resp = self.session.request(method, url, headers=self._headers(), timeout=30, **kw)
            except requests.RequestException as e:  # network blip
                last_error = e
                attempt += 1
                self._sleep(min(2 ** attempt, 60))
                continue
            if resp.status_code == 401:
                raise GitHubAuthError(
                    f"GitHub rejected {self.pool.label()} (401). Check it is valid and not expired.")
            if self._handle_limit(resp, resource):
                limit_hits += 1
                if limit_hits > 10 * len(self.pool.tokens):
                    break
                continue
            if resp.status_code == 200:
                return resp
            last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            if resp.status_code in (404, 422):  # not retryable
                break
            attempt += 1
            self._sleep(min(2 ** attempt, 60))
        raise last_error or RuntimeError(f"GitHub request failed after retries: {url}")

    def query(self, query: str, variables: dict) -> dict:
        last_error = None
        for _ in range(self.max_retries):
            resp = self._request("POST", GRAPHQL_URL, "graphql",
                                 json={"query": query, "variables": variables})
            payload = resp.json()
            errors = payload.get("errors") or []
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                self.pool.exhaust("graphql", float(resp.headers.get("x-ratelimit-reset", time.time() + 60)))
                continue
            if errors and not payload.get("data"):
                last_error = RuntimeError(str(errors)[:500])
                self._sleep(2)
                continue
            data = payload["data"]
            rate = (data or {}).get("rateLimit")
            if rate and rate["remaining"] < 50:
                # proactive: rotate (or sleep) before the next call fails
                self.pool.exhaust("graphql", _parse_iso(rate["resetAt"]))
            return data
        raise last_error or RuntimeError("GitHub GraphQL query failed after retries")

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        """REST GET, e.g. get('/search/repositories', {...}). Returns the response."""
        resource = "search" if path.startswith("/search/") else "core"
        return self._request("GET", REST_URL + path, resource, params=params or {})
