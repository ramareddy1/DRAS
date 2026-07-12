"""Concept graph loader.

Loads concepts.yaml once at import. Exposes:
  CONCEPTS         — dict[concept_id, Concept]
  ALIAS_INDEX      — dict[normalized_alias, concept_id]
  concepts_for_role(role)
  concept_by_id(id)

The graph is treated as read-only at runtime. Per-brand learned aliases live
in the memory layer (Phase 4), not here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_YAML_PATH = Path(__file__).parent / "concepts.yaml"


def _norm_alias(s: str) -> str:
    """Match the same normalization used during binding lookup."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass(frozen=True)
class ValueHints:
    regex: Optional[str] = None
    numeric_range: Optional[tuple] = None
    datetime: bool = False


@dataclass(frozen=True)
class Concept:
    id: str
    type: str
    role: str
    entity: str
    aliases: tuple
    value_hints: Optional[ValueHints] = None
    invariants: tuple = field(default_factory=tuple)


def _load() -> Dict[str, Concept]:
    raw = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))
    concepts: Dict[str, Concept] = {}
    for entry in raw.get("concepts", []):
        vh = entry.get("value_hints") or {}
        hints = ValueHints(
            regex=vh.get("regex"),
            numeric_range=tuple(vh["numeric_range"]) if vh.get("numeric_range") else None,
            datetime=bool(vh.get("datetime", False)),
        ) if vh else None
        c = Concept(
            id=entry["id"],
            type=entry["type"],
            role=entry["role"],
            entity=entry["entity"],
            aliases=tuple(entry.get("aliases", [])),
            value_hints=hints,
            invariants=tuple(entry.get("invariants", [])),
        )
        concepts[c.id] = c
    return concepts


CONCEPTS: Dict[str, Concept] = _load()

ALIAS_INDEX: Dict[str, str] = {}
for cid, c in CONCEPTS.items():
    # the concept id itself is also a self-alias, useful for "the user typed `order.id` directly"
    ALIAS_INDEX.setdefault(_norm_alias(cid), cid)
    for a in c.aliases:
        ALIAS_INDEX.setdefault(_norm_alias(a), cid)


def concept_by_id(cid: str) -> Optional[Concept]:
    return CONCEPTS.get(cid)


def concepts_for_role(role: str) -> List[Concept]:
    return [c for c in CONCEPTS.values() if c.role == role]


def concept_by_alias(text: str) -> Optional[Concept]:
    cid = ALIAS_INDEX.get(_norm_alias(text))
    return CONCEPTS.get(cid) if cid else None
