import json
import importlib
from unittest.mock import Mock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_browser_cdp_url(monkeypatch):
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    yield
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)


def _ok_response():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status = 200
    return response


def test_browser_connect_tool_is_registered_without_user_supplied_exec_args():
    import tools.browser_tool as browser_tool

    entry = browser_tool.registry.get_entry("browser_connect")

    assert entry is not None
    schema = entry.schema
    assert schema["parameters"]["properties"] == {}
    assert "executable" not in schema["parameters"]["properties"]
    assert "args" not in schema["parameters"]["properties"]


def test_browser_connect_reuses_reachable_default_local_endpoint(monkeypatch):
    import tools.browser_tool_connect as browser_connect

    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

    with patch(
        "hermes_cli.browser_connect.discover_local_cdp_url",
        return_value="http://127.0.0.1:9222",
    ) as discover, patch("hermes_cli.browser_connect.launch_chrome_debug") as launch, patch(
        "tools.browser_tool_lifecycle.cleanup_all_browsers",
    ) as cleanup:
        result = json.loads(browser_connect.browser_connect())

    assert result["connected"] is True
    assert result["url"] == "http://127.0.0.1:9222"
    assert result["launched"] is False
    assert browser_connect.os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"
    launch.assert_not_called()
    assert discover.call_args.args[0] == 9222
    cleanup.assert_called_once_with()


def test_browser_connect_launches_only_default_local_when_unreachable(monkeypatch):
    import tools.browser_tool_connect as browser_connect

    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    calls = {"count": 0}

    def fake_discover(port, timeout):
        calls["count"] += 1
        if calls["count"] < 3:
            return None
        return "http://127.0.0.1:9222"

    launch_result = Mock(launched=True)
    with patch("hermes_cli.browser_connect.discover_local_cdp_url", side_effect=fake_discover), \
         patch("tools.browser_tool_connect.time.sleep", return_value=None), \
         patch("hermes_cli.browser_connect.launch_chrome_debug", return_value=launch_result) as launch, \
         patch("tools.browser_tool_lifecycle.cleanup_all_browsers"):
        result = json.loads(browser_connect.browser_connect())

    assert result["connected"] is True
    assert result["url"] == "http://127.0.0.1:9222"
    assert result["launched"] is True
    assert browser_connect.os.environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"
    launch.assert_called_once()
    assert launch.call_args.args[0] == 9222


def test_browser_connect_does_not_launch_remote_configured_endpoint(monkeypatch):
    import tools.browser_tool_connect as browser_connect

    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

    with patch("hermes_cli.config.read_raw_config", return_value={"browser": {"cdp_url": "http://remote.example:9222"}}), \
         patch("tools.browser_tool_connect._http_reachable", return_value=False), \
         patch("hermes_cli.browser_connect.launch_chrome_debug") as launch:
        result = json.loads(browser_connect.browser_connect())

    assert result["connected"] is False
    assert "could not reach browser CDP" in result["error"]
    assert "BROWSER_CDP_URL" not in browser_connect.os.environ
    launch.assert_not_called()


def test_importing_browser_tool_does_not_launch_chrome(monkeypatch):
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

    with patch("subprocess.Popen") as popen:
        import tools.browser_tool as browser_tool
        importlib.reload(browser_tool)

    popen.assert_not_called()


def test_browser_navigate_does_not_implicitly_launch_default_local_cdp(monkeypatch):
    import tools.browser_tool as browser_tool

    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    session_info = {"session_name": "h_test", "cdp_url": None, "features": {}, "_first_nav": False}

    def fake_run_browser_command(task_id, command, args=None, timeout=None):
        if command == "open":
            return {"success": True, "data": {"title": "Example", "url": args[0]}}
        if command == "snapshot":
            return {"success": True, "data": {"snapshot": "Example page", "refs": {}}}
        raise AssertionError(f"unexpected browser command: {command}")

    with patch("tools.browser_tool_connect._ensure_browser_cdp_connected") as ensure_cdp, \
         patch("hermes_cli.browser_connect.launch_chrome_debug") as launch, \
         patch("tools.browser_tool_session._get_session_info", return_value=session_info), \
         patch("tools.browser_tool_session._run_browser_command", side_effect=fake_run_browser_command) as run_cmd:
        result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="local-default"))

    assert result["success"] is True
    ensure_cdp.assert_not_called()
    launch.assert_not_called()
    assert run_cmd.call_args_list[0].args[0] == "local-default"
    assert "BROWSER_CDP_URL" not in browser_tool.os.environ
    assert session_info["cdp_url"] is None
