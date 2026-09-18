"""Local on-device TTS engines for ``tools.tts_tool``: NeuTTS, Piper, KittenTTS, MOSS-TTS-Nano.

All four synthesize WAV natively; :func:`_finalize_wav_output` converts/renames to the requested
container. Piper and KittenTTS keep loaded models in small LRU caches registered in
``_LOCAL_TTS_MODEL_CACHES`` so warm/release can pre-load or drop them. ``_import_piper`` /
``_import_kittentts`` are resolved through the origin module at call time (test monkeypatches).
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from tools.tts_tool_delivery import (
    _OPUS_VOICE_ARGS, _finalize_wav_output, _origin, _remove_quietly, _section, _wav_sidecar_path)

logger = logging.getLogger("tools.tts_tool")

DEFAULT_KITTENTTS_MODEL = "KittenML/kitten-tts-nano-0.8-int8"  # 25MB
DEFAULT_KITTENTTS_VOICE = "Jasper"
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"  # balanced size/quality
_NEUTTS_SAMPLES = Path(__file__).parent / "neutts_samples"

# --- Bounded model caches ---
# Each entry is a whole loaded model (tens of MB); unbounded, one would be pinned per distinct
# voice for the process lifetime. Most sessions use one or two voices; a cold reload is cheap.
_TTS_MODEL_CACHE_MAX = 3

# Provider name -> the cache it populates (warm/release in tts_tool_lifecycle; a new local engine
# adds a row here plus a loader in _local_tts_warmers()). Piper keyed on absolute .onnx path
# (+cuda flag); KittenTTS on model name.
_piper_voice_cache: Dict[str, Any] = {}
_kittentts_model_cache: Dict[str, Any] = {}
_LOCAL_TTS_MODEL_CACHES: Dict[str, Dict[str, Any]] = {
    "piper": _piper_voice_cache, "kittentts": _kittentts_model_cache}


def _tts_cache_get_or_load(cache: Dict[str, Any], key: str, load: Callable[[], Any]) -> Any:
    """Get ``key`` from ``cache`` or load it, LRU-bounded at ``_TTS_MODEL_CACHE_MAX`` (a hit refreshes
    recency via pop + reinsert; eviction only releases the slot, not live references)."""
    if key in cache:
        cache[key] = cache.pop(key)
        return cache[key]
    value = load()
    cache[key] = value
    while len(cache) > _TTS_MODEL_CACHE_MAX:
        cache.pop(next(iter(cache)), None)
    return value


def _run_helper(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, stdin=subprocess.DEVNULL,
    )


# --- NeuTTS (subprocess via tools/neutts_synth.py so the ~500MB model exits after use) ---
def _generate_neutts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    neutts_config = tts_config.get("neutts") or {}
    wav_path = _wav_sidecar_path(output_path)
    cmd = [
        sys.executable, str(Path(__file__).parent / "neutts_synth.py"),
        "--text", text,
        "--out", wav_path,
        "--ref-audio", neutts_config.get("ref_audio", "") or str(_NEUTTS_SAMPLES / "jo.wav"),
        "--ref-text", neutts_config.get("ref_text", "") or str(_NEUTTS_SAMPLES / "jo.txt"),
        "--model", neutts_config.get("model", "neuphonic/neutts-air-q4-gguf"),
        "--device", neutts_config.get("device", "cpu")]
    result = _run_helper(cmd, 120)
    if result.returncode != 0:  # the synth script reports success lines as "OK:" on stderr too
        error_lines = [l for l in result.stderr.strip().splitlines() if not l.startswith("OK:")]
        raise RuntimeError(f"NeuTTS synthesis failed: {chr(10).join(error_lines) or 'unknown error'}")
    return _finalize_wav_output(wav_path, output_path)


# --- Piper (local neural VITS, 44 languages) ---
def _get_piper_voices_dir() -> Path:
    """``<HERMES_HOME>/cache/piper-voices/`` so voice downloads follow profile boundaries."""
    from hermes_constants import get_hermes_dir
    root = Path(get_hermes_dir("cache/piper-voices", "piper_voices_cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_piper_voice_path(voice: str, download_dir: Path) -> str:
    """Resolve *voice* (an .onnx path or a name like ``en_US-lessac-medium``, downloaded into
    *download_dir* on first use) to a concrete .onnx file; RuntimeError when it can't be."""
    voice = voice or DEFAULT_PIPER_VOICE
    candidate = Path(voice).expanduser()
    if candidate.suffix.lower() == ".onnx" and candidate.exists():
        return str(candidate)
    cached = download_dir / f"{voice}.onnx"
    if cached.exists() and (download_dir / f"{voice}.onnx.json").exists():
        return str(cached)
    logger.info("[Piper] Downloading voice '%s' to %s (first use)", voice, download_dir)
    try:
        result = _run_helper(
            [sys.executable, "-m", "piper.download_voices", voice, "--download-dir", str(download_dir)], 300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Piper voice download timed out after 300s for '{voice}'") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "no stderr output"
        raise RuntimeError(f"Piper voice download failed for '{voice}': {stderr[:400]}")
    if not cached.exists():
        raise RuntimeError(
            f"Piper voice download completed but {cached} is missing — "
            f"check voice name (see: https://github.com/OHF-Voice/piper1-gpl/"
            f"blob/main/docs/VOICES.md)")
    return str(cached)


def _load_piper_voice_for_config(tts_config: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """Resolve + load (or fetch from cache) the selected Piper voice -> ``(voice, piper_config)``.
    Shared by synthesis and ``warm_tts_provider`` so a warm-up fills exactly the slot synthesis hits."""
    PiperVoice = _origin()._import_piper()
    piper_config = _section(tts_config, "piper")
    voice_name = piper_config.get("voice") or DEFAULT_PIPER_VOICE
    download_dir = Path(piper_config.get("voices_dir") or _get_piper_voices_dir()).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    use_cuda = bool(piper_config.get("use_cuda", False))
    model_path = _resolve_piper_voice_path(voice_name, download_dir)

    def _load_piper_voice():
        logger.info("[Piper] Loading voice: %s", model_path)
        v = PiperVoice.load(model_path, use_cuda=use_cuda)
        logger.info("[Piper] Voice loaded")
        return v

    # speaker_id is applied per call via syn_config, so one instance serves every speaker.
    cache_key = f"{model_path}::cuda={use_cuda}"
    return _tts_cache_get_or_load(_piper_voice_cache, cache_key, _load_piper_voice), piper_config


_PIPER_ADVANCED_KNOBS = ("length_scale", "noise_scale", "noise_w_scale", "volume", "normalize_audio", "speaker_id")


def _generate_piper_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    import wave
    voice, piper_config = _load_piper_voice_for_config(tts_config)
    # Bad speaker_id drops to 0 (Piper's default); bools are rejected (they'd coerce to 1/0).
    _raw_speaker = piper_config.get("speaker_id", 0)
    speaker_id = _raw_speaker if type(_raw_speaker) is int else 0
    # Only build a SynthesisConfig when an advanced knob is configured, so we don't depend on a
    # newer piper-tts than the user's unless we must.
    syn_config = None
    if any(k in piper_config for k in _PIPER_ADVANCED_KNOBS):
        try:
            from piper import SynthesisConfig  # type: ignore
            syn_config = SynthesisConfig(
                length_scale=float(piper_config.get("length_scale", 1.0)),
                noise_scale=float(piper_config.get("noise_scale", 0.667)),
                noise_w_scale=float(piper_config.get("noise_w_scale", 0.8)),
                volume=float(piper_config.get("volume", 1.0)),
                normalize_audio=bool(piper_config.get("normalize_audio", True)),
                speaker_id=speaker_id)
        except ImportError:
            logger.warning("[Piper] SynthesisConfig not available in this piper-tts version — advanced knobs ignored")
    wav_path = _wav_sidecar_path(output_path)
    with wave.open(wav_path, "wb") as wav_file:
        if syn_config is not None:
            voice.synthesize_wav(text, wav_file, syn_config=syn_config)
        else:
            voice.synthesize_wav(text, wav_file)
    return _finalize_wav_output(wav_path, output_path)


# --- KittenTTS (local ONNX, 25-80MB models, CPU only) ---
def _load_kittentts_model_for_config(tts_config: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """Load (or fetch from cache) the KittenTTS model; returns ``(model, kittentts_config)``."""
    KittenTTS = _origin()._import_kittentts()
    kt_config = _section(tts_config, "kittentts")
    model_name = kt_config.get("model", DEFAULT_KITTENTTS_MODEL)

    def _load_kittentts_model():
        logger.info("[KittenTTS] Loading model: %s", model_name)
        m = KittenTTS(model_name)
        logger.info("[KittenTTS] Model loaded successfully")
        return m

    return _tts_cache_get_or_load(_kittentts_model_cache, model_name, _load_kittentts_model), kt_config


def _generate_kittentts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    model, kt_config = _load_kittentts_model_for_config(tts_config)
    audio = model.generate(  # numpy array at 24kHz
        text, voice=kt_config.get("voice", DEFAULT_KITTENTTS_VOICE),
        speed=kt_config.get("speed", 1.0), clean_text=kt_config.get("clean_text", True))
    import soundfile as sf
    wav_path = _wav_sidecar_path(output_path)
    sf.write(wav_path, audio, 24000)
    return _finalize_wav_output(wav_path, output_path)


# --- MOSS-TTS-Nano (local ONNX CPU, voice cloning from a reference clip) ---
# The runtime ships as a vendored repo (``moss_tts_nano_repo/``), not a pip package: it is loaded
# from its own directory by file path so its sibling modules resolve, without leaving the repo on
# ``sys.path`` afterwards. Unlike the other local engines it can emit Opus directly, so ``.ogg``
# requests convert through ffmpeg's libopus rather than _finalize_wav_output's generic rename.
@contextlib.contextmanager
def _prepend_sys_path(path: Path):
    """Temporarily prepend *path* to ``sys.path`` for local module imports."""
    path_str = str(path)
    inserted = False
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(path_str)


def _moss_repo_root() -> Path:
    """Return the bundled MOSS-TTS-Nano repository path."""
    return Path(__file__).resolve().parent.parent / "moss_tts_nano_repo"


def _import_moss_onnx_runtime():
    """Lazy-load the bundled MOSS ONNX runtime class from its vendored repo.

    Loaded under a private module name so a same-named module already in ``sys.modules`` cannot
    shadow the vendored one."""
    repo_root = _moss_repo_root()
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Bundled MOSS repo not found: {repo_root}")
    module_path = repo_root / "onnx_tts_runtime.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"Bundled MOSS runtime module not found: {module_path}")
    with _prepend_sys_path(repo_root):
        spec = importlib.util.spec_from_file_location("_hermes_moss_onnx_tts_runtime", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load MOSS runtime module from {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    runtime_cls = getattr(module, "OnnxTtsRuntime", None)
    if runtime_cls is None:
        raise ImportError("Bundled MOSS runtime does not export OnnxTtsRuntime")
    return runtime_cls


def _default_moss_ref_audio() -> str:
    """Return path to the bundled default MOSS voice-clone reference audio."""
    return str(_moss_repo_root() / "assets" / "audio" / "zh_1.wav")


def _check_moss_available() -> bool:
    """Whether the bundled MOSS runtime imports AND its default reference clip is present."""
    try:
        _import_moss_onnx_runtime()
        return Path(_default_moss_ref_audio()).is_file()
    except Exception:
        return False


def _coerce_bool_config(value: Any, default: bool) -> bool:
    """Coerce a config value to bool, without treating every non-empty string as truthy."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _moss_runtime_options(moss_config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the ``tts.moss`` section into runtime/synthesize kwargs."""
    cpu_threads = int(moss_config.get("cpu_threads", 4))
    voice_clone_max_text_tokens = int(moss_config.get("voice_clone_max_text_tokens", 75))
    sample_mode = moss_config.get("sample_mode", "fixed")
    if cpu_threads < 1:
        raise ValueError("MOSS cpu_threads must be >= 1")
    if voice_clone_max_text_tokens < 1:
        raise ValueError("MOSS voice_clone_max_text_tokens must be >= 1")
    if sample_mode not in {"greedy", "fixed", "full"}:
        raise ValueError("MOSS sample_mode must be one of: greedy, fixed, full")
    return {
        "model_dir": moss_config.get("model_dir") or None,
        "prompt_audio": moss_config.get("prompt_audio", "") or _default_moss_ref_audio(),
        "cpu_threads": cpu_threads,
        "sample_mode": sample_mode,
        "do_sample": _coerce_bool_config(moss_config.get("do_sample"), True),
        "streaming": _coerce_bool_config(moss_config.get("streaming"), False),
        "max_new_frames": moss_config.get("max_new_frames"),
        "voice_clone_max_text_tokens": voice_clone_max_text_tokens,
        "enable_wetext": _coerce_bool_config(moss_config.get("enable_wetext_processing"), False),
        "enable_normalize_tts_text": _coerce_bool_config(
            moss_config.get("enable_normalize_tts_text"), False),
        "seed": moss_config.get("seed"),
    }


def _moss_convert_wav(wav_path: str, output_path: str) -> None:
    """Move MOSS's WAV sidecar into *output_path*'s container.

    ``.ogg`` is forced to libopus (ffmpeg's .ogg default is Vorbis, which voice bubbles reject).
    Without ffmpeg the WAV is moved under the requested name so the tool stays usable — the caller
    reflects that in voice-bubble eligibility."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        shutil.move(wav_path, output_path)
        return
    opus = _OPUS_VOICE_ARGS if output_path.lower().endswith(".ogg") else []
    subprocess.run(
        [ffmpeg, "-i", wav_path, *opus, "-y", "-loglevel", "error", output_path],
        check=True, timeout=30)
    if not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg conversion failed: {output_path} was not created")


def _generate_moss_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """Generate speech with the bundled MOSS-TTS-Nano ONNX runtime."""
    OnnxTtsRuntime = _import_moss_onnx_runtime()
    opts = _moss_runtime_options(_section(tts_config, "moss"))
    prompt_audio = opts["prompt_audio"]
    if not os.path.exists(prompt_audio):
        raise FileNotFoundError(f"MOSS reference audio not found: {prompt_audio}")

    wav_path = _wav_sidecar_path(output_path)
    runtime = OnnxTtsRuntime(
        model_dir=opts["model_dir"], thread_count=opts["cpu_threads"],
        max_new_frames=opts["max_new_frames"], do_sample=opts["do_sample"],
        sample_mode=opts["sample_mode"])
    try:
        runtime.synthesize(
            text=text, prompt_audio_path=prompt_audio, output_audio_path=wav_path,
            sample_mode=opts["sample_mode"], do_sample=opts["do_sample"],
            streaming=opts["streaming"], max_new_frames=opts["max_new_frames"],
            voice_clone_max_text_tokens=opts["voice_clone_max_text_tokens"],
            enable_wetext=opts["enable_wetext"],
            enable_normalize_tts_text=opts["enable_normalize_tts_text"], seed=opts["seed"])
        # The runtime returns metadata, not a guarantee: a silently-failed synthesis leaves no file.
        if not os.path.exists(wav_path):
            raise RuntimeError(
                f"MOSS synthesize completed but output file was not created: {wav_path}")
        if wav_path != output_path:
            _moss_convert_wav(wav_path, output_path)
    finally:
        if wav_path != output_path:
            _remove_quietly(wav_path)

    if not os.path.exists(output_path):
        raise RuntimeError(f"TTS output file missing after generation: {output_path}")
    return output_path
