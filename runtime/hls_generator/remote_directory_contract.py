"""Shared remote directory contract helpers for HLS remote validation."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .config import remote_validation_config


def remote_directory_contract() -> dict[str, Any]:
    return dict(remote_validation_config()["directory_contract"])


def remote_directory_layout(run_id: str) -> dict[str, str]:
    contract = remote_directory_contract()
    active_rel = _render_run_path(contract["active_run_path_template"], run_id)
    backup_rel = _render_run_path(contract["backup_run_path_template"], run_id)
    return {
        "run_id": run_id,
        "project_root_relative": contract["project_root_dirname"],
        "conda_prefix_relative": _join(contract["project_root_dirname"], contract["conda_prefix_path"]),
        "active_run_relative": _join(contract["project_root_dirname"], active_rel),
        "backup_run_relative": _join(contract["project_root_dirname"], backup_rel),
        "archive_after_verification": str(contract["archive_after_verification"]).lower(),
        "archive_trigger": contract["archive_trigger"],
    }


def remote_directory_layout_for_workdir(remote_workdir: str, run_id: str) -> dict[str, str]:
    rel = remote_directory_layout(run_id)
    project_root_abs = _join(remote_workdir, rel["project_root_relative"])
    conda_prefix_abs = _join(remote_workdir, rel["conda_prefix_relative"])
    active_run_abs = _join(remote_workdir, rel["active_run_relative"])
    backup_run_abs = _join(remote_workdir, rel["backup_run_relative"])
    return {
        **rel,
        "project_root": project_root_abs,
        "conda_prefix": conda_prefix_abs,
        "active_run_dir": active_run_abs,
        "backup_run_dir": backup_run_abs,
    }


def validate_remote_result_contract(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    run_id = str(result.get("run_id") or "").strip()
    if not run_id:
        errors.append("missing run_id")
        return errors
    expected = remote_directory_layout(run_id)
    checks = {
        "remote_project_root": expected["project_root_relative"],
        "remote_conda_prefix": expected["conda_prefix_relative"],
        "remote_run_dir": expected["active_run_relative"],
        "remote_backup_dir": expected["backup_run_relative"],
    }
    for key, expected_value in checks.items():
        if str(result.get(key) or "").strip() != expected_value:
            errors.append(f"{key} must equal {expected_value}")
    if result.get("archived_after_verification") is not True:
        errors.append("archived_after_verification must be true")
    return errors


def _render_run_path(template: str, run_id: str) -> str:
    return template.replace("<run-id>", run_id).replace("__run_id__", run_id)


def _join(left: str, right: str) -> str:
    return PurePosixPath(left).joinpath(PurePosixPath(right)).as_posix()
