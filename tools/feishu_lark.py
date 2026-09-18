"""Shared lark_oapi plumbing for the feishu_doc / feishu_drive tool modules.

Holds the per-thread client handle, the cheap availability probe, and the generic
``BaseRequest`` builder + response unpacking (same pattern as feishu_comment.py).
No ``registry.register`` here on purpose: tool discovery must not import this module.
"""

import json
import threading

from gateway.platforms._shared import get_scoped_secret

# The lark client is injected per-thread by the feishu_comment event handler right
# before it runs the agent, so concurrent comment events never see each other's client.
_local = threading.local()


def set_client(client):
    """Store a lark client for the current thread (called by feishu_comment)."""
    _local.client = client


def get_client():
    """Return the lark client for the current thread, or None."""
    return getattr(_local, "client", None)


def build_fallback_client():
    """Build a profile-scoped Feishu/Lark client outside comment context."""
    app_id = str(get_scoped_secret("FEISHU_APP_ID", "") or "").strip()
    app_secret = str(get_scoped_secret("FEISHU_APP_SECRET", "") or "").strip()
    domain_name = str(get_scoped_secret("FEISHU_DOMAIN", "feishu") or "feishu").strip().lower()
    if not app_id or not app_secret:
        return None, (
            "Feishu client not available (not in a Feishu comment context and "
            "FEISHU_APP_ID/FEISHU_APP_SECRET are missing)"
        )
    try:
        import lark_oapi as lark
    except ImportError:
        return None, "lark_oapi not installed"
    domain = getattr(lark, "DOMAIN_FEISHU", None)
    if domain_name == "lark":
        domain = getattr(lark, "DOMAIN_LARK", domain)
    builder = lark.Client.builder().app_id(app_id).app_secret(app_secret)
    if domain is not None:
        builder = builder.domain(domain)
    if hasattr(lark, "LogLevel") and hasattr(lark.LogLevel, "WARNING"):
        builder = builder.log_level(lark.LogLevel.WARNING)
    return builder.build(), None


def _check_feishu():
    # find_spec checks importability without executing lark_oapi's __init__, which
    # eagerly loads websockets/dispatcher/every api model (~5s). This probe fires at
    # every ``hermes`` startup; the handlers still do the real import when invoked.
    import importlib.util
    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


def build_request(method, uri, paths=None, queries=None, body=None):
    """Build a tenant-token BaseRequest. Raises ImportError if lark_oapi is missing."""
    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest

    builder = (
        BaseRequest.builder()
        .http_method(HttpMethod.GET if method == "GET" else HttpMethod.POST)
        .uri(uri)
        .token_types({AccessTokenType.TENANT}))
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    if body is not None:
        builder = builder.body(body)
    return builder.build()


def lark_call(client, method, uri, paths=None, queries=None, body=None):
    """Build + execute a BaseRequest; returns (code, msg, data_dict).

    Tool handlers run synchronously in a worker thread (no running event loop), so the
    blocking lark client is called directly.
    """
    response = client.request(build_request(method, uri, paths, queries, body))
    return getattr(response, "code", None), getattr(response, "msg", ""), response_data(response)


def raw_body(response):
    """Parsed JSON object of the raw HTTP body, or None when absent/unparseable/not a dict."""
    raw = getattr(response, "raw", None)
    try:
        body = json.loads(raw.content) if raw and hasattr(raw, "content") else None
    except (json.JSONDecodeError, AttributeError):
        return None
    return body if isinstance(body, dict) else None


def response_data(response) -> dict:
    """``data`` of a lark response: prefer the raw JSON body, fall back to typed .data."""
    body = raw_body(response)
    data = body.get("data", {}) if body is not None else {}
    if data:
        return data
    resp_data = getattr(response, "data", None)
    if isinstance(resp_data, dict):
        return resp_data
    return vars(resp_data) if resp_data and hasattr(resp_data, "__dict__") else data
