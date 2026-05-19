"""
Tests for cross-platform audio/voice media routing.

These tests pin the expected delivery path for audio media files across
Telegram (where Bot-API sendAudio only accepts MP3/M4A and .ogg/.opus
only renders as a voice bubble when explicitly flagged) and via
``GatewayRunner._deliver_media_from_response``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    is_outbound_media_path_allowed,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _MediaRoutingAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content=None, **kwargs):
        return SendResult(success=True, message_id="text")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _event(thread_id=None):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        thread_id=thread_id,
    )
    return MessageEvent(
        text="make speech",
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg-1",
    )


def test_outbound_media_allowlist_requires_real_directory_containment():
    assert is_outbound_media_path_allowed("/tmp/real.png")
    assert not is_outbound_media_path_allowed("/tmpfoo/real.png")
    assert not is_outbound_media_path_allowed("/etc/passwd")


def test_outbound_media_allowlist_does_not_include_generic_hermes_cache(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    cache_media = hermes_home / "cache" / "images" / "generated.png"
    cache_media.parent.mkdir(parents=True)
    cache_media.write_bytes(b"image")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_OUTBOUND_MEDIA_ALLOWLIST", raising=False)

    assert not is_outbound_media_path_allowed(str(cache_media))


@pytest.mark.asyncio
async def test_base_adapter_routes_telegram_flac_media_tag_to_document_sender():
    adapter = _MediaRoutingAdapter()
    event = _event()
    adapter._message_handler = AsyncMock(return_value="MEDIA:/tmp/speech.flac")
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="voice"))
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc"))

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send_document.assert_awaited_once_with(
        chat_id="chat-1",
        file_path="/tmp/speech.flac",
        metadata=None,
    )
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_adapter_blocks_sensitive_media_tag_before_document_send(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    secret = hermes_home / ".env"
    secret.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    adapter = _MediaRoutingAdapter()
    event = _event()
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{secret}")
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc"))
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="voice"))
    adapter.send_video = AsyncMock(return_value=SendResult(success=True, message_id="video"))
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True, message_id="images"))

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send_document.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()
    adapter.send_video.assert_not_awaited()
    adapter.send_multiple_images.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_adapter_allows_configured_outbound_media_path(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    allowed_dir = tmp_path / "allowed-media"
    allowed_dir.mkdir()
    media = allowed_dir / "report.pdf"
    media.write_bytes(b"%PDF-1.4\n")
    (hermes_home / "config.yaml").write_text(
        "gateway:\n  outbound_media_allowlist:\n    - " + repr(str(allowed_dir)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    adapter = _MediaRoutingAdapter()
    event = _event()
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{media}")
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc"))

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send_document.assert_awaited_once_with(
        chat_id="chat-1",
        file_path=str(media),
        metadata=None,
    )


@pytest.mark.asyncio
async def test_base_adapter_routes_non_voice_telegram_ogg_media_tag_to_document_sender():
    adapter = _MediaRoutingAdapter()
    event = _event()
    adapter._message_handler = AsyncMock(return_value="MEDIA:/tmp/speech.ogg")
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="voice"))
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc"))

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send_document.assert_awaited_once_with(
        chat_id="chat-1",
        file_path="/tmp/speech.ogg",
        metadata=None,
    )
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_adapter_routes_voice_tagged_telegram_ogg_media_tag_to_voice_sender():
    adapter = _MediaRoutingAdapter()
    event = _event()
    adapter._message_handler = AsyncMock(
        return_value="[[audio_as_voice]]\nMEDIA:/tmp/speech.ogg"
    )
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="voice"))
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc"))

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send_voice.assert_awaited_once_with(
        chat_id="chat-1",
        audio_path="/tmp/speech.ogg",
        metadata=None,
    )
    adapter.send_document.assert_not_awaited()


def _fake_runner(thread_meta):
    """Build a fake GatewayRunner-like object with the helper methods needed by
    _deliver_media_from_response."""
    runner = SimpleNamespace(
        _thread_metadata_for_source=lambda source, anchor=None: thread_meta,
        _reply_anchor_for_event=lambda event: None,
    )
    return runner


@pytest.mark.asyncio
async def test_streaming_delivery_routes_telegram_flac_media_tag_to_document_sender():
    event = _event(thread_id="topic-1")
    adapter = SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="image")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="video")),
    )

    await GatewayRunner._deliver_media_from_response(
        _fake_runner({"thread_id": "topic-1"}),
        "MEDIA:/tmp/speech.flac",
        event,
        adapter,
    )

    adapter.send_document.assert_awaited_once_with(
        chat_id="chat-1",
        file_path="/tmp/speech.flac",
        metadata={"thread_id": "topic-1"},
    )
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_delivery_blocks_sensitive_media_tag(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    secret = hermes_home / ".env"
    secret.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    event = _event(thread_id="topic-1")
    adapter = SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_multiple_images=AsyncMock(return_value=SendResult(success=True, message_id="images")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="video")),
    )

    await GatewayRunner._deliver_media_from_response(
        _fake_runner({"thread_id": "topic-1"}),
        f"MEDIA:{secret}",
        event,
        adapter,
    )

    adapter.send_document.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()
    adapter.send_multiple_images.assert_not_awaited()
    adapter.send_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_delivery_routes_non_voice_telegram_ogg_media_tag_to_document_sender():
    event = _event(thread_id="topic-1")
    adapter = SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="image")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="video")),
    )

    await GatewayRunner._deliver_media_from_response(
        _fake_runner({"thread_id": "topic-1"}),
        "MEDIA:/tmp/speech.ogg",
        event,
        adapter,
    )

    adapter.send_document.assert_awaited_once_with(
        chat_id="chat-1",
        file_path="/tmp/speech.ogg",
        metadata={"thread_id": "topic-1"},
    )
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_delivery_routes_telegram_mp3_media_tag_to_voice_sender():
    """MP3 audio on Telegram must go through send_voice (which routes to
    sendAudio internally); Telegram accepts MP3 for the audio player."""
    event = _event(thread_id="topic-1")
    adapter = SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="image")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="video")),
    )

    await GatewayRunner._deliver_media_from_response(
        _fake_runner({"thread_id": "topic-1"}),
        "MEDIA:/tmp/speech.mp3",
        event,
        adapter,
    )

    adapter.send_voice.assert_awaited_once_with(
        chat_id="chat-1",
        audio_path="/tmp/speech.mp3",
        metadata={"thread_id": "topic-1"},
    )
    adapter.send_document.assert_not_awaited()
