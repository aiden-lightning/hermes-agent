"""OpenAI image generation backend.

Exposes OpenAI's ``gpt-image-2`` model at three quality tiers plus
OpenRouter's ``openai/gpt-5.4-image-2`` chat-completions image model as an
:class:`ImageGenProvider` implementation. The ``gpt-image-2`` tiers are
implemented as three virtual model IDs so the ``hermes tools`` model picker
and the ``image_gen.model`` config key behave like any other multi-model
backend:

    gpt-image-2-low     ~15s   fastest, good for iteration
    gpt-image-2-medium  ~40s   default — balanced
    gpt-image-2-high    ~2min  slowest, highest fidelity

The three ``gpt-image-2`` tiers hit the same underlying API model with a
different ``quality`` parameter via ``images.generate``. The
``openai/gpt-5.4-image-2`` entry uses ``chat.completions.create`` on
OpenRouter. Output is base64 JSON → saved under ``$HERMES_HOME/cache/images/``.

Selection precedence (first hit wins):

1. ``OPENAI_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.openai.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our tier IDs)
4. :data:`DEFAULT_MODEL` — ``gpt-image-2-medium``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
#
# All three IDs resolve to the same underlying API model with a different
# ``quality`` setting. ``api_model`` is what gets sent to OpenAI;
# ``quality`` is the knob that changes generation time and output fidelity.

API_MODEL = "gpt-image-2"
OPENROUTER_CHAT_IMAGE_MODEL = "openai/gpt-5.4-image-2"

_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2-low": {
        "display": "GPT Image 2 (Low)",
        "speed": "~15s",
        "strengths": "Fast iteration, lowest cost",
        "quality": "low",
        "api_method": "images.generate",
        "api_model": API_MODEL,
    },
    "gpt-image-2-medium": {
        "display": "GPT Image 2 (Medium)",
        "speed": "~40s",
        "strengths": "Balanced — default",
        "quality": "medium",
        "api_method": "images.generate",
        "api_model": API_MODEL,
    },
    "gpt-image-2-high": {
        "display": "GPT Image 2 (High)",
        "speed": "~2min",
        "strengths": "Highest fidelity, strongest prompt adherence",
        "quality": "high",
        "api_method": "images.generate",
        "api_model": API_MODEL,
    },
    OPENROUTER_CHAT_IMAGE_MODEL: {
        "display": "GPT 5.4 Image 2",
        "speed": "~30s",
        "strengths": "OpenRouter multimodal chat image generation",
        "api_method": "chat.completions",
        "api_model": OPENROUTER_CHAT_IMAGE_MODEL,
    },
}

DEFAULT_MODEL = "gpt-image-2-medium"

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}


def _load_openai_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml (returns {} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which tier to use and return ``(model_id, meta)``."""
    env_override = os.environ.get("OPENAI_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_openai_config()
    openai_cfg = cfg.get("openai") if isinstance(cfg.get("openai"), dict) else {}
    candidate: Optional[str] = None
    if isinstance(openai_cfg, dict):
        value = openai_cfg.get("model")
        if isinstance(value, str) and value in _MODELS:
            candidate = value
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top in _MODELS:
            candidate = top

    if candidate is not None:
        return candidate, _MODELS[candidate]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _get_openai_provider_config() -> Dict[str, Any]:
    cfg = _load_openai_config()
    openai_cfg = cfg.get("openai") if isinstance(cfg.get("openai"), dict) else {}
    return openai_cfg if isinstance(openai_cfg, dict) else {}


def _extract_chat_image_payload(response: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract ``(b64, url, revised_prompt)`` from chat-completions responses."""
    def _normalize_image_ref(item: Any) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(item, dict):
            b64 = item.get("b64_json") or item.get("image_base64")
            url = item.get("url")
            nested_image_url = item.get("image_url")
        else:
            b64 = getattr(item, "b64_json", None) or getattr(item, "image_base64", None)
            url = getattr(item, "url", None)
            nested_image_url = getattr(item, "image_url", None)

        if not url and nested_image_url:
            if isinstance(nested_image_url, dict):
                url = nested_image_url.get("url")
            elif isinstance(nested_image_url, str):
                url = nested_image_url
            else:
                url = getattr(nested_image_url, "url", None)

        if isinstance(url, str) and url.startswith("data:image/") and "," in url:
            header, _, data = url.partition(",")
            if ";base64" in header.lower() and data:
                return data, None

        return b64, url

    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        if message is None:
            continue

        revised_prompt = getattr(message, "revised_prompt", None)

        images = getattr(message, "images", None) or []
        for image in images:
            b64, url = _normalize_image_ref(image)
            if b64 or url:
                return b64, url, revised_prompt

        content = getattr(message, "content", None) or []
        for part in content:
            if isinstance(part, dict):
                part_revised = part.get("revised_prompt") or revised_prompt
            else:
                part_revised = getattr(part, "revised_prompt", None) or revised_prompt
            b64, url = _normalize_image_ref(part)
            if b64 or url:
                return b64, url, part_revised

    return None, None, None


def _cache_prefix(model_id: str) -> str:
    """Build a filesystem-safe cache prefix from the public model id."""
    return f"openai_{model_id.replace('/', '_')}"


def _resolve_api_config() -> Tuple[Optional[str], Optional[str]]:
    openai_cfg = _get_openai_provider_config()
    config_api_key = openai_cfg.get("api_key")
    api_key = config_api_key if isinstance(config_api_key, str) and config_api_key else None

    config_base_url = openai_cfg.get("base_url")
    base_url = config_base_url if isinstance(config_base_url, str) and config_base_url else None
    base_url_lower = base_url.lower() if isinstance(base_url, str) else ""

    if api_key is None:
        config_key_env = openai_cfg.get("key_env")
        key_env = config_key_env.strip() if isinstance(config_key_env, str) else ""
        if key_env:
            api_key = os.environ.get(key_env) or None

    if api_key is None:
        if "openrouter" in base_url_lower:
            api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
        else:
            api_key = os.environ.get("OPENAI_API_KEY")

    return api_key, base_url


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAIImageGenProvider(ImageGenProvider):
    """OpenAI image backend for gpt-image-2 and OpenRouter chat image models."""

    @property
    def name(self) -> str:
        return "openai"

    @property
    def display_name(self) -> str:
        return "OpenAI"

    def is_available(self) -> bool:
        api_key, _base_url = _resolve_api_config()
        if not api_key:
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "varies",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenAI",
            "badge": "paid",
            "tag": "gpt-image-2 at low/medium/high quality tiers",
            "env_vars": [
                {
                    "key": "OPENAI_API_KEY",
                    "prompt": "OpenAI API key",
                    "url": "https://platform.openai.com/api-keys",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="openai",
                aspect_ratio=aspect,
            )

        api_key, base_url = _resolve_api_config()
        if not api_key:
            return error_response(
                error=(
                    "OpenAI API key not set. Run `hermes tools` → Image "
                    "Generation → OpenAI to configure, or `hermes setup` "
                    "to add the key."
                ),
                error_type="auth_required",
                provider="openai",
                aspect_ratio=aspect,
            )

        try:
            import openai
        except ImportError:
            return error_response(
                error="openai Python package not installed (pip install openai)",
                error_type="missing_dependency",
                provider="openai",
                aspect_ratio=aspect,
            )

        tier_id, meta = _resolve_model()
        size = _SIZES.get(aspect, _SIZES["square"])
        is_openrouter = bool(base_url and "openrouter.ai" in base_url.lower())
        api_method = meta.get("api_method", "images.generate")
        api_model = meta.get("api_model", API_MODEL)
        if api_method == "images.generate" and is_openrouter:
            api_model = f"openai/{api_model}"

        try:
            client = openai.OpenAI(api_key=api_key, base_url=base_url)
            if api_method == "chat.completions":
                response = client.chat.completions.create(
                    model=api_model,
                    modalities=["image", "text"],
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4096,
                )
                b64, url, revised_prompt = _extract_chat_image_payload(response)
            else:
                # gpt-image-2 returns b64_json unconditionally and REJECTS
                # ``response_format`` as an unknown parameter. Don't send it.
                payload: Dict[str, Any] = {
                    "model": api_model,
                    "prompt": prompt,
                    "size": size,
                    "n": 1,
                    "quality": meta["quality"],
                }
                response = client.images.generate(**payload)
                data = getattr(response, "data", None) or []
                if not data:
                    return error_response(
                        error="OpenAI returned no image data",
                        error_type="empty_response",
                        provider="openai",
                        model=tier_id,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )

                first = data[0]
                b64 = getattr(first, "b64_json", None)
                url = getattr(first, "url", None)
                revised_prompt = getattr(first, "revised_prompt", None)
        except Exception as exc:
            logger.debug("OpenAI image generation failed", exc_info=True)
            return error_response(
                error=f"OpenAI image generation failed: {exc}",
                error_type="api_error",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if not b64 and not url:
            return error_response(
                error="OpenAI returned no image data",
                error_type="empty_response",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if b64:
            try:
                saved_path = save_b64_image(b64, prefix=_cache_prefix(tier_id))
            except Exception as exc:
                return error_response(
                    error=f"Could not save image to cache: {exc}",
                    error_type="io_error",
                    provider="openai",
                    model=tier_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        elif url:
            # Defensive — gpt-image-2 returns b64 today, but fall back
            # gracefully if the API ever changes.
            image_ref = url
        else:
            return error_response(
                error="OpenAI response contained neither b64_json nor URL",
                error_type="empty_response",
                provider="openai",
                model=tier_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": size}
        if "quality" in meta:
            extra["quality"] = meta["quality"]
        if revised_prompt:
            extra["revised_prompt"] = revised_prompt

        return success_response(
            image=image_ref,
            model=tier_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="openai",
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``OpenAIImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(OpenAIImageGenProvider())
