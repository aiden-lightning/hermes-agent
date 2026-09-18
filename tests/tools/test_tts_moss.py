"""Tests for the MOSS-TTS-Nano local provider in tools/tts_tool.py."""

import json
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)


class TestGenerateMossTts:
    def test_import_uses_bundled_runtime_path(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        repo_root = tmp_path / "moss_tts_nano_repo"
        repo_root.mkdir()
        module_path = repo_root / "onnx_tts_runtime.py"
        module_path.write_text(
            "class OnnxTtsRuntime:\n"
            "    pass\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(_local, "_moss_repo_root", lambda: repo_root)
        sys_modules = __import__("sys").modules
        sys_modules["onnx_tts_runtime"] = type("PoisonedModule", (), {"OnnxTtsRuntime": object})()

        runtime_cls = _local._import_moss_onnx_runtime()

        assert runtime_cls.__name__ == "OnnxTtsRuntime"
        assert runtime_cls.__module__ == "_hermes_moss_onnx_tts_runtime"

    def test_successful_mp3_generation_uses_default_reference_audio(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()

        def fake_synthesize(**kwargs):
            wav_path = kwargs["output_audio_path"]
            with open(wav_path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WAVEfmt fake")
            return {"audio_path": wav_path, "sample_rate": 24000}

        fake_runtime.synthesize.side_effect = fake_synthesize
        fake_runtime_cls = MagicMock(return_value=fake_runtime)

        ffmpeg_calls = []

        def fake_run(cmd, check=False, timeout=None, **kwargs):
            ffmpeg_calls.append(cmd)
            out_path = cmd[-1]
            with open(out_path, "wb") as f:
                f.write(b"fake-mp3-data")
            return MagicMock(returncode=0)

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        monkeypatch.setattr(_local.subprocess, "run", fake_run)

        output_path = str(tmp_path / "out.mp3")
        result = _local._generate_moss_tts("你好，世界", output_path, {})

        assert result == output_path
        assert (tmp_path / "out.mp3").exists()
        fake_runtime_cls.assert_called_once()
        synth_kwargs = fake_runtime.synthesize.call_args.kwargs
        assert synth_kwargs["text"] == "你好，世界"
        assert synth_kwargs["prompt_audio_path"] == _local._default_moss_ref_audio()
        assert synth_kwargs["output_audio_path"].endswith(".wav")
        assert ffmpeg_calls[0][0] == "/usr/bin/ffmpeg"

    def test_failed_ffmpeg_conversion_cleans_up_temp_wav(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()

        def fake_synthesize(**kwargs):
            wav_path = kwargs["output_audio_path"]
            with open(wav_path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WAVEfmt fake")

        fake_runtime.synthesize.side_effect = fake_synthesize
        fake_runtime_cls = MagicMock(return_value=fake_runtime)

        def fake_run(*args, **kwargs):
            raise _local.subprocess.CalledProcessError(1, args[0])

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        monkeypatch.setattr(_local.subprocess, "run", fake_run)

        output_path = tmp_path / "out.mp3"
        with pytest.raises(_local.subprocess.CalledProcessError):
            _local._generate_moss_tts("hello", str(output_path), {})

        assert not (tmp_path / "out.wav").exists()

    def test_custom_config_is_passed_to_runtime(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()

        def fake_synthesize(**kwargs):
            wav_path = kwargs["output_audio_path"]
            with open(wav_path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WAVEfmt fake")
            return {"audio_path": wav_path, "sample_rate": 24000}

        fake_runtime.synthesize.side_effect = fake_synthesize
        fake_runtime_cls = MagicMock(return_value=fake_runtime)

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: None)

        config = {
            "moss": {
                "model_dir": str(tmp_path / "models"),
                "prompt_audio": str(tmp_path / "speaker.wav"),
                "cpu_threads": 2,
                "sample_mode": "greedy",
                "do_sample": False,
                "streaming": True,
                "max_new_frames": 111,
                "voice_clone_max_text_tokens": 33,
                "enable_wetext_processing": False,
                "enable_normalize_tts_text": False,
                "seed": 7,
            }
        }
        (tmp_path / "speaker.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt fake")
        output_path = str(tmp_path / "out.ogg")
        result = _local._generate_moss_tts("hello", output_path, config)

        assert result == output_path
        fake_runtime_cls.assert_called_once_with(
            model_dir=str(tmp_path / "models"),
            thread_count=2,
            max_new_frames=111,
            do_sample=False,
            sample_mode="greedy",
        )
        synth_kwargs = fake_runtime.synthesize.call_args.kwargs
        assert synth_kwargs["prompt_audio_path"] == str(tmp_path / "speaker.wav")
        assert synth_kwargs["streaming"] is True
        assert synth_kwargs["voice_clone_max_text_tokens"] == 33
        assert synth_kwargs["enable_wetext"] is False
        assert synth_kwargs["enable_normalize_tts_text"] is False
        assert synth_kwargs["seed"] == 7
        assert (tmp_path / "out.ogg").exists()

    def test_ogg_conversion_uses_opus_codec(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()

        def fake_synthesize(**kwargs):
            wav_path = kwargs["output_audio_path"]
            with open(wav_path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WAVEfmt fake")

        fake_runtime.synthesize.side_effect = fake_synthesize
        fake_runtime_cls = MagicMock(return_value=fake_runtime)
        ffmpeg_calls = []

        def fake_run(cmd, check=False, timeout=None, **kwargs):
            ffmpeg_calls.append(cmd)
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-ogg-data")
            return MagicMock(returncode=0)

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        monkeypatch.setattr(_local.subprocess, "run", fake_run)

        _local._generate_moss_tts("hello", str(tmp_path / "out.ogg"), {})

        assert "libopus" in ffmpeg_calls[0]


class TestDispatcherBranch:
    def test_moss_not_installed_returns_helpful_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"tts": {"provider": "moss"}}),
            encoding="utf-8",
        )

        from tools import tts_tool as _tts

        monkeypatch.setattr(_tts, "_import_moss_onnx_runtime", lambda: (_ for _ in ()).throw(ImportError("no moss")))

        result = json.loads(_tts.text_to_speech_tool(text="Hello"))
        assert result["success"] is False
        assert "moss" in result["error"].lower()
        assert "moss-tts-nano" in result["error"].lower()

    def test_ogg_output_without_ffmpeg_is_not_marked_voice_compatible(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"tts": {"provider": "moss"}}),
            encoding="utf-8",
        )

        from tools import tts_tool as _tts

        def fake_generate(_text, out_path, _cfg):
            with open(out_path, "wb") as f:
                f.write(b"not-opus")
            return out_path

        monkeypatch.setattr(_tts, "_import_moss_onnx_runtime", lambda: object)
        monkeypatch.setattr(_tts, "_generate_moss_tts", fake_generate)
        monkeypatch.setattr(_tts, "_has_ffmpeg", lambda: False)

        result = json.loads(_tts.text_to_speech_tool(text="Hello", output_path=str(tmp_path / "out.ogg")))

        assert result["success"] is True
        assert result["voice_compatible"] is False
        assert "[[audio_as_voice]]" not in result["media_tag"]


class TestRequirements:
    def test_check_moss_available_requires_default_reference_audio(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: object)
        monkeypatch.setattr(_local, "_default_moss_ref_audio", lambda: str(tmp_path / "missing.wav"))

        assert _local._check_moss_available() is False

    def test_check_tts_requirements_accepts_configured_moss_provider(self, monkeypatch):
        """A configured MOSS provider is satisfied by its own availability check alone.

        ``check_tts_requirements`` resolves ONLY the configured provider (unrelated credentials
        never make another provider look usable), so the other backends are irrelevant here — they
        are knocked out to prove MOSS is what carries the result."""
        from tools import tts_tool as _tts

        monkeypatch.setattr(_tts, "_load_tts_config", lambda: {"provider": "moss"})
        monkeypatch.setattr(_tts, "_import_edge_tts", lambda: (_ for _ in ()).throw(ImportError("no edge")))
        monkeypatch.setattr(_tts, "_check_neutts_available", lambda: False)
        monkeypatch.setattr(_tts, "_check_kittentts_available", lambda: False)

        monkeypatch.setattr(_tts, "_check_moss_available", lambda: False)
        assert _tts.check_tts_requirements() is False

        monkeypatch.setattr(_tts, "_check_moss_available", lambda: True)
        assert _tts.check_tts_requirements() is True


class TestMossOutputValidation:
    """Tests for 8a79a070 output validation fixes."""

    def test_synthesize_without_output_file_raises_runtime_error(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()
        fake_runtime.synthesize.return_value = None
        fake_runtime_cls = MagicMock(return_value=fake_runtime)

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: None)

        output_path = str(tmp_path / "out.mp3")
        with pytest.raises(RuntimeError, match="synthesize completed but output file was not created"):
            _local._generate_moss_tts("hello", output_path, {})

    def test_ffmpeg_failure_raises_runtime_error(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()

        def fake_synthesize(**kwargs):
            wav_path = kwargs["output_audio_path"]
            with open(wav_path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WAVEfmt fake")

        fake_runtime.synthesize.side_effect = fake_synthesize
        fake_runtime_cls = MagicMock(return_value=fake_runtime)

        def fake_run(cmd, check=False, timeout=None, **kwargs):
            # Simulate ffmpeg running but NOT creating output (e.g. disk full)
            return MagicMock(returncode=0)

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        monkeypatch.setattr(_local.subprocess, "run", fake_run)

        output_path = str(tmp_path / "out.mp3")
        with pytest.raises(RuntimeError, match="ffmpeg conversion failed"):
            _local._generate_moss_tts("hello", output_path, {})

    def test_missing_final_output_raises_runtime_error(self, tmp_path, monkeypatch):
        from tools import tts_tool_local as _local

        fake_runtime = MagicMock()

        def fake_synthesize(**kwargs):
            wav_path = kwargs["output_audio_path"]
            with open(wav_path, "wb") as f:
                f.write(b"RIFF\x00\x00\x00\x00WAVEfmt fake")

        fake_runtime.synthesize.side_effect = fake_synthesize
        fake_runtime_cls = MagicMock(return_value=fake_runtime)

        def fake_run(cmd, check=False, timeout=None, **kwargs):
            out_path = cmd[-1]
            with open(out_path, "wb") as f:
                f.write(b"fake-mp3-data")
            return MagicMock(returncode=0)

        monkeypatch.setattr(_local, "_import_moss_onnx_runtime", lambda: fake_runtime_cls)
        monkeypatch.setattr(_local.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        monkeypatch.setattr(_local.subprocess, "run", fake_run)

        # When finally block removes the temp wav, also nuke the output file
        # to simulate a race condition / external deletion.
        original_remove = os.remove
        output_path = str(tmp_path / "out.mp3")

        def fake_remove(path):
            if str(path).endswith(".wav"):
                try:
                    original_remove(output_path)
                except FileNotFoundError:
                    pass
            original_remove(path)

        monkeypatch.setattr(_local.os, "remove", fake_remove)

        with pytest.raises(RuntimeError, match="TTS output file missing after generation"):
            _local._generate_moss_tts("hello", output_path, {})
