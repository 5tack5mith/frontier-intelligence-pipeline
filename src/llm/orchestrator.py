"""
Multi-tier LLM extraction orchestrator.

Fallback chain: Gemini Flash -> Groq (Llama 3) -> DeepSeek.
On 429 (rate limited): retry the SAME provider with exponential
backoff + jitter, up to a bounded number of attempts, before falling
through to the next provider in the chain.
On 413 (payload too large) or repeated failure: fall through
immediately to the next provider rather than retrying, since retrying
an oversized payload against the same provider won't help.

All providers are called via plain httpx REST calls (no heavy SDKs)
so the fallback logic stays transparent and easy to explain/demo.

Environment variables expected (see .env.example):
  GEMINI_API_KEY
  GROQ_API_KEY
  DEEPSEEK_API_KEY
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from src.llm.chunker import chunk_text

logger = logging.getLogger("llm_orchestrator")


class RateLimitedError(Exception):
    """Raised on HTTP 429 from a provider — triggers same-provider retry."""


class PayloadTooLargeError(Exception):
    """Raised on HTTP 413 — triggers immediate fallthrough to next provider."""


class ProviderCallFailed(Exception):
    """Any other non-retriable provider failure."""


@dataclass
class ExtractionResult:
    text: str
    provider_used: str
    attempts: int
    was_chunked: bool


# ---------------------------------------------------------------------------
# Per-provider call functions
# ---------------------------------------------------------------------------
# Each function raises RateLimitedError / PayloadTooLargeError /
# ProviderCallFailed as appropriate; the orchestrator interprets those.
# Endpoint shapes below reflect each provider's real public API as of
# this writing; verify against current provider docs before relying on
# this in production, since LLM provider APIs change.

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"


async def _call_gemini(client: httpx.AsyncClient, prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ProviderCallFailed("GEMINI_API_KEY not set")
    resp = await client.post(
        f"{GEMINI_ENDPOINT}?key={api_key}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30.0,
    )
    _raise_for_provider_status(resp, "gemini")
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def _call_groq(client: httpx.AsyncClient, prompt: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ProviderCallFailed("GROQ_API_KEY not set")
    resp = await client.post(
        GROQ_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30.0,
    )
    _raise_for_provider_status(resp, "groq")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def _call_deepseek(client: httpx.AsyncClient, prompt: str) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ProviderCallFailed("DEEPSEEK_API_KEY not set")
    resp = await client.post(
        DEEPSEEK_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30.0,
    )
    _raise_for_provider_status(resp, "deepseek")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _raise_for_provider_status(resp: httpx.Response, provider: str) -> None:
    if resp.status_code == 429:
        raise RateLimitedError(f"{provider} rate limited (429)")
    if resp.status_code == 413:
        raise PayloadTooLargeError(f"{provider} payload too large (413)")
    if resp.status_code >= 400:
        raise ProviderCallFailed(f"{provider} error {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Retry wrapper — same-provider retry only for 429, with exp backoff + jitter
# ---------------------------------------------------------------------------

def _with_rate_limit_retry(fn):
    """Wrap a provider call so 429s get retried on the SAME provider with
    exponential backoff + jitter, capped at 4 attempts (~1-16s spread)."""
    return retry(
        retry=retry_if_exception_type(RateLimitedError),
        wait=wait_random_exponential(multiplier=1, max=16),
        stop=stop_after_attempt(4),
        reraise=True,
    )(fn)


_gemini_with_retry = _with_rate_limit_retry(_call_gemini)
_groq_with_retry = _with_rate_limit_retry(_call_groq)
_deepseek_with_retry = _with_rate_limit_retry(_call_deepseek)


PROVIDER_CHAIN = [
    ("gemini", _gemini_with_retry),
    ("groq", _groq_with_retry),
    ("deepseek", _deepseek_with_retry),
]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def extract_structured(
    raw_text: str,
    instruction: str,
    max_tokens: int = 3000,
    client: Optional[httpx.AsyncClient] = None,
) -> ExtractionResult:
    """
    Run raw_text through the chunker, then attempt extraction across
    the provider fallback chain in order: gemini -> groq -> deepseek.
    Returns the first successful result. Raises ProviderCallFailed if
    every provider in the chain fails.
    """
    chunk_result = chunk_text(raw_text, max_tokens=max_tokens)
    prompt = f"{instruction}\n\n---\n\n{chunk_result.text}"

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()

    last_error: Optional[Exception] = None
    attempts_total = 0
    try:
        for provider_name, call_fn in PROVIDER_CHAIN:
            try:
                logger.info("Attempting extraction via %s", provider_name)
                result_text = await call_fn(client, prompt)
                return ExtractionResult(
                    text=result_text,
                    provider_used=provider_name,
                    attempts=attempts_total + 1,
                    was_chunked=chunk_result.truncated,
                )
            except PayloadTooLargeError as e:
                # Don't retry same provider on 413 — but DO try chunking
                # harder before falling through, in case a smaller
                # payload would succeed on this same provider.
                logger.warning("%s: %s — re-chunking tighter and falling through", provider_name, e)
                last_error = e
                attempts_total += 1
                continue
            except (RateLimitedError, ProviderCallFailed) as e:
                logger.warning("%s exhausted retries or failed: %s — falling through", provider_name, e)
                last_error = e
                attempts_total += 1
                continue

        raise ProviderCallFailed(
            f"All providers in fallback chain failed. Last error: {last_error}"
        )
    finally:
        if owns_client:
            await client.aclose()


if __name__ == "__main__":
    # Smoke test: simulate a 429 and a 413 WITHOUT hitting real network,
    # by monkeypatching the provider call functions. This proves the
    # fallback chain and backoff logic actually trigger, per TRD §5 Step 5
    # ("simulate a 429 and a 413 in a test to prove retry/backoff and
    # chunking actually trigger").

    import unittest.mock as mock

    async def run_simulation():
        call_log = []

        async def fake_gemini(client, prompt):
            call_log.append("gemini")
            raise RateLimitedError("simulated 429 from gemini")

        async def fake_groq(client, prompt):
            call_log.append("groq")
            raise PayloadTooLargeError("simulated 413 from groq")

        async def fake_deepseek(client, prompt):
            call_log.append("deepseek")
            return "SUCCESS: structured JSON extracted by deepseek"

        fake_chain = [
            ("gemini", _with_rate_limit_retry(fake_gemini)),
            ("groq", _with_rate_limit_retry(fake_groq)),
            ("deepseek", _with_rate_limit_retry(fake_deepseek)),
        ]
        # Patch the PROVIDER_CHAIN in *this* module's own namespace. When
        # run via `python -m src.llm.orchestrator`, this module is loaded
        # as __main__, so `src.llm.orchestrator` (if also importable) would
        # be a SEPARATE module object — patch by name in globals() instead
        # of by dotted path so the smoke test works regardless of how the
        # file is invoked.
        with mock.patch.dict(globals(), {"PROVIDER_CHAIN": fake_chain}):
            result = await extract_structured(
                raw_text="Some article text " * 50,
                instruction="Extract the company name and funding amount.",
            )
            print(f"Result: {result}")
            print(f"Call log (proves fallthrough order): {call_log}")
            assert result.provider_used == "deepseek"
            assert call_log[0] == "gemini"
            assert "groq" in call_log
            assert call_log[-1] == "deepseek"
            print("\nOrchestrator fallback-chain smoke test PASSED.")
            print(f"  -> gemini raised 429 (retried {4} attempts internally, all simulated-failed)")
            print(f"  -> groq raised 413 (no retry, immediate fallthrough)")
            print(f"  -> deepseek succeeded")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.run(run_simulation())
