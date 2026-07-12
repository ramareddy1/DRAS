import json


def _mk_candidate(key, diff):
    from app.models import Evidence, Rationale
    return {
        "rationale": Rationale(
            row_key=key, status="minor", confidence=0.70,
            rationale=[Evidence(source="threshold_minor", evidence="x")],
            alternatives=[],
        ),
        "row_ctx": {"key": key, "amount_a": 100.0, "amount_b": 100.0 - diff,
                    "diff_abs": diff, "diff_pct": diff, "match_type": "exact"},
    }


def test_batch_respects_cap_and_is_advisory(monkeypatch):
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")
    monkeypatch.setenv("RECONOPS_MAX_LLM_ROWS", "2")

    from app.tools.classify import batch_second_opinions

    candidates = [_mk_candidate("small", 1.0), _mk_candidate("big", 50.0),
                  _mk_candidate("mid", 10.0)]
    reviewed = batch_second_opinions(
        candidates=candidates, account_id="acc", job_id="job",
    )
    # Cap: only the top 2 by |diff_abs| were sent
    assert set(reviewed) <= {"big", "mid"}
    # Advisory: statuses unchanged on every candidate
    for c in candidates:
        assert c["rationale"].status == "minor"


def test_batch_appends_evidence_without_flipping_status(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")
    monkeypatch.setenv("RECONOPS_MAX_LLM_ROWS", "2")

    from app.tools import classify

    def fake_call(**kwargs):
        sent = json.loads(kwargs["messages"][0]["content"])
        # The reviewer disagrees with everything — must still not flip status
        return [{"key": r["key"], "agrees": False,
                 "note": f"diff ${r['diff_abs']} looks like a refund", "confidence": 0.9}
                for r in sent]

    monkeypatch.setattr(classify, "call_claude_json", fake_call)

    candidates = [_mk_candidate("small", 1.0), _mk_candidate("big", 50.0),
                  _mk_candidate("mid", 10.0)]
    reviewed = classify.batch_second_opinions(
        candidates=candidates, account_id="acc", job_id="job",
    )
    # Exactly the top 2 by $ impact were reviewed
    assert set(reviewed) == {"big", "mid"}
    for c in candidates:
        key = c["row_ctx"]["key"]
        sources = [e.source for e in c["rationale"].rationale]
        if key in ("big", "mid"):
            assert "llm_second_opinion" in sources
            assert c["rationale"].status == "minor"  # advisory only
        else:
            assert "llm_second_opinion" not in sources
