"""Gateway caller ownership and tool-scope rules for cronjob_manage."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def _identity_values(*values: Any) -> set[str]:
    return {str(value).strip() for value in values if str(value or "").strip()}


def _normalize_toolsets(values: Optional[Iterable[Any]]) -> list[str]:
    normalized: list[str] = []
    for value in values or ():
        name = str(value or "").strip()
        if name and name.lower() != "cronjob" and name not in normalized:
            normalized.append(name)
    return normalized


def gateway_owner_scope() -> Optional[Dict[str, str]]:
    """Stable gateway identity for the current turn; local callers remain unscoped."""
    from gateway.session_context import get_session_env

    platform = str(get_session_env("HERMES_SESSION_PLATFORM") or "").strip()
    user_id = str(get_session_env("HERMES_SESSION_USER_ID") or "").strip()
    if not platform or not user_id:
        return None
    owner = {"platform": platform, "user_id": user_id}
    for field, env_name in (
        ("user_id_alt", "HERMES_SESSION_USER_ID_ALT"),
        ("user_name", "HERMES_SESSION_USER_NAME"),
    ):
        value = str(get_session_env(env_name) or "").strip()
        if value:
            owner[field] = value
    return owner


def _configured_platform_admins(platform: str) -> set[str]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return set()
    platforms = config.get("platforms") if isinstance(config, dict) else None
    platform_config = platforms.get(platform) if isinstance(platforms, dict) else None
    if not isinstance(platform_config, dict):
        return set()
    extra = platform_config.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    admins = platform_config.get("admins")
    if admins is None:
        admins = extra.get("admins", [])
    if isinstance(admins, str):
        admins = [admins]
    return _identity_values(*(admins or []))


def caller_is_admin(owner: Optional[Dict[str, str]]) -> bool:
    """Local callers are operators; gateway callers require a stable configured ID."""
    if not owner:
        return True
    actor_ids = _identity_values(owner.get("user_id"), owner.get("user_id_alt"))
    return bool(actor_ids & _configured_platform_admins(owner.get("platform", "")))


def job_owned_by(job: Dict[str, Any], owner: Dict[str, str]) -> bool:
    job_owner = job.get("owner")
    if not isinstance(job_owner, dict):
        return False
    if str(job_owner.get("platform") or "") != str(owner.get("platform") or ""):
        return False
    job_ids = _identity_values(job_owner.get("user_id"), job_owner.get("user_id_alt"))
    caller_ids = _identity_values(owner.get("user_id"), owner.get("user_id_alt"))
    return bool(job_ids & caller_ids)


def can_access_job(
    job: Dict[str, Any], owner: Optional[Dict[str, str]], is_admin: bool,
) -> bool:
    return not owner or is_admin or job_owned_by(job, owner)


def permission_denial(job_ref: str) -> str:
    return f"Permission denied for cron job '{job_ref}'."


def filter_visible_jobs(
    jobs: Iterable[Dict[str, Any]], owner: Optional[Dict[str, str]], is_admin: bool,
) -> list[Dict[str, Any]]:
    if not owner or is_admin:
        return list(jobs)
    return [job for job in jobs if job_owned_by(job, owner)]


def _session_toolsets() -> Optional[list[str]]:
    from gateway.session_context import get_session_env

    raw = str(get_session_env("HERMES_SESSION_ENABLED_TOOLSETS") or "")
    resolved = _normalize_toolsets(raw.split(","))
    return resolved or None


def _derive_platform_toolsets(platform: str) -> Optional[list[str]]:
    """Fallback for callers that predate the per-turn toolset context binding."""
    if not platform:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        resolved = _normalize_toolsets(sorted(_get_platform_tools(load_config(), platform)))
    except Exception:
        return None
    return resolved or None


def resolve_job_toolsets(
    explicit_toolsets: Optional[Iterable[Any]],
    owner: Optional[Dict[str, str]],
    is_admin: bool,
) -> tuple[Optional[list[str]], Optional[str]]:
    """Keep gateway-authored jobs inside the creating session's tool boundary."""
    inherited = _session_toolsets()
    if owner and inherited is None:
        inherited = _derive_platform_toolsets(owner.get("platform", ""))
    if owner and inherited is None:
        return None, (
            "enabled_toolsets could not be resolved from the current session scope; "
            "gateway-owned cron jobs must inherit an explicit session or platform toolset scope."
        )

    explicit = _normalize_toolsets(explicit_toolsets)
    if explicit:
        if owner and not is_admin:
            inherited_names = {name.lower() for name in inherited or []}
            disallowed = [name for name in explicit if name.lower() not in inherited_names]
            if disallowed:
                return None, (
                    "enabled_toolsets may not include toolsets outside the current session scope: "
                    + ", ".join(sorted(disallowed))
                )
        return explicit, None
    if owner:
        return inherited, None
    return None, None


def context_reference_permission_error(
    refs: Iterable[Any], owner: Optional[Dict[str, str]], is_admin: bool,
) -> Optional[str]:
    """Reject cross-owner context chaining without disclosing another job's contents."""
    if not owner or is_admin:
        return None
    from cron.jobs import get_job

    for ref in refs:
        if isinstance(ref, str) and ref.strip().lower() == "self":
            continue
        job = get_job(str(ref))
        if job is not None and not job_owned_by(job, owner):
            return permission_denial(str(ref))
    return None
