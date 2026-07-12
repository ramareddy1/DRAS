import json
import threading

import pytest


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    return tmp_path


def test_atomic_write_json_replaces_not_truncates(data_dir):
    from app.memory.fsutil import atomic_write_json
    target = data_dir / "x.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    # No stray temp files left behind
    assert list(data_dir.glob("*.tmp")) == []


def test_concurrent_add_rule_loses_no_writes(data_dir, monkeypatch):
    # Re-import with patched DATA_DIR (module-level constant)
    import importlib
    from app.memory import rules_store
    importlib.reload(rules_store)
    from app.models import Rule

    account_id = "11111111-1111-4111-8111-111111111111"
    n = 25

    def add(i):
        rules_store.add_rule(account_id, Rule(
            account_id=account_id, kind="custom",
            description=f"rule-{i}", when={}, then={},
            origin="test", confidence=0.5, state="pending",
        ))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rules = rules_store.load_rules(account_id)
    assert len(rules) == n  # unlocked read-modify-write loses writes here
