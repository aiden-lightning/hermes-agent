"""Explicit Chrome DevTools Protocol connection tool."""

from __future__ import annotations

import contextlib
import os
import platform
import socket
import time
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from tools.registry import registry, tool_error, tool_result


def _is_default_local_cdp(parsed) -> bool:
    with contextlib.suppress(ValueError):
        return (
            parsed.scheme in {"http", "ws"}
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and (parsed.port or 80) == 9222
            and parsed.path in {"", "/", "/json", "/json/version"}
        )
    return False


def _http_reachable(parsed, timeout: float = 2.0) -> bool:
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    root = f"{scheme}://{parsed.netloc}".rstrip("/")
    for url in (f"{root}/json/version", f"{root}/json"):
        with contextlib.suppress(Exception), urllib.request.urlopen(url, timeout=timeout) as response:
            if 200 <= getattr(response, "status", 200) < 300:
                return True
    return False


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _connect_local_default(port: int) -> tuple[Optional[str], bool]:
    from hermes_cli.browser_connect import discover_local_cdp_url, launch_chrome_debug

    if discovered := discover_local_cdp_url(port, timeout=2.0):
        return discovered, False
    launch = launch_chrome_debug(port, platform.system())
    if not launch.launched:
        return None, False
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if discovered := discover_local_cdp_url(port, timeout=1.0):
            return discovered, True
        time.sleep(0.5)
    return None, True


def _ensure_browser_cdp_connected() -> Dict[str, Any]:
    """Validate the configured endpoint, launching only the default local CDP browser."""
    from hermes_cli.browser_connect import DEFAULT_BROWSER_CDP_URL
    from tools.browser_tool_cdp import _get_cdp_override_raw

    raw_url = _get_cdp_override_raw() or DEFAULT_BROWSER_CDP_URL
    parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return {"success": False, "connected": False, "error": f"unsupported browser url: {raw_url}"}
    if not parsed.hostname:
        return {"success": False, "connected": False, "error": f"missing host in browser url: {raw_url}"}
    try:
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    except ValueError:
        return {"success": False, "connected": False, "error": f"invalid port in browser url: {raw_url}"}

    launched = False
    if _is_default_local_cdp(parsed):
        resolved, launched = _connect_local_default(port)
        if not resolved:
            return {
                "success": False, "connected": False, "launched": launched,
                "error": f"could not reach browser CDP at {raw_url}",
            }
        parsed = urlparse(resolved)
    elif parsed.scheme in {"ws", "wss"} and parsed.path.startswith("/devtools/browser/"):
        if not _tcp_reachable(parsed.hostname, port):
            return {
                "success": False, "connected": False, "launched": False,
                "error": f"could not reach browser CDP at {raw_url}",
            }
    elif not _http_reachable(parsed):
        return {
            "success": False, "connected": False, "launched": False,
            "error": f"could not reach browser CDP at {raw_url}",
        }

    normalized = (
        parsed.geturl()
        if parsed.path.startswith("/devtools/browser/")
        else parsed._replace(path="", params="", query="", fragment="").geturl()
    )
    return {"success": True, "connected": True, "url": normalized, "launched": launched}


def browser_connect(task_id: Optional[str] = None) -> str:
    """Connect browser tools to configured/default CDP without accepting launch arguments."""
    from tools.browser_tool_cdp import _ensure_cdp_supervisor
    from tools.browser_tool_lifecycle import cleanup_all_browsers

    result = _ensure_browser_cdp_connected()
    if not result.get("success"):
        return tool_error(
            result.get("error", "could not reach browser CDP"),
            connected=False,
            launched=result.get("launched", False),
            success=False,
        )
    os.environ["BROWSER_CDP_URL"] = result["url"]
    cleanup_all_browsers()
    if task_id:
        _ensure_cdp_supervisor(task_id)
    return tool_result(
        connected=True, url=result["url"], launched=result.get("launched", False),
    )


BROWSER_CONNECT_SCHEMA = {
    "name": "browser_connect",
    "description": (
        "Explicitly connect browser tools to the configured Chrome DevTools Protocol endpoint. "
        "If no endpoint is configured, use the default local Chrome debug endpoint and launch a "
        "detected Chromium-family browser when needed. Accepts no executable path or launch arguments."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _handler(args, **kwargs):
    return browser_connect(task_id=kwargs.get("task_id"))


from tools.browser_tool_install import check_browser_requirements

registry.register(
    name="browser_connect",
    toolset="browser",
    schema=BROWSER_CONNECT_SCHEMA,
    handler=_handler,
    check_fn=check_browser_requirements,
    emoji="🌐",
)
