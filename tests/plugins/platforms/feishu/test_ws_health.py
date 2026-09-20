"""Feishu WebSocket health tracking and stale-link recovery."""

import asyncio
import concurrent.futures
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugins.platforms.feishu import adapter as feishu_adapter


def _inject_fake_lark_module(monkeypatch):
    root = types.ModuleType("lark_oapi")
    ws = types.ModuleType("lark_oapi.ws")
    client = types.ModuleType("lark_oapi.ws.client")
    client.loop = SimpleNamespace(name="sdk-default-loop")
    client.websockets = SimpleNamespace(connect=MagicMock())
    root.ws = ws
    ws.client = client
    monkeypatch.setitem(sys.modules, "lark_oapi", root)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", ws)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", client)
    monkeypatch.setattr(feishu_adapter, "_WS_ISOLATION_INSTALLED", False)
    return client


def test_any_inbound_frame_refreshes_websocket_activity(monkeypatch):
    client_module = _inject_fake_lark_module(monkeypatch)
    handled = []

    class FakeClient:
        async def _handle_message(self, message):
            handled.append(message)

        def start(self):
            client_module.loop.run_until_complete(self._handle_message(b"frame"))

    client = FakeClient()
    original_handler = client._handle_message
    adapter = SimpleNamespace(
        _ws_thread_loop=None,
        _ws_reconnect_nonce=3,
        _ws_reconnect_interval=10,
        _ws_ping_interval=30,
        _ws_ping_timeout=60,
        _ws_last_activity=0.0,
    )

    feishu_adapter._run_official_feishu_ws_client(client, adapter)

    assert handled == [b"frame"]
    assert adapter._ws_last_activity > 0
    assert client._handle_message == original_handler


def test_health_watchdog_closes_stale_socket_and_stops_sdk_loop(monkeypatch):
    class FakeLoop:
        def __init__(self):
            self.stop_requested = False

        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback):
            assert callback == self.stop
            self.stop_requested = True

        def stop(self):
            pass

    async def scenario():
        sleep_calls = 0

        async def fast_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        def submit(coro, _loop):
            result = concurrent.futures.Future()
            task = asyncio.create_task(coro)

            def finish(completed):
                try:
                    result.set_result(completed.result())
                except BaseException as exc:
                    result.set_exception(exc)

            task.add_done_callback(finish)
            return result

        class FakeClient:
            _auto_reconnect = True

            def __init__(self):
                self.disconnect_calls = 0

            async def _disconnect(self):
                self.disconnect_calls += 1

        loop = FakeLoop()
        client = FakeClient()
        adapter = SimpleNamespace(
            _running=True,
            _connection_mode="websocket",
            _ws_client=client,
            _ws_thread_loop=loop,
            _ws_last_activity=feishu_adapter.time.monotonic() - 10,
            _ws_health_restart_inflight=False,
            _websocket_health_timeout=lambda: 1.0,
        )
        monkeypatch.setattr(feishu_adapter.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(feishu_adapter.asyncio, "run_coroutine_threadsafe", submit)

        try:
            await feishu_adapter.FeishuAdapter._watch_websocket_health(adapter)
        except asyncio.CancelledError:
            pass
        return client, loop, adapter

    client, loop, adapter = asyncio.run(scenario())
    assert client.disconnect_calls == 1
    assert client._auto_reconnect is False
    assert loop.stop_requested is True
    assert adapter._ws_health_restart_inflight is True
