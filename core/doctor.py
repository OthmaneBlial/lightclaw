"""Secret-safe local diagnostics for the LightClaw CLI."""

from __future__ import annotations

import platform
import shutil
import stat
import sys
from pathlib import Path

from config import Config

from .jobs import inspect_job_database
from .llm.client import PROVIDER_SPECS, provider_sdk_available
from .security import access_policy_label, delegated_process_env
from .workspaces import WorkspaceSafetyError, validate_workspace_root


def build_doctor_report(config: Config) -> dict[str, object]:
    """Return only bounded diagnostics; never serialize credential values."""
    checks: list[dict[str, object]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    config_file = Path(config.config_path).expanduser()
    if config_file.is_file():
        mode = stat.S_IMODE(config_file.stat().st_mode)
        status = "ok" if mode & 0o077 == 0 else "warning"
        add("config", status, f"app config exists with mode {mode:04o}")
    else:
        add("config", "error", "app config does not exist; run lightclaw onboard")

    policy = access_policy_label(
        config.telegram_allowed_users,
        config.telegram_public_bot_ack,
    )
    add(
        "telegram_access",
        "ok" if config.telegram_allowed_users else ("warning" if config.telegram_public_bot_ack else "error"),
        policy,
    )
    add(
        "telegram_token",
        "ok" if bool(config.telegram_bot_token) else "error",
        "configured" if config.telegram_bot_token else "missing",
    )
    provider = config.llm_provider.strip().lower()
    provider_spec = PROVIDER_SPECS.get(provider)
    add(
        "provider",
        "ok" if provider_spec is not None else "error",
        provider or "missing",
    )
    if provider_spec is not None:
        sdk_ready = provider_sdk_available(provider)
        add(
            "provider_sdk",
            "ok" if sdk_ready else "error",
            f"{provider_spec.sdk} installed"
            if sdk_ready
            else (
                f"{provider_spec.sdk} missing; install the "
                f"lightclaw-ai[{provider_spec.install_extra}] extra"
            ),
        )

    try:
        root = validate_workspace_root(config.workspace_path)
        add("workspace", "ok", f"resolved task root: {root}")
    except WorkspaceSafetyError as exc:
        add("workspace", "error", str(exc))

    child_env = delegated_process_env()
    suspicious_names = sorted(
        key
        for key in child_env
        if any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    )
    add(
        "delegated_environment",
        "ok" if not suspicious_names else "error",
        "minimal environment contains no credential-named variables"
        if not suspicious_names
        else "unexpected sensitive variable names: " + ", ".join(suspicious_names),
    )

    available_agents = [name for name in ("codex", "claude") if shutil.which(name)]
    add(
        "local_agents",
        "ok" if available_agents else "warning",
        ", ".join(available_agents) if available_agents else "none found in PATH",
    )

    jobs = inspect_job_database(Path(config.memory_db_path).expanduser().resolve().with_name("jobs.db"))
    stalled = jobs.get("stalled_run_ids") if isinstance(jobs.get("stalled_run_ids"), list) else []
    job_error = str(jobs.get("error") or "")
    if job_error:
        add("durable_jobs", "error", "job database is unreadable")
    elif stalled:
        add("durable_jobs", "warning", f"{len(stalled)} stalled or abandoned run(s) require review")
    else:
        counts = jobs.get("counts") if isinstance(jobs.get("counts"), dict) else {}
        queued = int(counts.get("queued", 0))
        active = int(counts.get("running", 0)) + int(counts.get("cancel_requested", 0))
        add("durable_jobs", "ok", f"{active} active, {queued} queued, no stalled runs")

    statuses = [str(item["status"]) for item in checks]
    overall = "error" if "error" in statuses else ("warning" if "warning" in statuses else "ok")
    return {
        "schema_version": 1,
        "overall": overall,
        "lightclaw": {
            "python": platform.python_version(),
            "python_supported": (3, 10) <= sys.version_info[:2] <= (3, 13),
            "capability_profile": config.local_agent_capability_profile,
            "access_policy": policy,
        },
        "jobs": jobs,
        "checks": checks,
    }


def render_doctor_text(report: dict[str, object]) -> str:
    icons = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    details = report.get("lightclaw") if isinstance(report.get("lightclaw"), dict) else {}
    lines = ["LightClaw doctor", f"Overall: {str(report.get('overall', 'unknown')).upper()}"]
    lines.append(f"Access policy: {details.get('access_policy', 'unknown')}")
    lines.append(f"Capability profile: {details.get('capability_profile', 'unknown')}")
    lines.append("")
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    for item in checks:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "error"))
        lines.append(
            f"[{icons.get(status, 'ERROR')}] {item.get('name', 'check')}: "
            f"{item.get('detail', '')}"
        )
    return "\n".join(lines)
