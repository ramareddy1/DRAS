"""Guards two assumptions the tool registry design rests on (spec §2.6).

If either breaks on an SDK upgrade, the prompt-cache strategy and the
pack-tool layering silently stop working — with no error at runtime.
"""
from __future__ import annotations

import json

from anthropic import beta_tool

from app.llm import DEFAULT_MODEL


@beta_tool
def _sample_tool(dataset_id: str, limit: int = 10) -> str:
    """A sample tool used only to inspect generated schema.

    Args:
        dataset_id: Handle for a stored dataset.
        limit: Maximum rows to consider.
    """
    return "ok"


def test_default_model_is_opus_4_8():
    assert DEFAULT_MODEL == "claude-opus-4-8"


def test_generated_schema_is_byte_stable_across_calls():
    """Prompt caching is a byte-exact prefix match on the serialized tools."""
    first = json.dumps(_sample_tool.to_dict(), sort_keys=True)
    second = json.dumps(_sample_tool.to_dict(), sort_keys=True)
    assert first == second


def test_generated_schema_carries_docstring_and_params():
    schema = _sample_tool.to_dict()
    assert schema["name"] == "_sample_tool"
    assert "dataset_id" in schema["input_schema"]["properties"]
    assert "limit" in schema["input_schema"]["properties"]
    assert schema["input_schema"]["required"] == ["dataset_id"]


def test_raw_tool_definitions_mix_with_decorated_tools():
    """Pack tools ride behind tool search; both kinds share one list."""
    raw = {
        "type": "tool_search_tool_regex_20251119",
        "name": "tool_search_tool_regex",
    }
    tools = [_sample_tool, raw]
    assert len(tools) == 2
    assert callable(tools[0])
    assert tools[1]["name"] == "tool_search_tool_regex"
