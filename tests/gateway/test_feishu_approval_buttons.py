"""Tests for Feishu interactive card approval buttons."""

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Feishu mock so FeishuAdapter can be imported without lark-oapi
# ---------------------------------------------------------------------------
def _ensure_feishu_mocks():
    """Provide stubs for lark-oapi / aiohttp.web so the import succeeds."""
    if importlib.util.find_spec("lark_oapi") is None and "lark_oapi" not in sys.modules:
        mod = MagicMock()
        for name in (
            "lark_oapi", "lark_oapi.api.im.v1",
            "lark_oapi.event", "lark_oapi.event.callback_type",
        ):
            sys.modules.setdefault(name, mod)
    if importlib.util.find_spec("aiohttp") is None and "aiohttp" not in sys.modules:
        aio = MagicMock()
        sys.modules.setdefault("aiohttp", aio)
        sys.modules.setdefault("aiohttp.web", aio.web)


_ensure_feishu_mocks()

from gateway.config import PlatformConfig
import gateway.platforms.feishu as feishu_module
from gateway.platforms.feishu import FeishuAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter() -> FeishuAdapter:
    """Create a FeishuAdapter with mocked internals."""
    config = PlatformConfig(enabled=True)
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _make_card_action_data(
    action_value: dict,
    chat_id: str = "oc_12345",
    open_id: str = "ou_user1",
    user_id: str = "u_user1",
    union_id: str = "on_user1",
    token: str = "tok_abc",
) -> SimpleNamespace:
    """Create a mock Feishu card action callback data object."""
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id, user_id=user_id, union_id=union_id),
            action=SimpleNamespace(
                tag="button",
                value=action_value,
            ),
        ),
    )


def _close_submitted_coro(coro, _loop):
    """Close coroutines submitted through asyncio.run_coroutine_threadsafe in tests."""
    coro.close()
    return SimpleNamespace(add_done_callback=lambda *_args, **_kwargs: None)


def _close_adapter_submitted_coro(_loop, coro):
    """Close coroutines passed to FeishuAdapter._submit_on_loop in sync-handler tests."""
    coro.close()
    return True


# ===========================================================================
# send_exec_approval — interactive card with buttons
# ===========================================================================

class TestFeishuExecApproval:
    """Test send_exec_approval sends an interactive card."""

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="rm -rf /important",
                session_key="agent:main:feishu:group:oc_12345",
                description="dangerous deletion",
            )

        assert result.success is True
        assert result.message_id == "msg_001"

        mock_send.assert_called_once()
        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"

        # Verify card payload contains the command and buttons
        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "rm -rf /important" in card["elements"][0]["content"]
        assert "dangerous deletion" in card["elements"][0]["content"]

        # Check buttons
        actions = card["elements"][1]["actions"]
        assert len(actions) == 4
        action_names = [a["value"]["hermes_action"] for a in actions]
        assert action_names == [
            "approve_once", "approve_session", "approve_always", "deny"
        ]

    @pytest.mark.asyncio
    async def test_logs_successful_approval_card_send_with_correlation_fields(self, caplog):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_logged"),
        )
        raw_token = "sk-proj-" + "c" * 24
        command = "bash -lc 'deploy --token " + raw_token + "'"

        caplog.set_level(logging.INFO, logger=feishu_module.logger.name)
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await adapter.send_exec_approval(
                chat_id="oc_logged",
                command=command,
                session_key="agent:main:feishu:group:oc_logged",
                description="dangerous deploy",
            )

        assert result.success is True
        log_text = caplog.text
        assert "send_exec_approval sent" in log_text
        assert "approval_id=" in log_text
        assert "chat_id=oc_logged" in log_text
        assert "message_id=msg_logged" in log_text
        assert "session_key=agent:main:feishu:group:oc_logged" in log_text
        assert "description=dangerous deploy" in log_text
        assert "command=" in log_text
        assert raw_token in log_text

    @pytest.mark.asyncio
    async def test_stores_approval_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
            )

        assert len(adapter._approval_state) == 1
        approval_id = list(adapter._approval_state.keys())[0]
        state = adapter._approval_state[approval_id]
        assert state["session_key"] == "my-session-key"
        assert state["message_id"] == "msg_002"
        assert state["chat_id"] == "oc_12345"

    @pytest.mark.asyncio
    async def test_stores_requester_identity_from_metadata_source(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_requester"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
                metadata={
                    "source": {
                        "user_id": "ou_requester",
                        "user_id_alt": "u_requester",
                    },
                },
            )

        state = next(iter(adapter._approval_state.values()))
        assert state["requester_open_id"] == "ou_requester"
        assert state["requester_user_id"] == "u_requester"

    @pytest.mark.asyncio
    async def test_stores_requester_union_id_separately_from_user_id(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_requester_union"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
                metadata={
                    "source": {
                        "user_id": "ou_requester",
                        "user_id_alt": "on_requester",
                    },
                },
            )

        state = next(iter(adapter._approval_state.values()))
        assert state["requester_open_id"] == "ou_requester"
        assert state["requester_union_id"] == "on_requester"
        assert "requester_user_id" not in state

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._client = None
        result = await adapter.send_exec_approval(
            chat_id="oc_12345", command="ls", session_key="s"
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_truncates_long_command(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_003"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            long_cmd = "x" * 5000
            await adapter.send_exec_approval(
                chat_id="oc_12345", command=long_cmd, session_key="s"
            )

        card = json.loads(mock_send.call_args[1]["payload"])
        content = card["elements"][0]["content"]
        assert "..." in content
        assert len(content) < 5000

    @pytest.mark.asyncio
    async def test_multiple_approvals_get_unique_ids(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_x"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_1", command="cmd1", session_key="s1"
            )
            await adapter.send_exec_approval(
                chat_id="oc_2", command="cmd2", session_key="s2"
            )

        assert len(adapter._approval_state) == 2
        ids = list(adapter._approval_state.keys())
        assert ids[0] != ids[1]


# ===========================================================================
# send_update_prompt — interactive card with buttons
# ===========================================================================

class TestFeishuUpdatePrompt:
    """Test send_update_prompt sends an interactive card."""

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_up_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Restore stashed changes after update?",
                default="y",
                session_key="agent:main:feishu:group:oc_12345",
                metadata={"thread_id": "th_1"},
            )

        assert result.success is True
        assert result.message_id == "msg_up_001"

        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"
        assert kwargs["metadata"] == {"thread_id": "th_1"}

        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "Restore stashed changes after update?" in card["elements"][0]["content"]
        assert "Default: `y`" in card["elements"][0]["content"]
        actions = card["elements"][1]["actions"]
        assert [a["value"]["hermes_update_prompt_action"] for a in actions] == ["y", "n"]

    @pytest.mark.asyncio
    async def test_stores_prompt_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_up_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Continue update?",
                session_key="my-session-key",
            )

        assert len(adapter._update_prompt_state) == 1
        prompt_id = list(adapter._update_prompt_state.keys())[0]
        state = adapter._update_prompt_state[prompt_id]
        assert state["session_key"] == "my-session-key"
        assert state["message_id"] == "msg_up_002"
        assert state["chat_id"] == "oc_12345"

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._client = None
        result = await adapter.send_update_prompt(
            chat_id="oc_12345",
            prompt="Continue update?",
            session_key="s",
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_send_failure_returns_error(self):
        adapter = _make_adapter()
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            side_effect=TimeoutError("timed out"),
        ):
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Continue update?",
                session_key="s",
            )

        assert result.success is False
        assert "timed out" in (result.error or "")


# ===========================================================================
# _resolve_approval — approval state pop + gateway resolution
# ===========================================================================

class TestResolveApproval:
    """Test _resolve_approval pops state and calls resolve_gateway_approval."""

    @pytest.mark.asyncio
    async def test_resolves_once(self):
        adapter = _make_adapter()
        adapter._approval_state[1] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_001",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(1, "once", "Norbert", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("agent:main:feishu:group:oc_12345", "once")
        assert 1 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_resolves_deny(self):
        adapter = _make_adapter()
        adapter._approval_state[2] = {
            "session_key": "some-session",
            "message_id": "msg_002",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(2, "deny", "Alice", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("some-session", "deny")

    @pytest.mark.asyncio
    async def test_resolves_session(self):
        adapter = _make_adapter()
        adapter._approval_state[3] = {
            "session_key": "sess-3",
            "message_id": "msg_003",
            "chat_id": "oc_99",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(3, "session", "Bob", open_id="ou_user1", chat_id="oc_99")

        mock_resolve.assert_called_once_with("sess-3", "session")

    @pytest.mark.asyncio
    async def test_resolves_always(self):
        adapter = _make_adapter()
        adapter._approval_state[4] = {
            "session_key": "sess-4",
            "message_id": "msg_004",
            "chat_id": "oc_55",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(4, "always", "Carol", open_id="ou_user1", chat_id="oc_55")

        mock_resolve.assert_called_once_with("sess-4", "always")

    @pytest.mark.asyncio
    async def test_already_resolved_drops_silently(self):
        adapter = _make_adapter()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(99, "once", "Nobody", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_click_does_not_resolve(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_admin"}
        adapter._approval_state[5] = {
            "session_key": "sess-5",
            "message_id": "msg_005",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(5, "once", "Mallory", open_id="ou_intruder", chat_id="oc_12345")

        mock_resolve.assert_not_called()
        assert 5 in adapter._approval_state

    @pytest.mark.asyncio
    async def test_chat_mismatch_does_not_resolve(self):
        adapter = _make_adapter()
        adapter._approval_state[6] = {
            "session_key": "sess-6",
            "message_id": "msg_006",
            "chat_id": "oc_expected",
        }

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(6, "session", "Norbert", open_id="ou_user1", chat_id="oc_wrong")

        mock_resolve.assert_not_called()
        assert 6 in adapter._approval_state

# ===========================================================================
# _handle_card_action_event — non-approval card actions
# ===========================================================================

class TestNonApprovalCardAction:
    """Non-approval card actions should still route as synthetic commands."""

    @pytest.mark.asyncio
    async def test_routes_as_synthetic_command(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_normal",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(adapter, "get_chat_info", new_callable=AsyncMock, return_value={"name": "Test Chat"}),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        mock_handle.assert_called_once()
        event = mock_handle.call_args[0][0]
        assert "/card button" in event.text


# ===========================================================================
# _on_card_action_trigger — inline card response for approval actions
# ===========================================================================

class _FakeCallBackCard:
    def __init__(self):
        self.type = None
        self.data = None


class _FakeP2Response:
    def __init__(self):
        self.card = None


@pytest.fixture(autouse=False)
def _patch_callback_card_types(monkeypatch):
    """Provide real-ish P2CardActionTriggerResponse / CallBackCard for tests."""
    monkeypatch.setattr(feishu_module, "P2CardActionTriggerResponse", _FakeP2Response)
    monkeypatch.setattr(feishu_module, "CallBackCard", _FakeCallBackCard)


class TestCardActionCallbackResponse:
    """Test that _on_card_action_trigger returns updated card inline."""

    def test_approval_action_schedules_resolution(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._approval_state[1] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_001",
            "chat_id": "oc_12345",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 1},
            open_id="random_user",
        ).event
        captured = {}

        def _capture_submit(submit_loop, coro):
            captured["loop"] = submit_loop
            captured["qualname"] = coro.cr_code.co_name
            coro.close()
            return True

        with patch.object(adapter, "_submit_on_loop", side_effect=_capture_submit) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response is not None
        assert response.card is not None
        assert response.card.data["header"]["template"] == "green"
        assert "Approved once" in response.card.data["header"]["title"]["content"]
        assert captured == {"loop": loop, "qualname": "_resolve_approval"}
        assert 1 in adapter._approval_state
        mock_submit.assert_called_once()

    def test_unauthorized_click_does_not_resolve_or_pop_approval(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._approval_state[10] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_010",
            "chat_id": "oc_12345",
            "requester_open_id": "ou_requester",
            "requester_user_id": "u_requester",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 10},
            open_id="ou_intruder",
            user_id="u_intruder",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response is not None
        assert response.card is not None
        assert response.card.data["header"]["template"] == "orange"
        assert "only the original requester or bot admins" in response.card.data["elements"][0]["content"].lower()
        assert 10 in adapter._approval_state
        mock_submit.assert_not_called()

    def test_original_requester_click_resolves_approval(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._approval_state[11] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_011",
            "chat_id": "oc_12345",
            "requester_open_id": "ou_requester",
            "requester_user_id": "u_requester",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "approve_session", "approval_id": 11},
            open_id="ou_requester",
            user_id="u_different",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response.card.data["header"]["template"] == "green"
        assert "Approved for session" in response.card.data["header"]["title"]["content"]
        assert 11 in adapter._approval_state
        mock_submit.assert_called_once()

    def test_original_requester_click_resolves_by_union_id(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._approval_state[13] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_013",
            "chat_id": "oc_12345",
            "requester_open_id": "ou_requester",
            "requester_user_id": "u_requester",
            "requester_union_id": "on_requester",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "approve_session", "approval_id": 13},
            open_id="ou_different",
            user_id="u_different",
            union_id="on_requester",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response.card.data["header"]["template"] == "green"
        assert "Approved for session" in response.card.data["header"]["title"]["content"]
        assert 13 in adapter._approval_state
        mock_submit.assert_called_once()

    def test_bot_admin_click_resolves_approval(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._admins = {"u_admin"}
        adapter._approval_state[12] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_012",
            "chat_id": "oc_12345",
            "requester_open_id": "ou_requester",
            "requester_user_id": "u_requester",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "deny", "approval_id": 12},
            open_id="ou_admin",
            user_id="u_admin",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response.card.data["header"]["template"] == "red"
        assert "Denied" in response.card.data["header"]["title"]["content"]
        assert 12 in adapter._approval_state
        mock_submit.assert_called_once()

    def test_bot_admin_click_resolves_by_union_id(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._admins = {"on_admin"}
        adapter._approval_state[14] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_014",
            "chat_id": "oc_12345",
            "requester_open_id": "ou_requester",
            "requester_user_id": "u_requester",
            "requester_union_id": "on_requester",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "deny", "approval_id": 14},
            open_id="ou_admin",
            user_id="u_admin",
            union_id="on_admin",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response.card.data["header"]["template"] == "red"
        assert "Denied" in response.card.data["header"]["title"]["content"]
        assert 14 in adapter._approval_state
        mock_submit.assert_called_once()

    def test_nonmatching_union_id_is_rejected(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._approval_state[15] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_015",
            "chat_id": "oc_12345",
            "requester_union_id": "on_requester",
        }
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 15},
            open_id="ou_intruder",
            user_id="u_intruder",
            union_id="on_intruder",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response.card.data["header"]["template"] == "orange"
        assert "only the original requester or bot admins" in response.card.data["elements"][0]["content"].lower()
        assert 15 in adapter._approval_state
        mock_submit.assert_not_called()

    def test_stale_approval_click_returns_already_resolved_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        loop = MagicMock()
        event = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 99},
            open_id="ou_late_clicker",
            user_id="u_late_clicker",
        ).event

        with patch.object(adapter, "_submit_on_loop", side_effect=_close_adapter_submitted_coro) as mock_submit:
            response = adapter._handle_approval_card_action(
                event=event,
                action_value=event.action.value,
                loop=loop,
            )

        assert response is not None
        assert response.card is not None
        assert response.card.data["header"]["template"] == "grey"
        assert "Already resolved" in response.card.data["header"]["title"]["content"]
        assert "no longer pending" in response.card.data["elements"][0]["content"].lower()
        mock_submit.assert_not_called()

    def test_drops_action_when_loop_not_ready(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = None
        data = _make_card_action_data({"hermes_action": "approve_once", "approval_id": 1})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_returns_card_for_approve_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._approval_state[1] = {
            "session_key": "sess-1",
            "message_id": "msg-1",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 1},
            open_id="ou_bob",
        )
        adapter._approval_state[1] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_001",
            "chat_id": "oc_12345",
        }
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        card = response.card.data
        assert card["header"]["template"] == "green"
        assert "Approved once" in card["header"]["title"]["content"]
        assert "Bob" in card["elements"][0]["content"]

    def test_returns_card_for_deny_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_user1"}
        adapter._approval_state[2] = {
            "session_key": "sess-2",
            "message_id": "msg-2",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "deny", "approval_id": 2},
        )
        adapter._approval_state[2] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_002",
            "chat_id": "oc_12345",
        }

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response.card is not None
        card = response.card.data
        assert card["header"]["template"] == "red"
        assert "Denied" in card["header"]["title"]["content"]

    def test_ignores_missing_approval_id(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"hermes_action": "approve_once"})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_no_card_for_non_approval_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"some_other": "value"})

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None

    def test_falls_back_to_open_id_when_name_not_cached(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_unknown"}
        adapter._approval_state[3] = {
            "session_key": "sess-3",
            "message_id": "msg-3",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_session", "approval_id": 3},
            open_id="ou_unknown",
        )
        adapter._approval_state[3] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_003",
            "chat_id": "oc_12345",
        }

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        card = response.card.data
        assert "ou_unknown" in card["elements"][0]["content"]

    def test_ignores_expired_cached_name(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_expired"}
        adapter._approval_state[4] = {
            "session_key": "sess-4",
            "message_id": "msg-4",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 4},
            open_id="ou_expired",
        )
        adapter._approval_state[4] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_004",
            "chat_id": "oc_12345",
        }
        adapter._sender_name_cache["ou_expired"] = ("Old Name", 1)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        card = response.card.data
        assert "Old Name" not in card["elements"][0]["content"]
        assert "ou_expired" in card["elements"][0]["content"]

    def test_rejects_approval_click_from_unauthorized_user(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_allowed"}
        adapter._approval_state[5] = {
            "session_key": "sess-5",
            "message_id": "msg-5",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 5},
            open_id="ou_attacker",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.data["header"]["template"] == "orange"
        assert "only the original requester or bot admins" in response.card.data["elements"][0]["content"].lower()
        mock_submit.assert_not_called()

    def test_rejects_approval_click_when_callback_chat_mismatches(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._approval_state[6] = {
            "session_key": "sess-6",
            "message_id": "msg-6",
            "chat_id": "oc_expected",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 6},
            chat_id="oc_mismatch",
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.data["header"]["template"] == "orange"
        assert "only the original requester or bot admins" in response.card.data["elements"][0]["content"].lower()
        mock_submit.assert_not_called()

    def test_returns_card_for_update_prompt_yes(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_003",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
            open_id="ou_bob",
        )
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        card = response.card.data
        assert card["header"]["template"] == "green"
        assert "answered: Yes" in card["header"]["title"]["content"]
        assert "Bob" in card["elements"][0]["content"]

    def test_returns_card_for_update_prompt_no(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_user1"}
        adapter._update_prompt_state[2] = {
            "session_key": "sess-up-2",
            "message_id": "msg_up_004",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "n", "update_prompt_id": 2},
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        card = response.card.data
        assert card["header"]["template"] == "red"
        assert "answered: No" in card["header"]["title"]["content"]

    def test_ignores_missing_update_prompt_id(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data({"hermes_update_prompt_action": "y"})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_already_resolved_update_prompt_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 99},
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_update_prompt_schedule_failure_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_user1"}
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_005",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=RuntimeError("loop closed")):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None

    def test_update_prompt_unauthorized_operator_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_006",
            "chat_id": "oc_12345",
        }
        adapter._allowed_group_users = {"ou_allowed"}
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
            open_id="ou_intruder",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_update_prompt_empty_allowlists_fail_closed(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[7] = {
            "session_key": "sess-up-7",
            "message_id": "msg_up_007",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 7},
            open_id="ou_intruder",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        assert 7 in adapter._update_prompt_state
        mock_submit.assert_not_called()

    def test_update_prompt_chat_mismatch_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._update_prompt_state[8] = {
            "session_key": "sess-up-8",
            "message_id": "msg_up_008",
            "chat_id": "oc_expected",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 8},
            chat_id="oc_mismatch",
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        assert 8 in adapter._update_prompt_state
        mock_submit.assert_not_called()


class TestResolveUpdatePrompt:
    """Test update prompt resolution persists the response file."""

    @pytest.mark.asyncio
    async def test_writes_response_file(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_003",
            "chat_id": "oc_12345",
        }

        await adapter._resolve_update_prompt(1, "y", "Alice")

        assert (tmp_path / ".hermes" / ".update_response").read_text() == "y"
        assert 1 not in adapter._update_prompt_state

    @pytest.mark.asyncio
    async def test_overwrites_existing_response_file(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / ".update_response").write_text("n")
        adapter._update_prompt_state[2] = {
            "session_key": "sess-up-2",
            "message_id": "msg_up_004",
            "chat_id": "oc_12345",
        }

        await adapter._resolve_update_prompt(2, "y", "Alice")

        assert (home / ".update_response").read_text() == "y"

    @pytest.mark.asyncio
    async def test_unknown_prompt_id_drops_silently(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()

        await adapter._resolve_update_prompt(99, "n", "Nobody")

        assert not (tmp_path / ".hermes" / ".update_response").exists()

    @pytest.mark.asyncio
    async def test_chat_mismatch_does_not_write_response_file(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        adapter._allowed_group_users = {"ou_bob"}
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()
        adapter._update_prompt_state[10] = {
            "session_key": "sess-up-10",
            "message_id": "msg_up_010",
            "chat_id": "oc_expected",
        }

        await adapter._resolve_update_prompt(
            10,
            "y",
            "Bob",
            open_id="ou_bob",
            chat_id="oc_wrong",
        )

        assert not (tmp_path / ".hermes" / ".update_response").exists()
        assert 10 in adapter._update_prompt_state
