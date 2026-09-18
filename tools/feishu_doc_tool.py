"""Feishu Document Tool -- read document content via Feishu/Lark API.

Provides ``feishu_doc_read`` for reading document content as plain text.
Uses the same lazy-import + BaseRequest pattern as feishu_comment.py.
"""

from tools.feishu_lark import (  # noqa: F401  (set_client/get_client are imported by feishu_comment)
    _check_feishu,
    build_fallback_client,
    build_request,
    get_client,
    raw_body,
    set_client)
from tools.registry import registry, tool_error, tool_result

_RAW_CONTENT_URI = "/open-apis/docx/v1/documents/:document_id/raw_content"
_WIKI_GET_NODE_URI = "/open-apis/wiki/v2/spaces/get_node"

FEISHU_DOC_READ_SCHEMA = {
    "name": "feishu_doc_read",
    "description": (
        "Read the full content of a Feishu/Lark document as plain text. "
        "Useful when you need more context beyond the quoted text in a comment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doc_token": {
                "type": "string",
                "description": "The document token (from the document URL or comment context).",
            },
        },
        "required": ["doc_token"],
    },
}


def _handle_feishu_doc_read(args: dict, **kwargs) -> str:
    doc_token = args.get("doc_token", "").strip()
    if not doc_token:
        return tool_error("doc_token is required")
    client = get_client()
    if client is None:
        client, error = build_fallback_client()
        if client is None:
            return tool_error(error or "Feishu client not available")

    def _read(document_id: str):
        request = build_request("GET", _RAW_CONTENT_URI, paths={"document_id": document_id})
        return client.request(request)

    def _resolve_wiki(token: str):
        request = build_request("GET", _WIKI_GET_NODE_URI, queries=[("token", token)])
        response = client.request(request)
        code = getattr(response, "code", None)
        if code != 0:
            return None, tool_error(
                f"Failed to resolve wiki node: code={code} msg={getattr(response, 'msg', 'unknown error')}"
            )
        body = raw_body(response) or {}
        node = body.get("data", {}).get("node", {})
        obj_type = node.get("obj_type", "")
        obj_token = node.get("obj_token", "")
        if not obj_type or not obj_token:
            return None, tool_error("Wiki node did not include obj_type/obj_token")
        if obj_type != "docx":
            return None, tool_error(f"Wiki node resolves to unsupported obj_type={obj_type}")
        return obj_token, None

    def _content(response):
        body = raw_body(response) or {}
        content = body.get("data", {}).get("content", "")
        if isinstance(content, str) and content:
            return content
        data = getattr(response, "data", None)
        if isinstance(data, dict):
            return data.get("content", "")
        return getattr(data, "content", "") if data else ""

    try:
        effective_token = doc_token
        if doc_token.startswith("wiki"):
            effective_token, error = _resolve_wiki(doc_token)
            if error:
                return error
        response = _read(effective_token)
    except ImportError:
        return tool_error("lark_oapi not installed")

    code = getattr(response, "code", None)
    if code == 0:
        content = _content(response)
        if not content:
            return tool_error("No content returned from document API")
        return tool_result(success=True, content=content)
    if code == 1770002 and not doc_token.startswith("wiki"):
        try:
            effective_token, error = _resolve_wiki(doc_token)
            if error:
                return error
            response = _read(effective_token)
        except ImportError:
            return tool_error("lark_oapi not installed")
        code = getattr(response, "code", None)
        if code == 0:
            content = _content(response)
            if not content:
                return tool_error("No content returned from document API")
            return tool_result(success=True, content=content)
    return tool_error(f"Failed to read document: code={code} msg={getattr(response, 'msg', 'unknown error')}")


registry.register(
    name="feishu_doc_read", toolset="feishu_doc", schema=FEISHU_DOC_READ_SCHEMA, handler=_handle_feishu_doc_read,
    check_fn=_check_feishu, requires_env=[], is_async=False, description="Read Feishu document content",
    emoji="\U0001f4c4")


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import json  # noqa: F401,E402
import logging  # noqa: F401,E402
import threading  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'logger': ('tools.approval', 'logger'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
