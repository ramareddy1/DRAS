"""Anthropic client wrapper + per-call usage logging.

Single chokepoint for every LLM call in the system. The agent and all tools
call `call_claude(...)` here so we always log usage to
`data/llm_usage.jsonl`. Phase 5 surfaces aggregate numbers from this file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
USAGE_LOG_PATH = Path(os.getenv("RECONOPS_DATA_DIR", "data")) / "llm_usage.jsonl"


class LLMUnavailable(Exception):
    """Raised when no API key is configured. The agent treats this as fatal —
    no template fallback in v3."""


def _have_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def is_configured() -> bool:
    return _have_key()


def _log_usage(record: Dict[str, Any]) -> None:
    try:
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def call_claude(
    *,
    tool_name: str,
    account_id: str,
    job_id: Optional[str],
    system: str,
    messages: List[Dict[str, Any]],
    max_tokens: int = 1024,
    model: Optional[str] = None,
) -> str:
    """Make one Anthropic call and log usage. Returns plain text.

    Raises LLMUnavailable if no API key is configured — callers should let
    this propagate; the agent turns it into HTTP 503 at the boundary.
    """
    if not _have_key():
        raise LLMUnavailable(
            "AI service is currently unavailable: ANTHROPIC_API_KEY not configured."
        )

    # Test-only stub. Gated entirely on env var; no impact on real runs.
    if os.getenv("RECONOPS_STUB_LLM") == "1":
        return _stub_response(tool_name=tool_name, account_id=account_id, job_id=job_id,
                              system=system, messages=messages, model=model)

    from anthropic import Anthropic
    client = Anthropic()  # picks up key from env

    used_model = model or DEFAULT_MODEL
    started = time.time()
    try:
        msg = client.messages.create(
            model=used_model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
    except Exception as e:
        _log_usage({
            "ts": time.time(),
            "tool": tool_name, "account_id": account_id, "job_id": job_id,
            "model": used_model, "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - started) * 1000),
        })
        raise

    latency_ms = int((time.time() - started) * 1000)
    in_tokens = getattr(msg.usage, "input_tokens", None) if hasattr(msg, "usage") else None
    out_tokens = getattr(msg.usage, "output_tokens", None) if hasattr(msg, "usage") else None
    _log_usage({
        "ts": time.time(),
        "tool": tool_name, "account_id": account_id, "job_id": job_id,
        "model": used_model,
        "input_tokens": in_tokens, "output_tokens": out_tokens,
        "latency_ms": latency_ms,
    })
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def call_claude_json(
    *,
    tool_name: str,
    account_id: str,
    job_id: Optional[str],
    system: str,
    messages: List[Dict[str, Any]],
    max_tokens: int = 1024,
    model: Optional[str] = None,
) -> Any:
    """Like call_claude but expects JSON. Returns parsed Python or raises."""
    text = call_claude(
        tool_name=tool_name, account_id=account_id, job_id=job_id,
        system=system, messages=messages, max_tokens=max_tokens, model=model,
    )
    # Permissive JSON extraction — accept text with code fences or surrounding prose.
    s = text.strip()
    if s.startswith("```"):
        # strip ```json … ``` style fences
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Last resort: try to find the first { … } block
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start:end + 1])
        raise


def _stub_response(*, tool_name, account_id, job_id, system, messages, model) -> str:
    """Deterministic offline response for tests / local dev when
    RECONOPS_STUB_LLM=1. Records a synthetic usage line so the audit pipeline
    is also exercised."""
    sysprompt = (system or "").lower()
    if "operations analyst" in sysprompt:
        text = (
            "**Overall match quality: excellent.** Most rows matched cleanly. "
            "16 fee-pattern discrepancies look like processor fees, not losses.\n"
            "**Top patterns:** Stripe fee offsets dominate; a handful of unmatched "
            "in both directions.\n"
            "**Suggested actions:** investigate the largest unmatched orders; "
            "confirm processor rate; reconcile manual charges."
        )
    elif tool_name == "extract_from_text":
        text = '{"alias_proposals": [], "rule_proposals": [], "brand_facts": []}'
    else:
        # propose_classification fallback
        text = '{"status": "minor", "confidence": 0.65, "reason": "stubbed second opinion"}'
    _log_usage({
        "ts": time.time(),
        "tool": tool_name, "account_id": account_id, "job_id": job_id,
        "model": (model or DEFAULT_MODEL) + " (stub)",
        "input_tokens": 0, "output_tokens": 0, "latency_ms": 0,
        "stub": True,
    })
    return text


def require_llm_or_503():
    """Boundary helper used by FastAPI routes."""
    if not _have_key():
        raise HTTPException(
            status_code=503,
            detail="AI service is currently unavailable: ANTHROPIC_API_KEY not configured.",
        )
