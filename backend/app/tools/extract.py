"""Parse unstructured causal vocabulary into structured proposals.

This is the entry point for every form of free-text input the product
accepts:

  - Onboarding intake answers ("Which systems do you reconcile across?")
  - Drop-in notes ("We just switched to net-45 with Acme starting Nov 1.")
  - Per-decision justifications ("Stripe std fees" when marking a row expected)

The tool extracts three categories of proposals, each of which a Phase 5 UI
will surface for user confirmation before it's written to the account's
memory:

  1. alias_proposals     — "Total Price" -> order.gross_total
  2. rule_proposals      — "Acme invoices are net-45" -> expected_late
  3. brand_facts         — "we use 3PL Acme for fulfillment"

Nothing is auto-applied. Every proposal carries `user_origin_text` (the
verbatim source) so the system can replay the user's own words later.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..llm import LLMUnavailable, call_claude_json, is_configured
from ..ontology import CONCEPTS


def _ontology_summary_for_prompt() -> str:
    """Compact concept list the LLM can ground extractions in."""
    lines: List[str] = []
    for cid, c in CONCEPTS.items():
        aliases = ", ".join(list(c.aliases)[:5])
        lines.append(f"  - {cid} ({c.type}, role={c.role}): aliases={aliases}")
    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = (
    "You are extracting structured proposals from an e-commerce operator's "
    "free-text notes about their business. The operator is teaching the "
    "reconciliation system about idiosyncrasies that don't appear in their "
    "raw data — fee rates, vendor terms, expected unmatched records, custom "
    "ID formats, etc.\n\n"
    "Three categories to extract:\n"
    "  1. alias_proposals  — when the user names a column or field that "
    "should map to one of the known ontology concepts below.\n"
    "  2. rule_proposals   — when the user describes a recurring exception "
    "the system should remember (custom fee rate, expected unmatched, late "
    "invoice convention).\n"
    "  3. brand_facts      — durable facts about the business (who their 3PL "
    "is, what processors they use, what currency, when they switched providers).\n\n"
    "Known ontology concepts:\n"
    "{ontology}\n\n"
    "Respond with JSON only, this exact shape:\n"
    "{{\n"
    '  "alias_proposals":  [{{"text": str, "concept_id": str, "confidence": 0..1}}],\n'
    '  "rule_proposals":   [{{"description": str, "type": str, "confidence": 0..1}}],\n'
    '  "brand_facts":      [{{"fact": str, "category": str, "confidence": 0..1}}]\n'
    "}}\n\n"
    "Rules:\n"
    "  - Only extract concept_ids from the ontology list above. If unsure, omit.\n"
    "  - Set confidence < 0.7 when the inference is plausible but not explicit.\n"
    "  - Empty lists are fine. Quality over quantity.\n"
    "  - Do not add commentary outside the JSON."
)


def extract_from_text(
    *,
    text: str,
    account_id: str,
    context_kind: str = "note",      # "intake" | "note" | "justification"
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract proposals from one piece of unstructured input.

    Returns:
      {
        "source_text": str,
        "context_kind": str,
        "alias_proposals":  [...],
        "rule_proposals":   [...],
        "brand_facts":      [...],
        "extracted": bool,                # False if LLM unavailable
        "error": str | None,
      }

    Never raises; LLM failures degrade to an empty extraction with `error`
    populated. The caller (Phase 5 endpoints) decides whether to surface
    that to the user.
    """
    text = (text or "").strip()
    if not text:
        return _empty(text, context_kind, extracted=False, error="empty input")

    if not is_configured():
        return _empty(text, context_kind, extracted=False,
                      error="LLM unavailable: ANTHROPIC_API_KEY not configured")

    system = SYSTEM_PROMPT_TEMPLATE.format(ontology=_ontology_summary_for_prompt())
    user_payload = json.dumps({
        "context_kind": context_kind,
        "text": text,
    })

    try:
        data = call_claude_json(
            tool_name="extract_from_text",
            account_id=account_id, job_id=job_id,
            system=system,
            messages=[{"role": "user", "content": user_payload}],
            max_tokens=800,
        )
    except LLMUnavailable as e:
        return _empty(text, context_kind, extracted=False, error=str(e))
    except Exception as e:
        return _empty(text, context_kind, extracted=False,
                      error=f"{type(e).__name__}: {e}")

    return {
        "source_text": text,
        "context_kind": context_kind,
        "alias_proposals":  _sanitize_aliases(data.get("alias_proposals") or []),
        "rule_proposals":   _sanitize_rules(data.get("rule_proposals") or []),
        "brand_facts":      _sanitize_facts(data.get("brand_facts") or []),
        "extracted": True,
        "error": None,
    }


def _empty(text: str, kind: str, *, extracted: bool, error: Optional[str]) -> Dict[str, Any]:
    return {
        "source_text": text,
        "context_kind": kind,
        "alias_proposals": [],
        "rule_proposals": [],
        "brand_facts": [],
        "extracted": extracted,
        "error": error,
    }


def _sanitize_aliases(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for it in items:
        cid = (it.get("concept_id") or "").strip()
        if cid not in CONCEPTS:
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text[:200],
            "concept_id": cid,
            "confidence": _clamp_conf(it.get("confidence")),
        })
    return out


def _sanitize_rules(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for it in items:
        desc = (it.get("description") or "").strip()
        if not desc:
            continue
        out.append({
            "description": desc[:500],
            "type": (it.get("type") or "custom").strip()[:60],
            "confidence": _clamp_conf(it.get("confidence")),
        })
    return out


def _sanitize_facts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for it in items:
        fact = (it.get("fact") or "").strip()
        if not fact:
            continue
        out.append({
            "fact": fact[:500],
            "category": (it.get("category") or "general").strip()[:60],
            "confidence": _clamp_conf(it.get("confidence")),
        })
    return out


def _clamp_conf(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))
