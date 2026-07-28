from cryptography.fernet import Fernet

from app.memory import accounts
from app.integrations import connections_store


def _set_key(monkeypatch):
    monkeypatch.setenv("RECONOPS_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_upsert_and_get_round_trip(monkeypatch):
    _set_key(monkeypatch)
    acc = accounts.create_account()
    conn = connections_store.upsert_connection(acc.id, "shopify", "test-shop.myshopify.com", "shpat_secret")

    assert conn.shop_domain == "test-shop.myshopify.com"
    assert conn.status == "connected"

    loaded = connections_store.get_connection(acc.id, "shopify")
    assert loaded.shop_domain == "test-shop.myshopify.com"


def test_token_is_encrypted_at_rest(monkeypatch):
    _set_key(monkeypatch)
    acc = accounts.create_account()
    connections_store.upsert_connection(acc.id, "shopify", "test-shop.myshopify.com", "shpat_secret")

    from app.db.base import session_scope
    from app.db.models import ConnectionORM
    with session_scope() as s:
        row = s.query(ConnectionORM).filter(ConnectionORM.account_id == acc.id).first()
        assert "shpat_secret" not in row.payload["encrypted_token"]

    assert connections_store.get_decrypted_token(acc.id, "shopify") == "shpat_secret"


def test_upsert_replaces_existing_connection(monkeypatch):
    _set_key(monkeypatch)
    acc = accounts.create_account()
    connections_store.upsert_connection(acc.id, "shopify", "first-shop.myshopify.com", "token1")
    connections_store.upsert_connection(acc.id, "shopify", "second-shop.myshopify.com", "token2")

    conns = connections_store.list_connections(acc.id)
    assert len(conns) == 1
    assert conns[0].shop_domain == "second-shop.myshopify.com"


def test_delete_connection_is_noop_when_missing(monkeypatch):
    _set_key(monkeypatch)
    acc = accounts.create_account()
    connections_store.delete_connection(acc.id, "shopify")  # must not raise
    assert connections_store.get_connection(acc.id, "shopify") is None


def test_mark_synced_and_mark_error(monkeypatch):
    _set_key(monkeypatch)
    acc = accounts.create_account()
    connections_store.upsert_connection(acc.id, "shopify", "test-shop.myshopify.com", "tok")

    connections_store.mark_synced(acc.id, "shopify")
    assert connections_store.get_connection(acc.id, "shopify").last_synced_at is not None

    connections_store.mark_error(acc.id, "shopify")
    assert connections_store.get_connection(acc.id, "shopify").status == "error"


def test_deleting_account_cascades_to_connections(monkeypatch):
    _set_key(monkeypatch)
    acc = accounts.create_account()
    connections_store.upsert_connection(acc.id, "shopify", "test-shop.myshopify.com", "shpat_secret")

    from app.db.base import session_scope
    from app.db.models import AccountORM, ConnectionORM
    with session_scope() as s:
        s.delete(s.get(AccountORM, acc.id))

    with session_scope() as s:
        assert s.query(ConnectionORM).filter(ConnectionORM.account_id == acc.id).count() == 0
