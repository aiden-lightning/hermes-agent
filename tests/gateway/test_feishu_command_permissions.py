from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source(
    *,
    user_id: str = "u_member",
    user_id_alt: str | None = None,
    chat_type: str = "dm",
    thread_id: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_name="Feishu Chat",
        chat_type=chat_type,
        user_id=user_id,
        user_id_alt=user_id_alt,
        user_name="tester",
        thread_id=thread_id,
    )


def _make_event(
    text: str,
    *,
    source: SessionSource | None = None,
    raw_message: object | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=source or _make_source(),
        message_id="m1",
        raw_message=raw_message,
    )


def _make_runner(
    *,
    command_permissions: dict | None = None,
    tool_permissions: dict | None = None,
    admins: list[str] | None = None,
):
    from gateway.run import GatewayRunner

    extra = {}
    if admins is not None:
        extra["admins"] = admins
    if command_permissions is not None:
        extra["command_permissions"] = command_permissions
    if tool_permissions is not None:
        extra["tool_permissions"] = tool_permissions

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.FEISHU: PlatformConfig(enabled=True, token="***", extra=extra),
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._send_with_retry = AsyncMock()
    runner.adapters = {Platform.FEISHU: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.FEISHU,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_feishu_member_can_use_whitelisted_command():
    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status"]},
    )
    runner._handle_status_command = AsyncMock(return_value="status: ok")

    result = await runner._handle_message(_make_event("/status"))

    assert result == "status: ok"
    runner._handle_status_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_feishu_member_blocked_from_non_whitelisted_command():
    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status"]},
    )
    runner._handle_model_command = AsyncMock(
        side_effect=AssertionError("blocked command reached handler")
    )

    result = await runner._handle_message(_make_event("/model gpt-5"))

    assert result is not None
    assert "don't have permission" in result.lower()
    assert "/status" in result


def test_feishu_member_free_text_blocked_by_default():
    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status", "help"]},
    )

    result = runner._check_command_permissions(
        _make_event("hello"),
        raw_command=None,
        canonical_command=None,
    )

    assert result is not None
    assert "free-form prompts" in result.lower()
    assert "/status" in result
    assert "/help" in result


def test_feishu_admin_open_id_bypasses_plain_text_restrictions():
    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status"]},
    )
    raw_message = SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_admin", user_id="u_admin", union_id="on_admin")
            )
        )
    )

    result = runner._check_command_permissions(
        _make_event(
            "hello",
            source=_make_source(user_id="u_admin", user_id_alt="on_admin"),
            raw_message=raw_message,
        ),
        raw_command=None,
        canonical_command=None,
    )

    assert result is None


def test_feishu_member_can_opt_in_to_plain_text():
    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={
            "allowed_commands": ["status"],
            "allow_plain_text": True,
        },
    )

    result = runner._check_command_permissions(
        _make_event("hello"),
        raw_command=None,
        canonical_command=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_feishu_member_plain_text_resolves_pending_clarify_when_plain_text_disallowed():
    from tools import clarify_gateway as cm

    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status"]},
    )
    source = _make_source()
    cm.clear_session(build_session_key(source))
    cm.register(
        clarify_id="clarify-feishu-perms",
        session_key=build_session_key(source),
        question="What should I do next?",
        choices=None,
    )

    try:
        result = await runner._handle_message(_make_event("use option B", source=source))

        assert result == ""
        assert cm.wait_for_response("clarify-feishu-perms", timeout=0) == "use option B"
    finally:
        cm.clear_session(build_session_key(source))


@pytest.mark.asyncio
async def test_feishu_busy_steer_plain_text_respects_command_permissions():
    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status"], "allow_plain_text": False},
    )
    runner._busy_input_mode = "steer"
    source = _make_source()
    session_key = build_session_key(source)
    running_agent = MagicMock()
    running_agent.steer.return_value = True
    runner._running_agents[session_key] = running_agent

    handled = await runner._handle_active_session_busy_message(
        _make_event("inject this", source=source),
        session_key,
    )

    assert handled is True
    running_agent.steer.assert_not_called()
    adapter = runner.adapters[Platform.FEISHU]
    adapter._send_with_retry.assert_awaited_once()
    assert "free-form prompts" in adapter._send_with_retry.call_args.kwargs["content"].lower()


@pytest.mark.asyncio
async def test_feishu_member_slash_command_does_not_resolve_pending_clarify():
    from tools import clarify_gateway as cm

    runner = _make_runner(
        admins=["ou_admin"],
        command_permissions={"allowed_commands": ["status"]},
    )
    runner._handle_status_command = AsyncMock(return_value="status: ok")
    source = _make_source()
    cm.clear_session(build_session_key(source))
    cm.register(
        clarify_id="clarify-feishu-slash",
        session_key=build_session_key(source),
        question="What should I do next?",
        choices=None,
    )

    try:
        result = await runner._handle_message(_make_event("/status", source=source))

        assert result == "status: ok"
        assert cm.get_pending_for_session(build_session_key(source)) is not None
    finally:
        cm.clear_session(build_session_key(source))


def test_feishu_member_tool_permissions_limit_enabled_toolsets(monkeypatch):
    import hermes_cli.tools_config as tools_config

    runner = _make_runner(
        admins=["ou_admin"],
        tool_permissions={
            "allowed_toolsets": ["web", "memory"],
            "disabled_toolsets": ["memory"],
        },
    )
    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda *_args, **_kwargs: {"terminal", "web", "memory"},
    )

    enabled = runner._resolve_effective_enabled_toolsets(
        user_config={
            "platforms": {
                "feishu": {
                    "extra": {
                        "admins": ["ou_admin"],
                        "tool_permissions": {
                            "allowed_toolsets": ["web", "memory"],
                            "disabled_toolsets": ["memory"],
                        },
                    }
                }
            }
        },
        source=_make_source(),
        actor_ids={"u_member"},
    )

    assert enabled == ["web"]


def test_feishu_member_can_get_curl_without_terminal(monkeypatch):
    import hermes_cli.tools_config as tools_config

    runner = _make_runner(
        admins=["ou_admin"],
        tool_permissions={"allowed_toolsets": ["curl", "web"]},
    )
    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda *_args, **_kwargs: {"curl", "terminal", "web"},
    )

    enabled = runner._resolve_effective_enabled_toolsets(
        user_config={
            "platforms": {
                "feishu": {
                    "extra": {
                        "admins": ["ou_admin"],
                        "tool_permissions": {"allowed_toolsets": ["curl", "web"]},
                    }
                }
            }
        },
        source=_make_source(),
        actor_ids={"u_member"},
    )

    assert enabled == ["curl", "web"]


def test_feishu_admin_tool_permissions_bypass_member_limits(monkeypatch):
    import hermes_cli.tools_config as tools_config

    runner = _make_runner(
        admins=["ou_admin"],
        tool_permissions={"allowed_toolsets": ["web"]},
    )
    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda *_args, **_kwargs: {"terminal", "web", "memory"},
    )

    enabled = runner._resolve_effective_enabled_toolsets(
        user_config={
            "platforms": {
                "feishu": {
                    "extra": {
                        "admins": ["ou_admin"],
                        "tool_permissions": {"allowed_toolsets": ["web"]},
                    }
                }
            }
        },
        source=_make_source(user_id="u_admin", user_id_alt="on_admin"),
        actor_ids={"ou_admin", "u_admin", "on_admin"},
    )

    assert enabled == ["memory", "terminal", "web"]


def test_feishu_shared_session_scope_does_not_grant_tool_admin(monkeypatch):
    import hermes_cli.tools_config as tools_config

    scope_owner = "session_scope:agent:main:feishu:group:oc_chat:omt_thread"
    runner = _make_runner(
        admins=[scope_owner],
        tool_permissions={"allowed_toolsets": ["web"]},
    )
    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda *_args, **_kwargs: {"terminal", "web", "memory"},
    )

    enabled = runner._resolve_effective_enabled_toolsets(
        user_config={
            "platforms": {
                "feishu": {
                    "extra": {
                        "admins": [scope_owner],
                        "tool_permissions": {"allowed_toolsets": ["web"]},
                    }
                }
            }
        },
        source=_make_source(
            user_id="u_member",
            user_id_alt="on_member",
            chat_type="group",
            thread_id="omt_thread",
        ),
        actor_ids={"ou_member", "u_member", "on_member"},
    )

    assert enabled == ["web"]


def test_feishu_member_session_search_filters_to_own_identity():
    runner = _make_runner(
        admins=["ou_admin"],
        tool_permissions={"allowed_toolsets": ["web", "session_search"]},
    )
    source = _make_source(user_id="u_member", user_id_alt="on_member")

    filters = runner._resolve_session_search_filters(
        user_config={
            "platforms": {
                "feishu": {
                    "extra": {
                        "admins": ["ou_admin"],
                        "tool_permissions": {"allowed_toolsets": ["web", "session_search"]},
                    }
                }
            }
        },
        source=source,
        actor_ids={"ou_member", "u_member", "on_member"},
    )

    assert filters == {
        "source_filter": ["feishu"],
        "user_id_filter": ["on_member", "ou_member", "u_member"],
        "include_unowned_user_sessions": False,
    }


def test_feishu_shared_thread_session_search_includes_session_scope():
    runner = _make_runner(admins=["ou_admin"])
    source = _make_source(
        user_id="u_member",
        user_id_alt="on_member",
        chat_type="group",
        thread_id="omt_thread",
    )

    filters = runner._resolve_session_search_filters(
        user_config={"platforms": {"feishu": {"extra": {"admins": ["ou_admin"]}}}},
        source=source,
        actor_ids={"ou_member", "u_member", "on_member"},
    )

    assert filters == {
        "source_filter": ["feishu"],
        "user_id_filter": [
            "on_member",
            "ou_member",
            "session_scope:agent:main:feishu:group:oc_chat:omt_thread",
            "u_member",
        ],
        "include_unowned_user_sessions": False,
    }


def test_feishu_admin_session_search_filters_to_own_identity():
    runner = _make_runner(
        admins=["ou_admin"],
        tool_permissions={"allowed_toolsets": ["web", "session_search"]},
    )

    filters = runner._resolve_session_search_filters(
        user_config={
            "platforms": {
                "feishu": {
                    "extra": {
                        "admins": ["ou_admin"],
                        "tool_permissions": {"allowed_toolsets": ["web", "session_search"]},
                    }
                }
            }
        },
        source=_make_source(user_id="u_admin", user_id_alt="on_admin"),
        actor_ids={"ou_admin", "u_admin", "on_admin"},
    )

    assert filters == {
        "source_filter": ["feishu"],
        "user_id_filter": ["on_admin", "ou_admin", "u_admin"],
        "include_unowned_user_sessions": True,
    }


def test_feishu_session_search_filters_without_tool_permissions():
    runner = _make_runner(admins=["ou_admin"])

    filters = runner._resolve_session_search_filters(
        user_config={"platforms": {"feishu": {"extra": {"admins": ["ou_admin"]}}}},
        source=_make_source(user_id="u_member", user_id_alt="on_member"),
        actor_ids={"ou_member", "u_member", "on_member"},
    )

    assert filters == {
        "source_filter": ["feishu"],
        "user_id_filter": ["on_member", "ou_member", "u_member"],
        "include_unowned_user_sessions": False,
    }
