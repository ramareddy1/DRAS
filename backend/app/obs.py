"""Observability: JSON logs, request IDs, Sentry.

One line of JSON per request on stdout — grep-able on a VPS, parseable by
any log shipper later. Request IDs round-trip via X-Request-ID so a client
report ("it failed, id abc123") finds the exact log line and Sentry event.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

_EXTRA_FIELDS = ("request_id", "account_id", "job_id", "method", "path",
                 "status", "duration_ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k in _EXTRA_FIELDS:
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("RECONOPS_LOG_LEVEL", "INFO").upper())


access_log = logging.getLogger("reconops.access")


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        started = time.time()
        extra = {
            "request_id": rid,
            "account_id": request.headers.get("X-Account-Id"),
            "method": request.method,
            "path": request.url.path,
        }
        try:
            response = await call_next(request)
        except Exception:
            access_log.error("unhandled error", extra={
                **extra, "duration_ms": int((time.time() - started) * 1000)})
            raise
        response.headers["X-Request-ID"] = rid
        access_log.info("request", extra={
            **extra, "status": response.status_code,
            "duration_ms": int((time.time() - started) * 1000)})
        return response


def setup_sentry() -> bool:
    """Env-gated: no SENTRY_DSN -> no-op. Returns whether Sentry is active."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    import sentry_sdk
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("RECONOPS_ENV", "production"),
        traces_sample_rate=0.0,
    )
    return True
