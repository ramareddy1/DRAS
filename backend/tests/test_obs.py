import importlib


def test_cors_origins_come_from_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_CORS_ORIGINS", "https://app.example.com, https://staging.example.com")
    from app import main
    importlib.reload(main)
    cors = next(m for m in main.app.user_middleware
                if m.cls.__name__ == "CORSMiddleware")
    assert cors.kwargs["allow_origins"] == [
        "https://app.example.com", "https://staging.example.com"]


def test_health_reports_version(monkeypatch):
    monkeypatch.delenv("RECONOPS_CORS_ORIGINS", raising=False)
    from app import main
    importlib.reload(main)
    body = main.health()
    assert body["ok"] is True
    assert body["version"] == main.app.version
