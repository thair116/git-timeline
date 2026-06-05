"""Anthropic client wrapper with content-hash caching and cost tracking.

Cache is keyed on (stage, sha256(model + system + user)), so any prompt change
invalidates — which is what we want while iterating on wording. Tweak prompts
freely; cache only ever saves tokens when the *input* is byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import anthropic

# Pricing per million tokens ($). Keep in sync with Anthropic pricing page.
# These are the defaults as of 2026-04; adjust if the API returns a different
# model or pricing changes.
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_m, output_per_m)
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5":          (1.00, 5.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-opus-4-7":           (15.00, 75.00),
    "claude-opus-4-8":           (15.00, 75.00),
}

# Exit code a stage returns when it has queued prompts for the in-session agent
# to answer. The orchestrating skill watches for this code.
INSESSION_EXIT = 3


def _price(model: str, in_tok: int, out_tok: int) -> float:
    pi, po = PRICING.get(model, (3.00, 15.00))  # Sonnet as a safe default
    return (in_tok / 1_000_000) * pi + (out_tok / 1_000_000) * po


def _hash(model: str, system: str, user: str, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(system.encode())
    h.update(b"\x00")
    h.update(user.encode())
    h.update(b"\x00")
    h.update(extra.encode())
    return h.hexdigest()


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float
    cached: bool


class LLMClient:
    """Anthropic client with SQLite-backed content-hash cache."""

    def __init__(self, conn: sqlite3.Connection, *, api_key: str | None = None):
        self.conn = conn
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    def call(
        self,
        *,
        stage: str,
        key: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float | None = 0.0,
        extra_cache: str = "",
    ) -> LLMResult:
        """Make a single-turn call with cache check. Returns LLMResult."""
        input_hash = _hash(model, system, user, extra_cache)

        cached = self.conn.execute(
            "SELECT model, input_tokens, output_tokens, response "
            "FROM llm_cache WHERE stage = ? AND input_hash = ?",
            (stage, input_hash),
        ).fetchone()

        if cached:
            return LLMResult(
                text=cached["response"],
                model=cached["model"],
                input_tokens=cached["input_tokens"],
                output_tokens=cached["output_tokens"],
                cost=0.0,
                cached=True,
            )

        # Retry on transient errors.
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        last_exc = None
        for attempt in range(4):
            try:
                resp = self.client.messages.create(**kwargs)
                break
            except (
                anthropic.APIConnectionError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            ) as e:
                last_exc = e
                time.sleep(2**attempt)
        else:
            raise RuntimeError(f"LLM call failed after retries: {last_exc}")

        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        )
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cost = _price(model, in_tok, out_tok)

        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache"
            "(stage, input_hash, key, model, input_tokens, output_tokens, response) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stage, input_hash, key, model, in_tok, out_tok, text),
        )
        self.conn.commit()

        return LLMResult(
            text=text,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost=cost,
            cached=False,
        )

    def call_json(self, **kwargs) -> tuple[dict[str, Any], LLMResult]:
        """Same as call(), but parse the response as JSON.

        Expects the model to return a JSON object. Strips common fencing.
        """
        result = self.call(**kwargs)
        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        text = text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"non-JSON LLM response for {kwargs.get('key')}: {e}\n{text[:300]}")
        return parsed, result


class PendingAnswer(Exception):
    """Raised by InSessionClient on a cache miss.

    The prompt has been queued for the in-session Claude Code agent to answer.
    Stages catch this to collect prompts across a run rather than crash.
    """

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def in_session_mode() -> bool:
    """True when LLM work should be done by the in-session Claude Code agent
    instead of the Anthropic API.

    Active when GIT_TIMELINE_INSESSION is truthy, OR when no ANTHROPIC_API_KEY
    is set (graceful fallback so the skill works with zero configuration).
    Set GIT_TIMELINE_INSESSION=0 to force the API path to error loudly if the
    key is missing.
    """
    flag = os.environ.get("GIT_TIMELINE_INSESSION", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return not os.environ.get("ANTHROPIC_API_KEY")


class InSessionClient:
    """Drop-in for LLMClient that defers inference to the Claude Code agent.

    On a cache hit it behaves exactly like LLMClient. On a miss it records the
    prompt and raises PendingAnswer; the stage collects these, writes them to a
    prompts file, and exits with INSESSION_EXIT. The agent answers each prompt,
    the answers are ingested back into llm_cache, and the stage is re-run — now
    every call is a cache hit and the stage completes normally.
    """

    backend = "insession"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.pending: list[dict] = []
        self._seen: set[str] = set()

    def _cache_get(self, stage: str, input_hash: str):
        return self.conn.execute(
            "SELECT model, input_tokens, output_tokens, response "
            "FROM llm_cache WHERE stage = ? AND input_hash = ?",
            (stage, input_hash),
        ).fetchone()

    def call(
        self,
        *,
        stage: str,
        key: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra_cache: str = "",
    ) -> LLMResult:
        input_hash = _hash(model, system, user, extra_cache)
        cached = self._cache_get(stage, input_hash)
        if cached:
            return LLMResult(
                text=cached["response"],
                model=cached["model"],
                input_tokens=cached["input_tokens"],
                output_tokens=cached["output_tokens"],
                cost=0.0,
                cached=True,
            )
        if input_hash not in self._seen:
            self._seen.add(input_hash)
            self.pending.append({
                "stage": stage,
                "key": key,
                "input_hash": input_hash,
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "user": user,
            })
        raise PendingAnswer(key)

    def call_json(self, **kwargs) -> tuple[dict[str, Any], LLMResult]:
        # Miss raises PendingAnswer before we reach parsing; hit returns text.
        result = self.call(**kwargs)
        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        text = text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"non-JSON in-session answer for {kwargs.get('key')}: {e}\n{text[:300]}"
            )
        return parsed, result

    def ingest_answers(self, repo_name: str, stage: str) -> int:
        """Load any answers the agent has written into llm_cache.

        Joins <repo>.<stage>.answers.jsonl (key -> response) with the prompts
        file (key -> input_hash, model) so the cache lands in the exact slot the
        stage will look up on its next run. Removes both files once consumed.
        Returns the number of answers ingested.
        """
        from . import paths

        pend_p = paths.pending_path(repo_name, stage)
        ans_p = paths.answers_path(repo_name, stage)
        if not ans_p.exists() or not pend_p.exists():
            return 0

        manifest: dict[str, dict] = {}
        for line in pend_p.read_text().splitlines():
            line = line.strip()
            if line:
                rec = json.loads(line)
                manifest[rec["key"]] = rec

        n = 0
        for line in ans_p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            ans = json.loads(line)
            rec = manifest.get(ans["key"])
            if not rec:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO llm_cache"
                "(stage, input_hash, key, model, input_tokens, output_tokens, response) "
                "VALUES (?, ?, ?, ?, 0, 0, ?)",
                (rec["stage"], rec["input_hash"], rec["key"], rec["model"],
                 ans["response"]),
            )
            n += 1
        self.conn.commit()
        if n:
            pend_p.unlink(missing_ok=True)
            ans_p.unlink(missing_ok=True)
        return n

    def finish_pending(self, repo_name: str, stage: str) -> int:
        """Write queued prompts to the prompts file, print agent instructions,
        and return INSESSION_EXIT for the stage's main() to return."""
        from . import paths

        pend_p = paths.pending_path(repo_name, stage)
        ans_p = paths.answers_path(repo_name, stage)
        lines = [json.dumps(p, ensure_ascii=False) for p in self.pending]
        pend_p.write_text("\n".join(lines) + ("\n" if lines else ""))

        print()
        print(f"[in-session] {len(self.pending)} prompt(s) need answers "
              f"for stage '{stage}'.")
        print(f"  prompts file : {pend_p}")
        print(f"  answers file : {ans_p}")
        print("  Each prompt line is JSON with: key, model, max_tokens, system, user.")
        print("  Acting as the model described by 'system', answer 'user' for each,")
        print("  then append one JSON object per line to the answers file:")
        print('    {"key": "<same key>", "response": "<the model output>"}')
        print("  Re-run this exact stage afterward; answers are ingested automatically.")
        return INSESSION_EXIT


def make_client(conn: sqlite3.Connection, *, force_insession: bool | None = None):
    """Return the right LLM backend for the current environment.

    InSessionClient when in_session_mode() (no API key, or opted in); otherwise
    the API-backed LLMClient. Pass force_insession to override the env check.
    """
    use = in_session_mode() if force_insession is None else force_insession
    return InSessionClient(conn) if use else LLMClient(conn)


def spend_report(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT stage, model, COUNT(*) n, SUM(input_tokens) in_t, "
        "SUM(output_tokens) out_t FROM llm_cache GROUP BY stage, model"
    ).fetchall()
    totals = {"total_cost": 0.0, "stages": []}
    for r in rows:
        cost = _price(r["model"], r["in_t"], r["out_t"])
        totals["stages"].append({
            "stage": r["stage"],
            "model": r["model"],
            "n": r["n"],
            "input_tokens": r["in_t"],
            "output_tokens": r["out_t"],
            "cost": cost,
        })
        totals["total_cost"] += cost
    return totals
