from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, NO_REPLY_SENTINEL
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


def _runner_for_no_reply(
    tmp_path,
    monkeypatch,
    final_response,
    *,
    persist_to_db=False,
    user_text="Feishu quick reaction",
):
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"compression": {"enabled": False}},
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="test")}
    )
    runner.session_store = SessionStore(sessions_dir=tmp_path, config=runner.config)
    runner._session_db = runner.session_store._db if persist_to_db else None
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._model = "test-model"
    runner._base_url = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.adapters = {
        Platform.FEISHU: SimpleNamespace(
            send=AsyncMock(),
            stop_typing=AsyncMock(),
        )
    }
    runner._is_telegram_topic_lane = MagicMock(return_value=False)
    runner._collect_event_sender_ids = MagicMock(return_value=set())
    runner._resolve_effective_enabled_toolsets = MagicMock(return_value=[])
    runner._set_session_env = MagicMock(return_value=object())
    runner._clear_session_env = MagicMock()
    runner._prepare_inbound_message_text = AsyncMock(side_effect=lambda event, **_: event.text)
    runner._bind_adapter_run_generation = MagicMock()
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._clear_restart_failure_count = MagicMock()
    runner._drain_process_watcher_registrations = MagicMock()
    runner._should_send_voice_reply = MagicMock(return_value=False)
    runner._send_voice_reply = AsyncMock()
    runner._deliver_media_from_response = AsyncMock()
    async def _run_agent(**kwargs):
        if persist_to_db and runner._session_db is not None:
            runner._session_db.create_session(kwargs["session_id"], "feishu")
            runner._session_db.append_message(kwargs["session_id"], "user", user_text)
            runner._session_db.append_message(kwargs["session_id"], "assistant", final_response)
        return {
            "final_response": final_response,
            "messages": [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": final_response},
            ],
            "history_offset": 0,
            "api_calls": 1,
        }

    runner._run_agent = AsyncMock(side_effect=_run_agent)
    return runner


def _reaction_event():
    source = SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_type="group",
        user_id="ou_user",
        user_name="User",
    )
    return MessageEvent(
        text="Feishu quick reaction",
        message_type=MessageType.TEXT,
        source=source,
        message_id="reaction-event",
        metadata={
            "feishu_event_kind": "reaction",
            "no_reply_sentinel": NO_REPLY_SENTINEL,
        },
    )


@pytest.mark.asyncio
async def test_reaction_no_reply_sentinel_returns_none_and_does_not_send(tmp_path, monkeypatch):
    runner = _runner_for_no_reply(tmp_path, monkeypatch, f"  {NO_REPLY_SENTINEL}\n")
    event = _reaction_event()
    session_key = runner.session_store.get_or_create_session(event.source).session_key

    result = await runner._handle_message_with_agent(event, event.source, session_key, run_generation=1)

    assert result is None
    assert runner.adapters[Platform.FEISHU].send.await_args_list == []
    runner.hooks.emit.assert_any_await(
        "agent:end",
        {
            "platform": "feishu",
            "user_id": "ou_user",
            "chat_id": "oc_chat",
            "session_id": runner.session_store.get_or_create_session(event.source).session_id,
            "message": "Feishu quick reaction",
            "response": "",
        },
    )

    transcript = runner.session_store.load_transcript(
        runner.session_store.get_or_create_session(event.source).session_id
    )
    assert {"role": "user", "content": "Feishu quick reaction"}.items() <= transcript[-1].items()
    assert all(
        not (msg.get("role") == "assistant" and msg.get("content", "").strip() == NO_REPLY_SENTINEL)
        for msg in transcript
    )


@pytest.mark.asyncio
async def test_reaction_normal_response_still_returns_for_delivery(tmp_path, monkeypatch):
    runner = _runner_for_no_reply(tmp_path, monkeypatch, "Thanks for the reaction.")
    event = _reaction_event()
    session_key = runner.session_store.get_or_create_session(event.source).session_key

    result = await runner._handle_message_with_agent(event, event.source, session_key, run_generation=1)

    assert result == "Thanks for the reaction."


@pytest.mark.asyncio
async def test_ordinary_message_no_reply_sentinel_returns_none_and_does_not_send(tmp_path, monkeypatch):
    runner = _runner_for_no_reply(tmp_path, monkeypatch, NO_REPLY_SENTINEL)
    event = _reaction_event()
    event.metadata = {}
    session_key = runner.session_store.get_or_create_session(event.source).session_key

    result = await runner._handle_message_with_agent(event, event.source, session_key, run_generation=1)

    assert result is None
    assert runner.adapters[Platform.FEISHU].send.await_args_list == []


@pytest.mark.asyncio
async def test_user_text_containing_no_reply_sentinel_still_processes_and_sends(tmp_path, monkeypatch):
    user_text = f"{NO_REPLY_SENTINEL} 的输入没有被跳过"
    runner = _runner_for_no_reply(
        tmp_path,
        monkeypatch,
        "normal response",
        user_text=user_text,
    )
    event = _reaction_event()
    event.text = user_text
    event.metadata = {}
    session_key = runner.session_store.get_or_create_session(event.source).session_key

    result = await runner._handle_message_with_agent(event, event.source, session_key, run_generation=1)

    assert result == "normal response"
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaction_no_reply_sentinel_is_removed_from_session_db(tmp_path, monkeypatch):
    runner = _runner_for_no_reply(tmp_path, monkeypatch, NO_REPLY_SENTINEL, persist_to_db=True)
    event = _reaction_event()
    session = runner.session_store.get_or_create_session(event.source)

    result = await runner._handle_message_with_agent(event, event.source, session.session_key, run_generation=1)

    assert result is None
    db_messages = runner._session_db.get_messages_as_conversation(session.session_id)
    assert any(msg.get("role") == "user" and msg.get("content") == "Feishu quick reaction" for msg in db_messages)
    assert all(
        not (msg.get("role") == "assistant" and msg.get("content", "").strip() == NO_REPLY_SENTINEL)
        for msg in db_messages
    )


@pytest.mark.asyncio
async def test_ordinary_message_no_reply_sentinel_is_removed_from_session_db(tmp_path, monkeypatch):
    runner = _runner_for_no_reply(tmp_path, monkeypatch, NO_REPLY_SENTINEL, persist_to_db=True)
    event = _reaction_event()
    event.metadata = {}
    session = runner.session_store.get_or_create_session(event.source)

    result = await runner._handle_message_with_agent(event, event.source, session.session_key, run_generation=1)

    assert result is None
    db_messages = runner._session_db.get_messages_as_conversation(session.session_id)
    assert any(msg.get("role") == "user" and msg.get("content") == "Feishu quick reaction" for msg in db_messages)
    assert all(
        not (msg.get("role") == "assistant" and msg.get("content", "").strip() == NO_REPLY_SENTINEL)
        for msg in db_messages
    )
