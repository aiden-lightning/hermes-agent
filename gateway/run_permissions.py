"""Per-sender gateway command, toolset, and session-search permissions."""

from __future__ import annotations

from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_owner_key, is_shared_multi_user_session
from utils import is_truthy_value


class GatewayPermissionsMixin:
    def _get_platform_command_permissions(self, platform: Optional[Platform]) -> Optional[Dict[str, Any]]:
        if platform is None:
            return None
        config = getattr(self, "config", None)
        platform_cfg = config.platforms.get(platform) if config and hasattr(config, "platforms") else None
        extra = getattr(platform_cfg, "extra", None)
        if not isinstance(extra, dict):
            return None
        permissions = extra.get("command_permissions")
        if not isinstance(permissions, dict):
            return None
        if "enabled" in permissions and not is_truthy_value(permissions.get("enabled"), default=True):
            return None
        return permissions

    @staticmethod
    def _get_platform_extra_from_user_config(
        user_config: Dict[str, Any], platform: Optional[Platform],
    ) -> Dict[str, Any]:
        if platform is None or not isinstance(user_config, dict):
            return {}
        platforms_cfg = user_config.get("platforms")
        if not isinstance(platforms_cfg, dict):
            return {}
        platform_cfg = platforms_cfg.get(platform.value)
        if not isinstance(platform_cfg, dict):
            return {}
        extra = platform_cfg.get("extra")
        return extra if isinstance(extra, dict) else {}

    @staticmethod
    def _normalize_identity_values(values: Any) -> set[str]:
        if values is None:
            return set()
        iterable = values.split(",") if isinstance(values, str) else (
            values if isinstance(values, (list, tuple, set, frozenset)) else [values]
        )
        return {text for value in iterable if (text := str(value or "").strip())}

    @staticmethod
    def _normalize_string_values(values: Any, *, lowercase: bool = False) -> set[str]:
        normalized = GatewayPermissionsMixin._normalize_identity_values(values)
        return {value.lower() for value in normalized} if lowercase else normalized

    @staticmethod
    def _normalize_allowed_command_values(values: Any) -> tuple[set[str], list[str]]:
        if values is None:
            iterable = []
        elif isinstance(values, str):
            iterable = [values]
        elif isinstance(values, (list, tuple, set, frozenset)):
            iterable = list(values)
        else:
            iterable = [values]
        try:
            from hermes_cli.commands import resolve_command
        except Exception:
            resolve_command = None
        allowed: set[str] = set()
        display: list[str] = []
        for value in iterable:
            normalized = str(value or "").strip().lstrip("/").lower()
            if not normalized:
                continue
            display.append("*" if normalized == "*" else f"/{normalized}")
            allowed.add(normalized)
            if resolve_command is not None:
                try:
                    command = resolve_command(normalized)
                except Exception:
                    command = None
                if command:
                    allowed.add(command.name)
        return allowed, display

    @staticmethod
    def _collect_source_sender_ids(source: Any) -> set[str]:
        if source is None:
            return set()
        return GatewayPermissionsMixin._normalize_identity_values(
            [getattr(source, "user_id", None), getattr(source, "user_id_alt", None)]
        )

    @staticmethod
    def _collect_event_sender_ids(event: MessageEvent) -> set[str]:
        source = getattr(event, "source", None)
        ids = GatewayPermissionsMixin._collect_source_sender_ids(source)
        if not source or getattr(source, "platform", None) != Platform.FEISHU:
            return ids

        def field(obj: Any, key: str) -> Any:
            return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

        def add_ids(obj: Any) -> None:
            for key in ("open_id", "user_id", "union_id"):
                text = str(field(obj, key) or "").strip() if obj is not None else ""
                if text:
                    ids.add(text)

        raw_event = field(getattr(event, "raw_message", None), "event")
        add_ids(field(field(raw_event, "sender"), "sender_id"))
        add_ids(field(raw_event, "user_id"))
        add_ids(field(raw_event, "operator"))
        return ids

    def _sender_matches_platform_admin(
        self, *, extra: Dict[str, Any], permissions: Dict[str, Any],
        source: Optional[SessionSource], actor_ids: Optional[set[str]] = None,
    ) -> bool:
        admin_values = permissions.get("admins") if isinstance(permissions, dict) else None
        if admin_values is None:
            admin_values = extra.get("admins", [])
        admin_ids = self._normalize_identity_values(admin_values)
        effective_actor_ids = set(actor_ids or ())
        effective_actor_ids.update(self._collect_source_sender_ids(source))
        return bool(admin_ids and effective_actor_ids and admin_ids & effective_actor_ids)

    def _resolve_session_owner_user_id(self, source: SessionSource) -> Optional[str]:
        config = getattr(self, "config", None)
        return build_session_owner_key(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        )

    def _resolve_effective_enabled_toolsets(
        self, *, user_config: Dict[str, Any], source: SessionSource,
        actor_ids: Optional[set[str]] = None, platform_key: Optional[str] = None,
    ) -> list[str]:
        from gateway.run import _platform_config_key
        from hermes_cli.tools_config import _get_platform_tools

        platform = getattr(source, "platform", None)
        resolved_platform_key = platform_key or _platform_config_key(platform)
        enabled_toolsets = sorted(_get_platform_tools(user_config, resolved_platform_key))
        extra = self._get_platform_extra_from_user_config(user_config, platform)
        permissions = extra.get("tool_permissions")
        if not isinstance(permissions, dict):
            return enabled_toolsets
        if "enabled" in permissions and not is_truthy_value(permissions.get("enabled"), default=True):
            return enabled_toolsets
        if self._sender_matches_platform_admin(
            extra=extra, permissions=permissions, source=source, actor_ids=actor_ids,
        ):
            return enabled_toolsets
        allowed = self._normalize_string_values(permissions.get("allowed_toolsets"), lowercase=True)
        if allowed:
            enabled_toolsets = [name for name in enabled_toolsets if name.lower() in allowed]
        disabled = self._normalize_string_values(permissions.get("disabled_toolsets"), lowercase=True)
        if disabled:
            enabled_toolsets = [name for name in enabled_toolsets if name.lower() not in disabled]
        return enabled_toolsets

    def _resolve_session_search_filters(
        self, *, user_config: Dict[str, Any], source: SessionSource,
        actor_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        platform = getattr(source, "platform", None)
        if platform != Platform.FEISHU:
            return {}
        extra = self._get_platform_extra_from_user_config(user_config, platform)
        is_admin = self._sender_matches_platform_admin(
            extra=extra, permissions={}, source=source, actor_ids=actor_ids,
        )
        effective_actor_ids = set(actor_ids or ())
        effective_actor_ids.update(self._collect_source_sender_ids(source))
        config = getattr(self, "config", None)
        if is_shared_multi_user_session(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        ):
            owner_id = self._resolve_session_owner_user_id(source)
            if owner_id:
                effective_actor_ids.add(owner_id)
        return {
            "source_filter": [platform.value],
            "user_id_filter": sorted(effective_actor_ids),
            "include_unowned_user_sessions": is_admin,
        }

    @staticmethod
    def _format_command_permission_denial(
        *, command_name: Optional[str], allowed_commands: list[str], free_text: bool,
    ) -> str:
        allowed_suffix = ""
        if allowed_commands:
            preview = ", ".join(allowed_commands[:12])
            if len(allowed_commands) > 12:
                preview += ", ..."
            allowed_suffix = f" Allowed commands: {preview}."
        if free_text:
            return f"Only bot admins can send free-form prompts here.{allowed_suffix}"
        return f"You don't have permission to use `/{command_name}` here.{allowed_suffix}"

    def _check_command_permissions(
        self, event: MessageEvent, *, raw_command: Optional[str], canonical_command: Optional[str],
    ) -> Optional[str]:
        if bool(getattr(event, "internal", False)):
            return None
        source = getattr(event, "source", None)
        platform = getattr(source, "platform", None)
        permissions = self._get_platform_command_permissions(platform)
        if not permissions:
            return None
        config = getattr(self, "config", None)
        platform_cfg = config.platforms.get(platform) if config and hasattr(config, "platforms") else None
        extra = getattr(platform_cfg, "extra", None)
        extra = extra if isinstance(extra, dict) else {}
        if self._sender_matches_platform_admin(
            extra=extra, permissions=permissions, source=source,
            actor_ids=self._collect_event_sender_ids(event),
        ):
            return None
        allowed, display = self._normalize_allowed_command_values(permissions.get("allowed_commands", []))
        if raw_command:
            candidates = {raw_command.lower()}
            if canonical_command:
                candidates.add(canonical_command.lower())
            if "*" in allowed or candidates & allowed:
                return None
            return self._format_command_permission_denial(
                command_name=raw_command, allowed_commands=display, free_text=False,
            )
        if is_truthy_value(permissions.get("allow_plain_text"), default=False):
            return None
        return self._format_command_permission_denial(
            command_name=None, allowed_commands=display, free_text=True,
        )

    def _command_permission_denial_for_event(self, event: MessageEvent) -> Optional[str]:
        command = event.get_command()
        try:
            from hermes_cli.commands import resolve_command
            command_def = resolve_command(command) if command else None
        except Exception:
            command_def = None
        return self._check_command_permissions(
            event, raw_command=command, canonical_command=command_def.name if command_def else command,
        )
