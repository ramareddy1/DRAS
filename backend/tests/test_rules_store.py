from app.memory import accounts, rules_store
from app.models import Rule


def test_save_and_load_rules_round_trip():
    acc = accounts.create_account()
    rule = Rule(account_id=acc.id, kind="fee_pattern", description="test rule",
                when={"rate": 0.01}, then={"status": "fee_offset"}, origin="user")
    rules_store.save_rules(acc.id, [rule])

    loaded = rules_store.load_rules(acc.id)
    assert len(loaded) == 1
    assert loaded[0].id == rule.id
    assert loaded[0].description == "test rule"


def test_save_rules_overwrites_previous_set():
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    assert len(rules_store.load_rules(acc.id)) == 3

    rules_store.save_rules(acc.id, [])
    assert rules_store.load_rules(acc.id) == []


def test_add_and_revoke_rule():
    acc = accounts.create_account()
    rule = Rule(account_id=acc.id, kind="force_status", description="pin",
                when={"signature_prefix": "abc"}, then={"status": "match"}, origin="user")
    rules_store.add_rule(acc.id, rule)
    assert len(rules_store.load_rules(acc.id)) == 1

    assert rules_store.revoke_rule(acc.id, rule.id) is True
    loaded = rules_store.load_rules(acc.id)
    assert loaded[0].state == "revoked"


def test_deleting_account_cascades_to_rules():
    from app.db.base import session_scope
    from app.db.models import AccountORM, RuleORM

    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)

    with session_scope() as s:
        s.delete(s.get(AccountORM, acc.id))

    with session_scope() as s:
        assert s.query(RuleORM).filter(RuleORM.account_id == acc.id).count() == 0
