"""Rule-proposal heuristic.

After every job, scan the decision log: when the same
`(signature, user_status)` pair has been logged at least `MIN_OCCURRENCES`
times AND there isn't already a rule covering it, write a `pending` rule
to `rules.json`. The Phase 5 UI surfaces pending rules for Accept / Edit
/ Reject on `/rules`.

`user_origin_text` on the proposed rule is taken from the most recent
decision entry — so the rule reads as the user's own words.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from ..models import Rule
from . import decision_log, rules_store

MIN_OCCURRENCES = 3


def _signature_for_existing_rule(rule: Rule) -> str:
    return (rule.when.get("signature_prefix")
            or rule.when.get("signature")
            or "")


def _existing_signatures(account_id: str) -> set:
    return {_signature_for_existing_rule(r) for r in rules_store.load_rules(account_id)
            if r.state in ("active", "pending")}


def propose_from_decisions(account_id: str) -> List[Rule]:
    """Inspect the decision log; return any new pending rules written."""
    entries = list(decision_log.replay(account_id))
    if not entries:
        return []

    counts: Counter = Counter()
    latest_reason: Dict[Tuple[str, str], str] = {}
    latest_status: Dict[Tuple[str, str], Optional[str]] = {}
    for e in entries:
        if not e.signature or not e.user_status:
            continue
        key = (e.signature, e.user_status)
        counts[key] += 1
        if e.user_reason:
            latest_reason[key] = e.user_reason
        latest_status[key] = e.user_status

    existing = _existing_signatures(account_id)
    created: List[Rule] = []

    for (sig, user_status), n in counts.items():
        if n < MIN_OCCURRENCES:
            continue
        if sig in existing:
            continue
        rule = Rule(
            account_id=account_id,
            kind="force_status",
            description=(
                f"User marked {n} rows with signature {sig[:8]}… as "
                f"'{user_status}'."
            ),
            when={"signature_prefix": sig},
            then={"status": user_status if user_status in
                  ("match", "minor", "major", "fee_offset") else "match"},
            origin=f"user_confirmed_{n}x",
            user_origin_text=latest_reason.get((sig, user_status)),
            confidence=min(0.95, 0.6 + 0.05 * n),
            state="pending",
        )
        rules_store.add_rule(account_id, rule)
        created.append(rule)
    return created
