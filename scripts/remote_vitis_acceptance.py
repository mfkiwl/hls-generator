#!/usr/bin/env python3
"""Remote SSH link and Vitis acceptance checks via erie-remote-ssh."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from integration.hls_adapter import run_hls_workflow  # noqa: E402
from runtime.hls_generator.board_acceptance import BOARD_RUNNABLE_PROFILE, board_acceptance_config, resolve_host_template_path  # noqa: E402
from runtime.hls_generator.board_platform_payload import U55C_PLATFORM_NAME, default_local_u55c_payload_root, prepare_local_u55c_platform_archive, validate_local_board_platform_payload  # noqa: E402
from runtime.hls_generator.config import remote_validation_config, skill_config_path, skill_dependencies_config, skill_root, vitis_tool_timeout  # noqa: E402
from runtime.hls_generator.remote_directory_contract import remote_directory_layout_for_workdir  # noqa: E402
from runtime.hls_generator.skill_dependencies import SkillDependencyError, require_skill_dependencies  # noqa: E402
from runtime.hls_generator.user_config import get_board_platform_selection, get_vitis_selection, set_board_platform_selection, set_vitis_selection, user_config_path  # noqa: E402
from runtime.hls_generator.validation import READINESS_LEVELS  # noqa: E402

PASS_STATUS = "passed"
DRY_RUN_STATUS = "dry_run"
BLOCKED_VITIS_STATUS = "blocked_vitis_server"
BLOCKED_VERSION_STATUS = "blocked_remote_version_choice"
BLOCKED_PROFILE_STATUS = "blocked_remote_profile_config"
BLOCKED_BOARD_STATUS = "blocked_board_validation"
FAILED_STATUS = "failed"
UTF8_HINT = "Set PYTHONUTF8=1 and PYTHONIOENCODING=utf-8 when calling erie-remote-ssh."
BOARD_STATUS_MARKER = "HLS_BOARD_STATUS"


class RemoteAcceptanceError(RuntimeError):
    """Expected user-facing remote acceptance failure."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate HLS generator remote confidence through erie-remote-ssh.")
    parser.add_argument("--mode", required=True, choices=("link", "vitis", "board"))
    parser.add_argument("--server", help="Single-server target id or name from erie-remote-ssh config.")
    parser.add_argument("--build-server", help="Build-server id or name for split build/validate topology.")
    parser.add_argument("--validate-server", help="Validation-server id or name for split build/validate topology.")
    parser.add_argument("--profile", help="Optional remote_validation.vitis_profiles key for Vitis mode.")
    parser.add_argument("--vitis-version", help="Explicit remote Vitis version to use and remember for this server.")
    parser.add_argument("--target-part", help="Optional explicit target part override for remote HLS synthesis.")
    parser.add_argument("--platform-name", help="Explicit board platform name or platform spec for board mode.")
    parser.add_argument("--remote-platform-root", help="Remote directory containing an uploaded board platform for board mode.")
    parser.add_argument("--remote-xpfm", help="Explicit remote XPFM path for board mode.")
    parser.add_argument("--readiness", default="cosim", choices=READINESS_LEVELS)
    parser.add_argument("--example-spec", default="hls_vector_scale_mock_spec.json", help="Example spec from assets/examples used for Vitis acceptance artifacts.")
    parser.add_argument("--comment-language", default="auto", choices=("auto", "en", "zh"), help="Comment language for locally generated HLS acceptance artifacts.")
    parser.add_argument("--timeout", type=int, help="Override remote command timeout in seconds.")
    parser.add_argument("--cleanup-remote", action="store_true", help="Delete the remote validation directory after a successful Vitis run.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned erie helper steps without connecting.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        result = run_acceptance(args)
    except SkillDependencyError as exc:
        result = exc.report
    except (OSError, RemoteAcceptanceError, ValueError) as exc:
        result = {"status": FAILED_STATUS, "error": str(exc)}

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_format_result(result))

    if result["status"] in {PASS_STATUS, DRY_RUN_STATUS}:
        return 0
    if result["status"] == BLOCKED_PROFILE_STATUS:
        return 5
    if result["status"] == BLOCKED_VERSION_STATUS:
        return 4
    if result["status"] == BLOCKED_VITIS_STATUS:
        return 3
    if result["status"] == BLOCKED_BOARD_STATUS:
        return 6
    return 1


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    require_skill_dependencies(skill_dependencies_config(), scopes={"core"})
    config = remote_validation_config()
    base_timeout = int(args.timeout or config["default_timeout_s"])
    if args.mode in {"vitis", "board"}:
        base_timeout = max(base_timeout, int(vitis_tool_timeout(args.readiness)) + 30)
    timeout = base_timeout
    helper = ErieHelper(config, timeout)
    topology = _resolve_topology(args)
    plan = _planned_steps(
        args.mode,
        topology["server"],
        args.profile,
        args.readiness,
        cleanup_remote=bool(getattr(args, "cleanup_remote", False)),
        example_spec=str(getattr(args, "example_spec", "")),
        validate_server=topology.get("validate_server"),
        topology=topology["topology"],
    )
    if args.dry_run:
        result = {
            "status": DRY_RUN_STATUS,
            "mode": args.mode,
            "server": topology["server"],
            "build_server": topology.get("build_server"),
            "validate_server": topology.get("validate_server"),
            "topology": topology["topology"],
            "steps": plan,
            "uses_erie_remote_ssh": True,
        }
        if args.mode in {"vitis", "board"}:
            result.update({"cleanup_performed": False, "remote_artifacts_retained": True})
        return result
    if args.mode == "link":
        return _run_link_mode(args, config, helper, plan, topology)
    if args.mode == "board":
        if topology["topology"] != "single_server":
            raise ValueError("Board acceptance currently requires --server and does not support split topology.")
        return _run_board_mode(args, config, helper, plan, topology)
    if topology["topology"] == "split_build_validate":
        return _run_split_vitis_mode(args, config, helper, plan, topology)
    return _run_vitis_mode(args, config, helper, plan, topology)


def _run_link_mode(args: argparse.Namespace, config: dict[str, Any], helper: "ErieHelper", plan: list[str], topology: dict[str, Any]) -> dict[str, Any]:
    run_dir = _new_run_dir(config, "link")
    helper.preflight(topology["server"])
    output = helper.exec(topology["server"], list(config["link_probe_command"]))
    _reject_decode_noise(output)
    required = ("HLS_REMOTE_LINK_OK", "host=", "pwd=", "python=")
    missing = [item for item in required if item not in output]
    status = PASS_STATUS if not missing else FAILED_STATUS
    result = {
        "status": status,
        "mode": "link",
        "server": topology["server"],
        "topology": topology["topology"],
        "run_dir": str(run_dir),
        "steps": plan,
        "output": output,
        "missing_markers": missing,
        "uses_erie_remote_ssh": True,
    }
    _write_report(run_dir, result)
    return result


def _run_vitis_mode(args: argparse.Namespace, config: dict[str, Any], helper: "ErieHelper", plan: list[str], topology: dict[str, Any]) -> dict[str, Any]:
    profiles = config.get("vitis_profiles", {})
    run_dir = _new_run_dir(config, "vitis")
    settings = _write_erie_settings_overlay(config, run_dir)
    server = topology["server"]
    helper.preflight(server, settings=settings)
    helper.scan_software(server, settings=settings)
    candidates = _vitis_version_candidates(config, settings, server)
    profile = _resolve_profile_config(
        args,
        run_dir,
        candidates=candidates,
        configured_profiles=profiles,
        required_fields=("settings_script", "expected_tool"),
    )
    if profile.get("status") == BLOCKED_PROFILE_STATUS:
        _write_report(run_dir, profile)
        return profile
    selected_profile = _select_vitis_profile(args, run_dir, candidates, profile)
    if selected_profile.get("status") == BLOCKED_VERSION_STATUS:
        _write_report(run_dir, selected_profile)
        return selected_profile

    if args.target_part and not str(selected_profile.get("target_part") or "").strip():
        selected_profile = {**selected_profile, "target_part": str(args.target_part)}
    profile_probe = _probe_vitis(server, settings, helper, selected_profile)
    if profile_probe["status"] != PASS_STATUS:
        result = {
            "status": BLOCKED_VITIS_STATUS,
            "mode": "vitis",
            "server": server,
            "profile": args.profile,
            "vitis_version": selected_profile.get("version"),
            "readiness": args.readiness,
            "run_dir": str(run_dir),
            "topology": topology["topology"],
            "steps": plan,
            "probe": profile_probe,
            "uses_erie_remote_ssh": True,
        }
        _write_report(run_dir, result)
        return result
    selected_profile = {
        **selected_profile,
        "expected_tool": str(profile_probe.get("resolved_tool") or selected_profile.get("expected_tool")),
        "tool_path": str(profile_probe.get("tool_path") or ""),
    }
    if not str(selected_profile.get("target_part") or "").strip():
        inferred_target_part = _probe_target_part_hint(server, settings, helper)
        if inferred_target_part:
            selected_profile["target_part"] = inferred_target_part

    artifact_dir = _generate_local_hls_artifacts(run_dir, comment_language=args.comment_language, example_spec=args.example_spec)
    package_path = _create_vitis_package(run_dir, artifact_dir)
    remote_workdir = _probe_remote_workdir(server, settings, helper)
    result = _run_server_vitis_phase(
        helper,
        settings,
        server,
        selected_profile,
        args.readiness,
        package_path,
        config,
        run_dir,
        phase_label="single",
        cleanup_remote=args.cleanup_remote,
        remote_workdir=remote_workdir,
    )
    result.update(
        {
            "mode": "vitis",
            "topology": topology["topology"],
            "profile": args.profile,
            "example_spec": args.example_spec,
            "run_dir": str(run_dir),
            "artifact_dir": str(artifact_dir),
            "uses_erie_remote_ssh": True,
        }
    )
    _write_report(run_dir, result)
    return result


def _run_board_mode(args: argparse.Namespace, config: dict[str, Any], helper: "ErieHelper", plan: list[str], topology: dict[str, Any]) -> dict[str, Any]:
    profiles = config.get("vitis_profiles", {})
    run_dir = _new_run_dir(config, "board")
    settings = _write_erie_settings_overlay(config, run_dir)
    server = topology["server"]
    helper.preflight(server, settings=settings)
    helper.scan_software(server, settings=settings)
    remote_workdir = _probe_remote_workdir(server, settings, helper)
    candidates = _vitis_version_candidates(config, settings, server)
    board_profile = _resolve_profile_config(
        args,
        run_dir,
        candidates=candidates,
        configured_profiles=profiles,
        required_fields=("settings_script", "expected_tool"),
    )
    if board_profile.get("status") == BLOCKED_PROFILE_STATUS:
        _write_report(run_dir, board_profile)
        return board_profile
    selected_profile = _select_vitis_profile(args, run_dir, candidates, board_profile)
    if selected_profile.get("status") == BLOCKED_VERSION_STATUS:
        _write_report(run_dir, selected_profile)
        return selected_profile
    selected_profile = _merge_profile_fields(selected_profile, board_profile)
    if args.target_part and not str(selected_profile.get("target_part") or "").strip():
        selected_profile["target_part"] = str(args.target_part)
    if not str(selected_profile.get("target_part") or "").strip():
        inferred_target_part = _probe_target_part_hint(server, settings, helper) or _infer_target_part_from_server(settings, server)
        if inferred_target_part:
            selected_profile["target_part"] = inferred_target_part
    selected_profile = _merge_profile_fields(
        selected_profile,
        _resolve_board_platform_selection(args, server, remote_workdir, selected_profile, config["directory_contract"]),
    )
    platform_probe = _probe_platform_name(server, settings, helper, selected_profile)
    platform_upload: dict[str, Any] = {}
    if platform_probe["status"] != PASS_STATUS:
        upload_selection = _local_board_platform_upload_selection(remote_workdir, selected_profile, platform_probe, config["directory_contract"])
        if upload_selection:
            try:
                platform_upload = _upload_local_board_platform_payload(helper, settings, server, run_dir, remote_workdir, upload_selection)
            except RemoteAcceptanceError as exc:
                platform_upload = {"status": FAILED_STATUS, "error": str(exc), "selection": upload_selection}
            if platform_upload.get("status") == PASS_STATUS:
                selected_profile = _merge_profile_fields(selected_profile, platform_upload["selection"])
                platform_probe = _probe_platform_name(server, settings, helper, selected_profile)
    if platform_probe.get("selected_platform") and not str(selected_profile.get("platform_name") or "").strip():
        selected_profile["platform_name"] = str(platform_probe["selected_platform"])
    if platform_probe.get("selected_xpfm") and not str(selected_profile.get("remote_xpfm") or "").strip():
        selected_profile["remote_xpfm"] = str(platform_probe["selected_xpfm"])
    hardware_probe = _probe_hardware_fingerprint(server, settings, helper, selected_profile)
    toolchain_probe = _probe_board_toolchain(server, settings, helper, selected_profile)
    blocking_reasons: list[str] = []
    if not str(selected_profile.get("platform_name") or "").strip():
        blocking_reasons.append("missing_platform_name")
    if not str(selected_profile.get("target_part") or "").strip():
        blocking_reasons.append("missing_target_part")
    if platform_probe["status"] != PASS_STATUS:
        blocking_reasons.append("platform_probe")
    if hardware_probe["status"] != PASS_STATUS:
        blocking_reasons.append("hardware_probe")
    if toolchain_probe["status"] != PASS_STATUS:
        blocking_reasons.append("toolchain_probe")
    if blocking_reasons:
        upload_plan = _board_platform_upload_plan(run_dir, server, remote_workdir, selected_profile, platform_probe, config["directory_contract"])
        result = {
            "status": BLOCKED_BOARD_STATUS,
            "mode": "board",
            "server": server,
            "profile": args.profile,
            "readiness": args.readiness,
            "example_spec": args.example_spec,
            "run_dir": str(run_dir),
            "topology": topology["topology"],
            "steps": plan,
            "blocking_reasons": blocking_reasons,
            "platform_probe": platform_probe,
            "platform_upload": platform_upload,
            "hardware_probe": hardware_probe,
            "toolchain_probe": toolchain_probe,
            "platform_upload_plan": upload_plan,
            "uses_erie_remote_ssh": True,
        }
        _write_report(run_dir, result)
        return result

    artifact_dir = _generate_local_hls_artifacts(run_dir, comment_language=args.comment_language, example_spec=args.example_spec)
    package_path, board_metadata = _create_board_package(run_dir, artifact_dir, example_spec=args.example_spec)
    layout = remote_directory_layout_for_workdir(remote_workdir, run_dir.name)
    request_paths: list[str] = []
    request_paths.extend(_ensure_remote_project_layout(helper, settings, server, layout))
    request_paths.extend(_transfer_package_by_request_commands(helper, settings, server, layout["active_run_relative"], package_path))
    command = _remote_board_command(layout["active_run_dir"], selected_profile, board_metadata)
    detached = helper.exec_detached(server, "run board-level HLS acceptance", command, settings=settings)
    job_result = helper.wait_for_job(server, detached["job_id"], settings=settings, max_wait_s=max(helper.timeout, 5400))
    request_paths.append(detached["manifest"])
    if job_result["status"] != "succeeded":
        tail = _safe_tail_log(helper, server, detached["job_id"], settings)
        result = {
            "status": FAILED_STATUS,
            "mode": "board",
            "server": server,
            "profile": args.profile,
            "readiness": args.readiness,
            "example_spec": args.example_spec,
            "run_dir": str(run_dir),
            "topology": topology["topology"],
            "steps": plan,
            "hardware_probe": hardware_probe,
            "toolchain_probe": toolchain_probe,
            "job_status": job_result["status"],
            "job_output": job_result["output"],
            "tail_log": tail,
            "uses_erie_remote_ssh": True,
        }
        _write_report(run_dir, result)
        return result
    request_paths.append(_archive_remote_run(helper, settings, server, layout))
    result = {
        "status": PASS_STATUS,
        "mode": "board",
        "server": server,
        "topology": topology["topology"],
        "profile": args.profile,
        "vitis_version": str(selected_profile.get("version") or ""),
        "readiness": args.readiness,
        "example_spec": args.example_spec,
        "run_dir": str(run_dir),
        "artifact_dir": str(artifact_dir),
        "run_id": layout["run_id"],
        "remote_project_root": layout["project_root_relative"],
        "remote_project_root_abs": layout["project_root"],
        "remote_conda_prefix": layout["conda_prefix_relative"],
        "remote_conda_prefix_abs": layout["conda_prefix"],
        "remote_run_dir": layout["active_run_relative"],
        "remote_run_dir_abs": layout["active_run_dir"],
        "remote_backup_dir": layout["backup_run_relative"],
        "remote_backup_dir_abs": layout["backup_run_dir"],
        "remote_dir": layout["backup_run_relative"],
        "cleanup_performed": False,
        "remote_artifacts_retained": True,
        "archived_after_verification": True,
        "archive_trigger": config["directory_contract"]["archive_trigger"],
        "requests": request_paths,
        "job_id": detached["job_id"],
        "job_status": job_result["status"],
        "platform_probe": platform_probe,
        "platform_upload": platform_upload,
        "hardware_probe": hardware_probe,
        "toolchain_probe": toolchain_probe,
        "board_profile": {
            "platform_name": str(selected_profile.get("platform_name") or ""),
            "remote_platform_root": str(selected_profile.get("remote_platform_root") or ""),
            "remote_xpfm": str(selected_profile.get("remote_xpfm") or ""),
            "target_part": str(selected_profile.get("target_part") or ""),
        },
        "board_metadata": board_metadata,
        "board_status_marker": BOARD_STATUS_MARKER,
        "uses_erie_remote_ssh": True,
    }
    _write_report(run_dir, result)
    return result


def _run_split_vitis_mode(args: argparse.Namespace, config: dict[str, Any], helper: "ErieHelper", plan: list[str], topology: dict[str, Any]) -> dict[str, Any]:
    profiles = config.get("vitis_profiles", {})
    run_dir = _new_run_dir(config, "vitis-split")
    settings = _write_erie_settings_overlay(config, run_dir)
    build_server = topology["build_server"]
    validate_server = topology["validate_server"]

    helper.preflight(build_server, settings=settings)
    helper.preflight(validate_server, settings=settings)
    helper.scan_software(build_server, settings=settings)
    helper.scan_software(validate_server, settings=settings)

    build_candidates = _vitis_version_candidates(config, settings, build_server)
    validate_candidates = _vitis_version_candidates(config, settings, validate_server)
    shared_version = _select_shared_vitis_version(args, build_candidates, validate_candidates)
    build_profile = _resolve_profile_for_version(build_server, build_candidates, profiles, shared_version)
    validate_profile = _resolve_profile_for_version(validate_server, validate_candidates, profiles, shared_version)
    target_part = _resolve_target_part(args, settings, validate_server, validate_profile, build_profile)
    if not target_part:
        blocked = _blocked_profile_config(
            argparse.Namespace(
                server=build_server,
                profile=args.profile,
                readiness=args.readiness,
                example_spec=args.example_spec,
            ),
            run_dir,
            missing_fields=["target_part"],
            configured_profiles=profiles,
        )
        blocked["topology"] = topology["topology"]
        blocked["build_server"] = build_server
        blocked["validate_server"] = validate_server
        blocked["vitis_version"] = shared_version
        _write_report(run_dir, blocked)
        return blocked

    build_profile = {**build_profile, "target_part": target_part}
    validate_profile = {**validate_profile, "target_part": target_part}
    build_workdir = _probe_remote_workdir(build_server, settings, helper)
    validate_workdir = _probe_remote_workdir(validate_server, settings, helper)

    build_probe = _probe_vitis(build_server, settings, helper, build_profile)
    validate_probe = _probe_vitis(validate_server, settings, helper, validate_profile)
    device_probe = _probe_fpga_presence(validate_server, settings, helper)
    if build_probe["status"] != PASS_STATUS or validate_probe["status"] != PASS_STATUS or device_probe["status"] != PASS_STATUS:
        result = {
            "status": BLOCKED_VITIS_STATUS,
            "mode": "vitis",
            "topology": topology["topology"],
            "build_server": build_server,
            "validate_server": validate_server,
            "vitis_version": shared_version,
            "target_part": target_part,
            "run_dir": str(run_dir),
            "steps": plan,
            "build_probe": build_probe,
            "validate_probe": validate_probe,
            "device_probe": device_probe,
            "uses_erie_remote_ssh": True,
        }
        _write_report(run_dir, result)
        return result
    build_profile = {
        **build_profile,
        "expected_tool": str(build_probe.get("resolved_tool") or build_profile.get("expected_tool")),
        "tool_path": str(build_probe.get("tool_path") or ""),
    }
    validate_profile = {
        **validate_profile,
        "expected_tool": str(validate_probe.get("resolved_tool") or validate_profile.get("expected_tool")),
        "tool_path": str(validate_probe.get("tool_path") or ""),
    }

    artifact_dir = _generate_local_hls_artifacts(run_dir, comment_language=args.comment_language, example_spec=args.example_spec)
    package_path = _create_vitis_package(run_dir, artifact_dir)

    build_result = _run_server_vitis_phase(
        helper,
        settings,
        build_server,
        build_profile,
        args.readiness,
        package_path,
        config,
        run_dir,
        phase_label="build",
        cleanup_remote=args.cleanup_remote,
        remote_workdir=build_workdir,
    )
    validate_result = _run_server_vitis_phase(
        helper,
        settings,
        validate_server,
        validate_profile,
        args.readiness,
        package_path,
        config,
        run_dir,
        phase_label="validation",
        cleanup_remote=args.cleanup_remote,
        remote_workdir=validate_workdir,
    )

    passed = build_result["status"] == PASS_STATUS and validate_result["status"] == PASS_STATUS
    result = {
        "status": PASS_STATUS if passed else FAILED_STATUS,
        "mode": "vitis",
        "topology": topology["topology"],
        "build_server": build_server,
        "validate_server": validate_server,
        "vitis_version": shared_version,
        "target_part": target_part,
        "readiness": args.readiness,
        "example_spec": args.example_spec,
        "run_dir": str(run_dir),
        "steps": plan,
        "build_result": build_result,
        "validation_result": validate_result,
        "uses_erie_remote_ssh": True,
        "remote_artifacts_retained": (build_result.get("remote_artifacts_retained") is True and validate_result.get("remote_artifacts_retained") is True),
    }
    _write_report(run_dir, result)
    return result


class ErieHelper:
    def __init__(self, config: dict[str, Any], timeout: int) -> None:
        self.config = config
        self.timeout = timeout
        self.erie_skill_dir = Path(config["erie_skill_dir"])
        self.settings = Path(config["erie_settings_path"])
        self.script = self.erie_skill_dir / "scripts" / "remote_ssh.py"
        if not self.script.exists():
            raise RemoteAcceptanceError(f"erie-remote-ssh helper was not found: {self.script}")
        if not self.settings.exists():
            raise RemoteAcceptanceError(f"erie-remote-ssh settings were not found: {self.settings}")

    def preflight(self, server: str, *, settings: Path | None = None) -> None:
        active_settings = settings or self.settings
        self._run(["discover", "--settings", str(active_settings), "--json"])
        self._run(["list", "--settings", str(active_settings)])
        self._run(["check", "--settings", str(active_settings), "--server", server])
        self._run(["workspace-check", "--settings", str(active_settings), "--server", server, "--timeout", str(self.timeout)])

    def exec(self, server: str, command: list[str], *, settings: Path | None = None) -> str:
        active_settings = settings or self.settings
        return self._run(["exec", "--settings", str(active_settings), "--server", server, "--timeout", str(self.timeout), "--", *command])

    def scan_software(self, server: str, *, settings: Path | None = None) -> str:
        active_settings = settings or self.settings
        return self._run(["scan-software", "--settings", str(active_settings), "--server", server, "--timeout", str(self.timeout)])

    def request_and_run(self, settings: Path, server: str, operation: str, payload: list[str] | str, reason: str) -> str:
        if operation == "mkdir":
            request_stdout = self._run(["request-mkdir", "--settings", str(settings), "--server", server, "--path", payload[0], "--reason", reason])
        elif operation == "delete":
            args = ["request-delete", "--settings", str(settings), "--server", server, "--path", payload[0], "--reason", reason]
            if "--recursive" in payload:
                args.insert(-2, "--recursive")
            request_stdout = self._run(args)
        elif operation == "command":
            command = payload if isinstance(payload, str) else " ".join(payload)
            request_stdout = self._run(["request-command", "--settings", str(settings), "--server", server, "--reason", reason, "--", command])
        else:
            raise RemoteAcceptanceError(f"Unsupported request operation: {operation}")
        request_path = _parse_request_path(request_stdout)
        self._run_request_execute(
            settings,
            request_path,
            retries=1 if self._is_idempotent_request(operation, reason) else 0,
        )
        return request_path

    def request_upload_and_run(self, settings: Path, server: str, local_path: Path, remote_path: str, reason: str) -> str:
        request_stdout = self._run(
            [
                "request-upload",
                "--settings",
                str(settings),
                "--server",
                server,
                "--local",
                str(local_path),
                "--remote",
                remote_path,
                "--reason",
                reason,
            ]
        )
        request_path = _parse_request_path(request_stdout)
        self._run(["run-request", "--settings", str(settings), "--request", request_path, "--execute", "--timeout", str(self.timeout)])
        return request_path

    def exec_detached(self, server: str, reason: str, command: str, *, settings: Path | None = None) -> dict[str, Any]:
        active_settings = settings or self.settings
        output = self._run(["exec-detached", "--settings", str(active_settings), "--server", server, "--reason", reason, "--timeout", str(self.timeout), "--", "bash", "-lc", command])
        job_id = _field_from_output(output, "job_id")
        remote_job_dir = _field_from_output(output, "remote_job_dir")
        manifest = _field_from_output(output, "manifest")
        return {"job_id": job_id, "remote_job_dir": remote_job_dir, "manifest": manifest, "output": output}

    def wait_for_job(self, server: str, job_id: str, *, settings: Path | None = None, poll_s: int = 10, max_wait_s: int | None = None) -> dict[str, Any]:
        active_settings = settings or self.settings
        deadline = time.time() + float(max_wait_s or self.timeout)
        last_output = ""
        status_timeout = self._status_timeout()
        while time.time() < deadline:
            last_output, returncode = self._run_with_returncode(
                ["status", "--settings", str(active_settings), "--server", server, "--job", job_id, "--timeout", str(status_timeout)]
            )
            status = _field_from_output(last_output, "status")
            if status in {"succeeded", "failed", "not_found"}:
                return {"status": status, "output": last_output, "returncode": returncode}
            if returncode != 0:
                if "timed out" in last_output.lower():
                    time.sleep(poll_s)
                    continue
                raise RemoteAcceptanceError(f"erie-remote-ssh status command failed: {last_output.strip()}")
            time.sleep(poll_s)
        tail = self.tail_log(server, job_id, settings=active_settings, lines=40)
        raise RemoteAcceptanceError(f"Detached remote job {job_id} did not finish within {max_wait_s or self.timeout}s.\n{tail}")

    def tail_log(self, server: str, job_id: str, *, settings: Path | None = None, lines: int = 40) -> str:
        active_settings = settings or self.settings
        return self._run(["tail-log", "--settings", str(active_settings), "--server", server, "--job", job_id, "--lines", str(lines), "--timeout", str(self._status_timeout())])

    def _status_timeout(self) -> int:
        return min(max(int(self.timeout), 30), 180)

    def _request_timeout(self) -> int:
        return min(max(int(self.timeout), 30), 180)

    @staticmethod
    def _is_idempotent_request(operation: str, reason: str) -> bool:
        normalized_reason = reason.lower()
        if operation in {"mkdir", "delete"}:
            return True
        return operation == "command" and any(
            marker in normalized_reason
            for marker in (
                "initialize remote package payload",
                "prepare remote",
            )
        )

    def _run_request_execute(self, settings: Path, request_path: str, *, retries: int = 0) -> str:
        timeout_s = self._request_timeout()
        args = ["run-request", "--settings", str(settings), "--request", request_path, "--execute", "--timeout", str(timeout_s)]
        attempts = max(retries, 0) + 1
        last_output = ""
        for attempt in range(attempts):
            combined, returncode = self._run_with_returncode(args, timeout_s=timeout_s)
            if returncode == 0:
                return combined
            last_output = combined
            if "timed out" in combined.lower() and attempt + 1 < attempts:
                continue
            break
        raise RemoteAcceptanceError(f"erie-remote-ssh command failed (run-request): {last_output.strip()}")

    def _run(self, args: list[str]) -> str:
        combined, returncode = self._run_with_returncode(args)
        if returncode != 0:
            raise RemoteAcceptanceError(f"erie-remote-ssh command failed ({args[0]}): {combined.strip()}")
        return combined

    def _run_with_returncode(self, args: list[str], *, timeout_s: int | None = None) -> tuple[str, int]:
        env = os.environ.copy()
        env.update(self.config["python_env"])
        command = [sys.executable, str(self.script), *args]
        process_timeout = max(int(timeout_s if timeout_s is not None else self.timeout) + 10, 30)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=process_timeout, check=False)
        combined = (result.stdout or "") + (result.stderr or "")
        _reject_decode_noise(combined)
        return combined, result.returncode


def _resolve_topology(args: argparse.Namespace) -> dict[str, Any]:
    single = bool(getattr(args, "server", None))
    split_build = bool(getattr(args, "build_server", None))
    split_validate = bool(getattr(args, "validate_server", None))
    if single and (split_build or split_validate):
        raise ValueError("Use either --server or the pair --build-server/--validate-server, not both.")
    if split_build != split_validate:
        raise ValueError("Split topology requires both --build-server and --validate-server.")
    if split_build and split_validate:
        return {
            "topology": "split_build_validate",
            "server": str(args.build_server),
            "build_server": str(args.build_server),
            "validate_server": str(args.validate_server),
        }
    if single:
        return {"topology": "single_server", "server": str(args.server)}
    raise ValueError("Provide either --server or both --build-server and --validate-server.")


def _probe_vitis(server: str, settings: Path, helper: ErieHelper, profile: dict[str, Any]) -> dict[str, Any]:
    expected_tool = str(profile["expected_tool"])
    expected_tool_path = str(profile.get("expected_tool_path") or "").strip()
    settings_script = str(profile["settings_script"])
    env_setup_script = str(profile.get("env_setup_script") or "").strip()
    command_text = (
        f"if [ -f {shlex.quote(settings_script)} ]; then source {shlex.quote(settings_script)} >/dev/null 2>&1; fi; "
        f"printf 'expected_tool='; command -v {shlex.quote(expected_tool)} || true; "
    )
    if env_setup_script:
        command_text = (
            f"if [ -f {shlex.quote(settings_script)} ]; then source {shlex.quote(settings_script)} >/dev/null 2>&1; fi; "
            f"if [ -f {shlex.quote(env_setup_script)} ]; then source {shlex.quote(env_setup_script)} >/dev/null 2>&1; fi; "
            f"printf 'expected_tool='; command -v {shlex.quote(expected_tool)} || true; "
        )
    if expected_tool_path:
        command_text += (
            f"printf '\\nexpected_tool_path='; if [ -x {shlex.quote(expected_tool_path)} ]; then printf %s {shlex.quote(expected_tool_path)}; fi; "
        )
    command_text += "printf '\\nfallback_vitis_run='; command -v vitis-run || true; "
    command_text += "printf '\\nfallback_vitis_hls='; command -v vitis_hls || true"
    command = [
        "bash",
        "-lc",
        command_text,
    ]
    output = helper.exec(server, command, settings=settings)
    _reject_decode_noise(output)
    tool_path = ""
    direct_tool_path = ""
    fallback_vitis_run = ""
    fallback_vitis_hls = ""
    for line in output.splitlines():
        if line.startswith("expected_tool="):
            tool_path = line.split("=", 1)[1].strip()
        elif line.startswith("expected_tool_path="):
            direct_tool_path = line.split("=", 1)[1].strip()
        elif line.startswith("fallback_vitis_run="):
            fallback_vitis_run = line.split("=", 1)[1].strip()
        elif line.startswith("fallback_vitis_hls="):
            fallback_vitis_hls = line.split("=", 1)[1].strip()
    resolved_tool = expected_tool
    if not tool_path and direct_tool_path:
        tool_path = direct_tool_path
    if not tool_path and fallback_vitis_run:
        tool_path = fallback_vitis_run
        resolved_tool = "vitis-run"
    elif not tool_path and fallback_vitis_hls:
        tool_path = fallback_vitis_hls
        resolved_tool = "vitis_hls"
    return {
        "status": PASS_STATUS if tool_path else BLOCKED_VITIS_STATUS,
        "expected_tool": expected_tool,
        "resolved_tool": resolved_tool,
        "tool_path": tool_path,
        "output": output,
    }


def _probe_fpga_presence(server: str, settings: Path, helper: ErieHelper) -> dict[str, Any]:
    command = [
        "bash",
        "-lc",
        "if lspci | grep -iq 'xilinx'; then printf 'fpga_present=yes\\n'; lspci | grep -i 'xilinx' | head -n 12; else printf 'fpga_present=no\\n'; fi",
    ]
    output = helper.exec(server, command, settings=settings)
    _reject_decode_noise(output)
    return {"status": PASS_STATUS if "fpga_present=yes" in output else BLOCKED_VITIS_STATUS, "output": output}


def _probe_target_part_hint(server: str, settings: Path, helper: ErieHelper) -> str:
    command = [
        "bash",
        "-lc",
        "if [ -d /opt/xilinx/firmware/u55c ] || [ -d /tools/Xilinx/firmware/u55c ]; then printf 'target_part=xcu55c-fsvh2892-2L-e'; "
        "elif [ -d /opt/xilinx/firmware/u50 ] || [ -d /tools/Xilinx/firmware/u50 ]; then printf 'target_part=xcu50-fsvh2104-2-e'; "
        "fi",
    ]
    output = helper.exec(server, command, settings=settings)
    _reject_decode_noise(output)
    for line in output.splitlines():
        if line.startswith("target_part="):
            return line.split("=", 1)[1].strip()
    return ""


def _probe_hardware_fingerprint(server: str, settings: Path, helper: ErieHelper, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    source_settings = ""
    xbmgmt_tool_path = ""
    if profile:
        settings_script = str(profile.get("settings_script") or "").strip()
        xrt_setup_script = str(profile.get("xrt_setup_script") or "").strip()
        xbmgmt_tool_path = str(profile.get("xbmgmt_tool_path") or "").strip()
        if settings_script:
            source_settings += f"source {shlex.quote(settings_script)} >/dev/null 2>&1 || true; "
        if xrt_setup_script:
            source_settings += f"source {shlex.quote(xrt_setup_script)} >/dev/null 2>&1 || true; "
    xbmgmt_probe = (
        f"if [ -x {shlex.quote(xbmgmt_tool_path)} ]; then {shlex.quote(xbmgmt_tool_path)} examine 2>/dev/null; "
        "else xbmgmt examine 2>/dev/null; fi"
        if xbmgmt_tool_path
        else "xbmgmt examine 2>/dev/null || true"
    )
    command = [
        "bash",
        "-lc",
        f"{source_settings}"
        "printf 'cpu_model='; (lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -n 1); "
        "printf '\\nlspci='; (lspci | grep -Ei 'xilinx|alveo' | head -n 20 || true); "
        "printf '\\nfirmware_scan='; (find /opt/xilinx/firmware -maxdepth 2 -type d 2>/dev/null | head -n 40 || true); "
        "printf '\\nboard_scan='; ((xrt-smi examine 2>/dev/null || xbutil examine 2>/dev/null || true) | head -n 120); "
        f"printf '\\nmgmt_scan='; (({xbmgmt_probe}) | head -n 120)",
    ]
    output = helper.exec(server, command, settings=settings)
    _reject_decode_noise(output)
    lspci_text = _section_value(output, "lspci")
    firmware_text = _section_value(output, "firmware_scan")
    board_text = _section_value(output, "board_scan")
    mgmt_text = _section_value(output, "mgmt_scan")
    normalized = " ".join((lspci_text, board_text, mgmt_text)).lower()
    firmware_hint = any(token in firmware_text.lower() for token in ("u55c", "xcu55c", "xilinx_u55c"))
    status = PASS_STATUS if any(token in normalized for token in ("u55c", "xcu55c", "xilinx_u55c")) else BLOCKED_BOARD_STATUS
    evidence_path = ""
    if status != PASS_STATUS:
        evidence_path = "hardware fingerprint does not yet prove an active U55C device"
    return {"status": status, "output": output, "evidence": evidence_path, "firmware_hint": firmware_hint}


def _probe_board_toolchain(server: str, settings: Path, helper: ErieHelper, profile: dict[str, Any]) -> dict[str, Any]:
    settings_script = str(profile["settings_script"])
    xrt_setup_script = str(profile.get("xrt_setup_script") or "").strip()
    vpp_path = str(profile.get("vpp_path") or "").strip()
    xrt_tool_path = str(profile.get("xrt_tool_path") or "").strip()
    source_xrt = f"source {shlex.quote(xrt_setup_script)} >/dev/null 2>&1 || true; " if xrt_setup_script else ""
    command = [
        "bash",
        "-lc",
        f"source {shlex.quote(settings_script)} >/dev/null 2>&1 || true; "
        f"{source_xrt}"
        "printf 'vpp='; command -v v++ || true; "
        f"printf '\\nvpp_path='; if [ -x {shlex.quote(vpp_path)} ]; then printf %s {shlex.quote(vpp_path)}; fi; "
        "printf '\\ngpp='; command -v g++ || true; "
        "printf '\\nxrt='; command -v xrt-smi || command -v xbutil || true; "
        f"printf '\\nxrt_path='; if [ -x {shlex.quote(xrt_tool_path)} ]; then printf %s {shlex.quote(xrt_tool_path)}; fi",
    ]
    output = helper.exec(server, command, settings=settings)
    _reject_decode_noise(output)
    has_vpp = "vpp=/" in output or "vpp_path=/" in output
    has_xrt = "xrt=/" in output or "xrt_path=/" in output
    status = PASS_STATUS if has_vpp and "gpp=/" in output and has_xrt else BLOCKED_BOARD_STATUS
    return {"status": status, "output": output}


def _probe_platform_name(server: str, settings: Path, helper: ErieHelper, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    if profile and str(profile.get("platform_name") or "").strip():
        platform_name = str(profile["platform_name"]).strip()
        upload_probe = _probe_uploaded_platform(server, settings, helper, profile)
        if upload_probe.get("status") == PASS_STATUS:
            return {
                "status": PASS_STATUS,
                "selected_platform": platform_name,
                "selected_xpfm": str(upload_probe.get("selected_xpfm") or ""),
                "candidates": [platform_name],
                "all_platforms": [platform_name],
                "output": str(upload_probe.get("output") or "platform_name=provided"),
            }
        if str(profile.get("remote_xpfm") or "").strip() or str(profile.get("remote_platform_root") or "").strip():
            shell_probe = _probe_shell_name(server, settings, helper, profile)
            return {
                "status": BLOCKED_BOARD_STATUS,
                "selected_platform": "",
                "selected_xpfm": "",
                "candidates": [],
                "all_platforms": [],
                "reason": str(upload_probe.get("reason") or "missing_uploaded_platform_payload"),
                "shell_name": str(shell_probe.get("shell_name") or ""),
                "suggested_platform_name": str(shell_probe.get("suggested_platform_name") or ""),
                "output": str(upload_probe.get("output") or "platform_name=provided"),
            }
    target_part = str(profile.get("target_part") or "").strip().lower() if profile else ""
    expected_family = "u55c" if "u55c" in target_part else "u50" if "u50" in target_part else ""
    command = [
        "bash",
        "-lc",
        "find /tools/Xilinx/Vitis /opt/xilinx -type f -name '*.xpfm' 2>/dev/null | head -n 200",
    ]
    output = helper.exec(server, command, settings=settings)
    _reject_decode_noise(output)
    paths = [line.strip() for line in output.splitlines() if line.strip()]
    platform_names = sorted({PurePosixPath(path).stem for path in paths})
    if expected_family:
        matched = [name for name in platform_names if expected_family in name.lower()]
    else:
        matched = [name for name in platform_names if any(token in name.lower() for token in ("u55c", "u50"))]
    if len(matched) == 1:
        return {
            "status": PASS_STATUS,
            "selected_platform": matched[0],
            "selected_xpfm": "",
            "candidates": matched,
            "all_platforms": platform_names,
            "output": output,
        }
    shell_probe = _probe_shell_name(server, settings, helper, profile)
    reason = "no_matching_platform" if not matched else "multiple_matching_platforms"
    if shell_probe.get("shell_name"):
        reason = f"{reason}_shell_detected"
    return {
        "status": BLOCKED_BOARD_STATUS,
        "selected_platform": "",
        "selected_xpfm": "",
        "candidates": matched,
        "all_platforms": platform_names,
        "reason": reason,
        "shell_name": str(shell_probe.get("shell_name") or ""),
        "suggested_platform_name": str(shell_probe.get("suggested_platform_name") or ""),
        "output": output,
    }


def _probe_uploaded_platform(server: str, settings: Path, helper: ErieHelper, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    if not profile:
        return {"status": BLOCKED_BOARD_STATUS, "reason": "missing_profile"}
    remote_xpfm = str(profile.get("remote_xpfm") or "").strip()
    remote_platform_root = str(profile.get("remote_platform_root") or "").strip()
    platform_name = str(profile.get("platform_name") or "").strip()
    if remote_xpfm:
        command = ["bash", "-lc", f"if [ -f {shlex.quote(remote_xpfm)} ]; then printf 'selected_xpfm=%s' {shlex.quote(remote_xpfm)}; fi"]
        output = helper.exec(server, command, settings=settings)
        _reject_decode_noise(output)
        selected_xpfm = _section_value(output, "selected_xpfm")
        if selected_xpfm:
            return {"status": PASS_STATUS, "selected_xpfm": selected_xpfm, "output": output}
        return {"status": BLOCKED_BOARD_STATUS, "reason": "missing_uploaded_xpfm", "output": output}
    if remote_platform_root:
        command = [
            "bash",
            "-lc",
            f"find {shlex.quote(remote_platform_root)} -maxdepth 3 -type f -name '*.xpfm' 2>/dev/null | sed -n '1,40p'",
        ]
        output = helper.exec(server, command, settings=settings)
        _reject_decode_noise(output)
        paths = [line.strip() for line in output.splitlines() if line.strip()]
        if not paths:
            return {"status": BLOCKED_BOARD_STATUS, "reason": "missing_uploaded_platform_payload", "output": output}
        if platform_name:
            matched = [path for path in paths if PurePosixPath(path).stem == platform_name]
            if len(matched) == 1:
                return {"status": PASS_STATUS, "selected_xpfm": matched[0], "output": output}
        if len(paths) == 1:
            return {"status": PASS_STATUS, "selected_xpfm": paths[0], "output": output}
        return {"status": BLOCKED_BOARD_STATUS, "reason": "multiple_uploaded_xpfm_candidates", "output": output}
    return {"status": BLOCKED_BOARD_STATUS, "reason": "missing_uploaded_platform_payload"}


def _probe_shell_name(server: str, settings: Path, helper: ErieHelper, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    xbmgmt_tool_path = str(profile.get("xbmgmt_tool_path") or "").strip() if profile else ""
    command_text = (
        f"if [ -x {shlex.quote(xbmgmt_tool_path)} ]; then {shlex.quote(xbmgmt_tool_path)} examine 2>/dev/null; "
        "else xbmgmt examine 2>/dev/null; fi"
        if xbmgmt_tool_path
        else "xbmgmt examine 2>/dev/null || true"
    )
    output = helper.exec(server, ["bash", "-lc", command_text], settings=settings)
    _reject_decode_noise(output)
    shell_name = ""
    for line in output.splitlines():
        match = re.search(r"\|\[[^\]]+\]\s+\|\s*([A-Za-z0-9_]+)\s+\|", line)
        if match:
            shell_name = match.group(1).strip()
            break
    return {
        "shell_name": shell_name,
        "suggested_platform_name": _suggest_platform_name_from_shell(shell_name),
        "output": output,
    }


def _suggest_platform_name_from_shell(shell_name: str) -> str:
    normalized = str(shell_name or "").strip().lower()
    if normalized == "xilinx_u55c_gen3x16_xdma_base_3":
        return "xilinx_u55c_gen3x16_xdma_3_202210_1"
    return ""


def _merge_profile_fields(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, str):
            if value.strip():
                merged[key] = value
            continue
        if value is not None:
            merged[key] = value
    return merged


def _resolve_board_platform_selection(
    args: argparse.Namespace,
    server: str,
    remote_workdir: str,
    selected_profile: dict[str, Any],
    directory_contract: dict[str, Any],
) -> dict[str, Any]:
    explicit = _explicit_board_platform_selection(args, remote_workdir)
    if explicit:
        set_board_platform_selection(server, explicit)
        return explicit
    saved = get_board_platform_selection(server)
    if saved:
        normalized_saved = dict(saved)
        normalized_saved["remote_platform_root"] = _normalize_remote_platform_path(remote_workdir, str(saved.get("remote_platform_root") or ""))
        normalized_saved["remote_xpfm"] = _normalize_remote_platform_path(remote_workdir, str(saved.get("remote_xpfm") or ""))
        return normalized_saved
    platform_name = str(selected_profile.get("platform_name") or "").strip()
    if not platform_name:
        platform_name = _default_platform_name_for_part(str(selected_profile.get("target_part") or ""))
    if not platform_name:
        return {}
    return _governed_remote_platform_selection(remote_workdir, platform_name, directory_contract)


def _explicit_board_platform_selection(args: argparse.Namespace, remote_workdir: str) -> dict[str, Any]:
    if not any(str(getattr(args, field, "") or "").strip() for field in ("platform_name", "remote_platform_root", "remote_xpfm")):
        return {}
    return {
        "platform_name": str(getattr(args, "platform_name", "") or "").strip(),
        "remote_platform_root": _normalize_remote_platform_path(remote_workdir, str(getattr(args, "remote_platform_root", "") or "").strip()),
        "remote_xpfm": _normalize_remote_platform_path(remote_workdir, str(getattr(args, "remote_xpfm", "") or "").strip()),
        "source": "upload",
    }


def _normalize_remote_platform_path(remote_workdir: str, raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute():
        return path.as_posix()
    return (PurePosixPath(remote_workdir) / path).as_posix()


def _default_platform_name_for_part(target_part: str) -> str:
    normalized = str(target_part or "").strip().lower()
    if "u55c" in normalized:
        return "xilinx_u55c_gen3x16_xdma_3_202210_1"
    if "u50" in normalized:
        return "xilinx_u50_gen3x16_xdma_5_202210_1"
    return ""


def _governed_remote_platform_selection(remote_workdir: str, platform_name: str, directory_contract: dict[str, Any]) -> dict[str, Any]:
    project_root = PurePosixPath(remote_workdir) / str(directory_contract["project_root_dirname"])
    root_template = str(directory_contract["platform_root_path_template"]).replace("<platform-name>", platform_name)
    root = (project_root / PurePosixPath(root_template)).as_posix()
    return {
        "platform_name": platform_name,
        "remote_platform_root": root,
        "remote_xpfm": (PurePosixPath(root) / f"{platform_name}.xpfm").as_posix(),
        "source": "upload",
    }


def _local_board_platform_upload_selection(
    remote_workdir: str,
    selected_profile: dict[str, Any],
    platform_probe: dict[str, Any],
    directory_contract: dict[str, Any],
) -> dict[str, Any]:
    platform_name = str(selected_profile.get("platform_name") or platform_probe.get("suggested_platform_name") or _default_platform_name_for_part(str(selected_profile.get("target_part") or ""))).strip()
    if platform_name != U55C_PLATFORM_NAME:
        return {}
    selection = _governed_remote_platform_selection(remote_workdir, platform_name, directory_contract)
    if str(selected_profile.get("remote_platform_root") or "").strip():
        selection["remote_platform_root"] = str(selected_profile["remote_platform_root"]).strip()
    if str(selected_profile.get("remote_xpfm") or "").strip():
        selection["remote_xpfm"] = str(selected_profile["remote_xpfm"]).strip()
    return selection


def _upload_local_board_platform_payload(
    helper: ErieHelper,
    settings: Path,
    server: str,
    run_dir: Path,
    remote_workdir: str,
    selection: dict[str, Any],
    *,
    local_root: Path | None = None,
) -> dict[str, Any]:
    platform_name = str(selection.get("platform_name") or "").strip()
    if platform_name != U55C_PLATFORM_NAME:
        return {"status": "skipped", "reason": "only the governed U55C payload has a fixed local dependency source", "selection": selection}
    prepared = prepare_local_u55c_platform_archive(run_dir / "platform-upload", local_root=local_root)
    if prepared.get("status") != PASS_STATUS:
        return {
            "status": BLOCKED_BOARD_STATUS,
            "reason": "invalid_local_u55c_platform_payload",
            "local_payload": prepared,
            "selection": selection,
        }
    archive_path = Path(str(prepared["archive_path"]))
    remote_root = PurePosixPath(str(selection["remote_platform_root"]))
    remote_parent = remote_root.parent
    remote_archive_abs = remote_parent / archive_path.name
    remote_archive_rel = _remote_relative_to_workdir(remote_workdir, remote_archive_abs)
    upload_request = helper.request_upload_and_run(settings, server, archive_path, remote_archive_rel, "upload U55C platform payload")
    remote_xpfm = str(selection["remote_xpfm"])
    command = (
        f"mkdir -p {shlex.quote(remote_parent.as_posix())} && "
        f"tar -xzf {shlex.quote(remote_archive_abs.as_posix())} -C {shlex.quote(remote_parent.as_posix())} && "
        f"test -f {shlex.quote(remote_xpfm)}"
    )
    extract_request = helper.request_and_run(settings, server, "command", command, "extract U55C platform payload")
    set_board_platform_selection(server, selection)
    return {
        "status": PASS_STATUS,
        "platform_name": platform_name,
        "archive_path": str(archive_path),
        "remote_archive": remote_archive_abs.as_posix(),
        "remote_archive_relative": remote_archive_rel,
        "remote_platform_root": str(selection["remote_platform_root"]),
        "remote_xpfm": remote_xpfm,
        "selection": selection,
        "local_payload": prepared,
        "requests": [upload_request, extract_request],
    }


def _remote_relative_to_workdir(remote_workdir: str, remote_path: PurePosixPath) -> str:
    workdir = PurePosixPath(remote_workdir)
    try:
        return remote_path.relative_to(workdir).as_posix()
    except ValueError:
        return remote_path.as_posix().lstrip("/")


def _board_platform_upload_plan(
    run_dir: Path,
    server: str,
    remote_workdir: str,
    selected_profile: dict[str, Any],
    platform_probe: dict[str, Any],
    directory_contract: dict[str, Any],
) -> dict[str, Any]:
    platform_name = str(selected_profile.get("platform_name") or platform_probe.get("suggested_platform_name") or _default_platform_name_for_part(str(selected_profile.get("target_part") or ""))).strip()
    if not platform_name:
        return {}
    selection = _governed_remote_platform_selection(remote_workdir, platform_name, directory_contract)
    local_payload = (
        validate_local_board_platform_payload(default_local_u55c_payload_root(), expected_platform_name=U55C_PLATFORM_NAME)
        if platform_name == U55C_PLATFORM_NAME
        else {}
    )
    upload_plan = {
        "server": server,
        "platform_name": platform_name,
        "source": "upload",
        "expected_local_directory": platform_name,
        "local_payload": local_payload,
        "remote_platform_root": selection["remote_platform_root"],
        "remote_xpfm": selection["remote_xpfm"],
        "recommended_steps": [
            f"tar the local platform directory {platform_name}/ into a single archive",
            f"upload the archive to {server} under {selection['remote_platform_root']}",
            f"extract the archive so that {selection['remote_xpfm']} exists on the remote host",
            f"rerun remote_vitis_acceptance.py --mode board --server {server} --platform-name {platform_name} --remote-platform-root {selection['remote_platform_root']} --remote-xpfm {selection['remote_xpfm']}",
        ],
        "recommended_commands": [
            f"python C:<REDACTED_LOCAL_PATH> request-upload --settings <erie-settings.json> --server {server} --local <local-platform-archive> --remote erie-hls-generator/platforms/alveo/{platform_name}.tar.gz --reason \"upload U55C platform payload\"",
            f"python C:<REDACTED_LOCAL_PATH> request-command --settings <erie-settings.json> --server {server} --reason \"extract U55C platform payload\" -- bash -lc \"mkdir -p {shlex.quote(selection['remote_platform_root'])} && tar -xzf erie-hls-generator/platforms/alveo/{platform_name}.tar.gz -C {shlex.quote(selection['remote_platform_root'])} --strip-components=1\"",
        ],
    }
    request_path = run_dir / "remote_board_platform_request.json"
    _write_json(request_path, upload_plan)
    upload_plan["request_path"] = str(request_path)
    return upload_plan


def _section_value(output: str, key: str) -> str:
    prefix = f"{key}="
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        parts = [line.split("=", 1)[1].strip()]
        for extra in lines[index + 1 :]:
            if re.match(r"^[A-Za-z0-9_]+=", extra):
                break
            parts.append(extra.strip())
        return "\n".join(item for item in parts if item)
    return ""


def _probe_remote_workdir(server: str, settings: Path, helper: ErieHelper) -> str:
    output = helper.exec(server, ["bash", "-lc", "pwd"], settings=settings)
    _reject_decode_noise(output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise RemoteAcceptanceError(f"Could not determine remote workdir for server {server}.")
    return lines[-1]


def _ensure_remote_project_layout(helper: ErieHelper, settings: Path, server: str, layout: dict[str, str]) -> list[str]:
    request_paths: list[str] = []
    project_root = shlex.quote(layout["project_root_relative"])
    conda_prefix = shlex.quote(layout["conda_prefix_relative"])
    runs_parent = shlex.quote(str(PurePosixPath(layout["active_run_relative"]).parent))
    backups_parent = shlex.quote(str(PurePosixPath(layout["backup_run_relative"]).parent))
    active_run = shlex.quote(layout["active_run_relative"])
    command = f"mkdir -p {project_root} {conda_prefix} {runs_parent} {backups_parent} {active_run}"
    request_paths.append(helper.request_and_run(settings, server, "command", command, "prepare governed remote project root, conda prefix path, and active run directory"))
    return request_paths


def _archive_remote_run(helper: ErieHelper, settings: Path, server: str, layout: dict[str, str]) -> str:
    active_run = shlex.quote(layout["active_run_relative"])
    backup_run = shlex.quote(layout["backup_run_relative"])
    backup_parent = shlex.quote(str(PurePosixPath(layout["backup_run_relative"]).parent))
    command = f"mkdir -p {backup_parent} && rm -rf {backup_run} && mv {active_run} {backup_run}"
    return helper.request_and_run(settings, server, "command", command, "archive verified remote run into governed backups directory")


def _select_vitis_profile(args: argparse.Namespace, run_dir: Path, candidates: list[dict[str, Any]], fallback_profile: dict[str, Any]) -> dict[str, Any]:
    explicit_version = str(args.vitis_version or "").strip()
    if explicit_version:
        selected = _find_candidate(candidates, explicit_version)
        if not selected:
            raise RemoteAcceptanceError(f"Requested Vitis version {explicit_version!r} was not found on {args.server}.")
        set_vitis_selection(args.server, selected)
        return selected

    saved = get_vitis_selection(args.server)
    if saved:
        candidate = _find_candidate(candidates, str(saved.get("version") or "")) if candidates else None
        if candidate:
            merged = {**candidate, **saved}
            set_vitis_selection(args.server, merged)
            return merged
        if not candidates:
            return saved

    if len(candidates) > 1:
        request = _remote_vitis_version_request(args, run_dir, candidates)
        request_path = run_dir / "remote_vitis_version_request.json"
        _write_json(request_path, request)
        return {
            "status": BLOCKED_VERSION_STATUS,
            "mode": "vitis",
            "server": args.server,
            "profile": args.profile,
            "readiness": args.readiness,
            "example_spec": args.example_spec,
            "run_dir": str(run_dir),
            "remote_vitis_version_request": str(request_path),
            "candidate_versions": candidates,
            "user_config_path": str(user_config_path()),
            "uses_erie_remote_ssh": True,
        }
    if len(candidates) == 1:
        return candidates[0]
    return {
        "version": str(fallback_profile.get("version") or args.profile),
        "settings_script": str(fallback_profile["settings_script"]),
        "expected_tool": str(fallback_profile["expected_tool"]),
        "target_part": str(fallback_profile.get("target_part", "")),
    }


def _select_shared_vitis_version(args: argparse.Namespace, build_candidates: list[dict[str, Any]], validate_candidates: list[dict[str, Any]]) -> str:
    if args.vitis_version:
        return str(args.vitis_version)
    shared = sorted({str(item.get("version")) for item in build_candidates} & {str(item.get("version")) for item in validate_candidates}, key=_version_sort_key)
    if not shared:
        raise RemoteAcceptanceError("No shared Vitis version is available across the selected build and validation servers.")
    return shared[0]


def _version_sort_key(value: str) -> tuple[int, ...]:
    match = re.findall(r"\d+", str(value))
    return tuple(int(item) for item in match) if match else (9999,)


def _resolve_profile_for_version(server: str, candidates: list[dict[str, Any]], configured_profiles: dict[str, Any], version: str) -> dict[str, Any]:
    saved = get_vitis_selection(server)
    if saved and str(saved.get("version") or "") == version and str(saved.get("settings_script") or "").strip() and str(saved.get("expected_tool") or "").strip():
        return saved
    candidate = _find_candidate(candidates, version)
    if candidate:
        set_vitis_selection(server, candidate)
        return candidate
    for _, profile in configured_profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("version") or "") == version and str(profile.get("settings_script") or "").strip() and str(profile.get("expected_tool") or "").strip():
            return profile
    raise RemoteAcceptanceError(f"Could not resolve Vitis profile for server {server!r} and version {version!r}.")


def _resolve_target_part(args: argparse.Namespace, settings: Path, validate_server: str, validate_profile: dict[str, Any], build_profile: dict[str, Any]) -> str:
    if str(getattr(args, "target_part", "") or "").strip():
        return str(args.target_part).strip()
    for profile in (validate_profile, build_profile, get_vitis_selection(validate_server) or {}):
        target_part = str(profile.get("target_part") or "").strip() if isinstance(profile, dict) else ""
        if target_part:
            return target_part
    inferred = _infer_target_part_from_server(settings, validate_server)
    return inferred


def _resolve_profile_config(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    candidates: list[dict[str, Any]],
    configured_profiles: dict[str, Any],
    required_fields: tuple[str, ...],
) -> dict[str, Any]:
    explicit_profile = str(args.profile or "").strip()
    if explicit_profile:
        profile = configured_profiles.get(explicit_profile)
        if not isinstance(profile, dict):
            return _blocked_profile_config(args, run_dir, missing_fields=list(required_fields), configured_profiles=configured_profiles)
        resolved = {**profile, "version": str(profile.get("version") or explicit_profile)}
        missing = [field for field in required_fields if not str(resolved.get(field) or "").strip()]
        if missing:
            return _blocked_profile_config(args, run_dir, missing_fields=missing, configured_profiles=configured_profiles)
        return resolved

    saved = get_vitis_selection(args.server)
    if saved:
        missing = [field for field in required_fields if not str(saved.get(field) or "").strip()]
        if not missing:
            return saved

    complete_profiles: list[tuple[str, dict[str, Any]]] = []
    for name, profile in configured_profiles.items():
        if not isinstance(profile, dict):
            continue
        resolved = {**profile, "version": str(profile.get("version") or name)}
        missing = [field for field in required_fields if not str(resolved.get(field) or "").strip()]
        if not missing:
            complete_profiles.append((name, resolved))
    if len(complete_profiles) == 1:
        return complete_profiles[0][1]

    candidate_profiles = [
        item
        for item in candidates
        if all(str(item.get(field) or "").strip() for field in required_fields)
    ]
    if candidate_profiles:
        return dict(candidate_profiles[0])

    return _blocked_profile_config(args, run_dir, missing_fields=list(required_fields), configured_profiles=configured_profiles)


def _blocked_profile_config(
    args: argparse.Namespace,
    run_dir: Path,
    *,
    missing_fields: list[str],
    configured_profiles: dict[str, Any],
) -> dict[str, Any]:
    mode = str(getattr(args, "mode", "vitis") or "vitis")
    recommended_commands = [
        f"python .\\scripts\\remote_vitis_acceptance.py --mode {mode} --server {args.server} --profile <configured-profile> --readiness {args.readiness} --example-spec {args.example_spec} --json",
        f"python .\\scripts\\remote_vitis_acceptance.py --mode {mode} --server {args.server} --vitis-version <version> --readiness {args.readiness} --example-spec {args.example_spec} --json",
    ]
    request = {
        "version": 1,
        "action": "ask_remote_vitis_profile_config",
        "question": "Remote Vitis validation requires an explicit configured profile or a previously saved remote selection. Configure the missing values before retrying.",
        "server": args.server,
        "profile": args.profile,
        "readiness": args.readiness,
        "example_spec": args.example_spec,
        "missing_fields": missing_fields,
        "configured_profiles": sorted(str(name) for name in configured_profiles),
        "user_config_path": str(user_config_path()),
        "recommended_commands": recommended_commands,
    }
    request_path = run_dir / "remote_vitis_profile_request.json"
    _write_json(request_path, request)
    return {
        "status": BLOCKED_PROFILE_STATUS,
        "mode": mode,
        "server": args.server,
        "profile": args.profile,
        "readiness": args.readiness,
        "example_spec": args.example_spec,
        "run_dir": str(run_dir),
        "missing_fields": missing_fields,
        "configured_profiles": sorted(str(name) for name in configured_profiles),
        "remote_vitis_profile_request": str(request_path),
        "user_config_path": str(user_config_path()),
        "uses_erie_remote_ssh": True,
    }


def _vitis_version_candidates(config: dict[str, Any], settings_path: Path, server: str) -> list[dict[str, Any]]:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    server_list_path = _resolve_erie_server_list(settings, settings_path, Path(config["erie_skill_dir"]))
    try:
        server_list = json.loads(server_list_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    raw_server = _find_server_record(server_list, server)
    if not raw_server:
        return []
    inferred_target_part = _infer_target_part_from_server_record(raw_server)
    scan = raw_server.get("software_scan", {})
    tools = scan.get("tools", {}) if isinstance(scan, dict) else {}
    vitis = tools.get("vitis", {}) if isinstance(tools, dict) else {}
    versions = vitis.get("versions") if isinstance(vitis, dict) else None
    raw_versions = versions if isinstance(versions, list) else ([vitis] if vitis.get("status") == "installed" else [])
    candidates: list[dict[str, Any]] = []
    for item in raw_versions:
        if not isinstance(item, dict) or item.get("status") != "installed":
            continue
        install_path = str(item.get("install_path") or "").strip()
        executable_path = str(item.get("path") or "").strip()
        version = _version_label(item)
        settings_script = (install_path.rstrip("/") + "/settings64.sh") if install_path else ""
        expected_tool_path = _infer_vitis_hls_executable(install_path, version)
        env_setup_script = _infer_vitis_hls_env_setup(install_path, version)
        candidates.append(
            {
                "version": version,
                "settings_script": settings_script,
                "expected_tool": "vitis_hls",
                "expected_tool_path": expected_tool_path,
                "env_setup_script": env_setup_script,
                "vpp_path": install_path.rstrip("/") + "/bin/v++" if install_path else "",
                "xrt_tool_path": "/opt/xilinx/xrt/bin/xrt-smi",
                "xrt_setup_script": "/opt/xilinx/xrt/setup.sh",
                "xbmgmt_tool_path": "/opt/xilinx/xrt/bin/xbmgmt",
                "target_part": str(item.get("target_part") or inferred_target_part or ""),
                "install_path": install_path,
                "executable_path": executable_path,
            }
        )
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(str(candidate["version"]), candidate)
    return list(unique.values())


def _find_server_record(server_list: dict[str, Any], server: str) -> dict[str, Any] | None:
    for item in server_list.get("servers", []):
        if not isinstance(item, dict):
            continue
        selectors = {str(item.get("id") or ""), str(item.get("name") or ""), str(item.get("legacy_id") or "")}
        if server in selectors:
            return item
    return None


def _infer_target_part_from_server(settings_path: Path, server: str) -> str:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    server_list_path = _resolve_erie_server_list(settings, settings_path, Path(remote_validation_config()["erie_skill_dir"]))
    try:
        server_list = json.loads(server_list_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    record = _find_server_record(server_list, server)
    if not record:
        return ""
    return _infer_target_part_from_server_record(record)


def _infer_target_part_from_server_record(record: dict[str, Any]) -> str:
    models: list[str] = []
    for source_key in ("inventory_snapshot", "software_scan"):
        source = record.get(source_key)
        if not isinstance(source, dict):
            continue
        for item in source.get("fpga_devices", []) or []:
            if isinstance(item, dict) and item.get("model"):
                models.append(str(item["model"]))
    normalized = (" ".join(models) + " " + str(record.get("name") or "")).lower()
    if "u55c" in normalized:
        return "xcu55c-fsvh2892-2L-e"
    if "u50" in normalized:
        return "".join(("xcu", "50", "-fsvh2104-2-e"))
    return ""


def _version_label(item: dict[str, Any]) -> str:
    for value in (item.get("install_path"), item.get("version"), item.get("path")):
        text = str(value or "")
        match = re.search(r"(20\d{2}\.\d+)", text)
        if match:
            return match.group(1)
    return str(item.get("version") or item.get("install_path") or item.get("path") or "unknown")


def _find_candidate(candidates: list[dict[str, Any]], version: str) -> dict[str, Any] | None:
    return next((item for item in candidates if str(item.get("version")) == version), None)


def _infer_vitis_hls_executable(install_path: str, version: str) -> str:
    path_text = str(install_path or "").strip()
    if "/Vitis/" in path_text:
        return path_text.replace("/Vitis/", "/Vitis_HLS/").rstrip("/") + "/bin/vitis_hls"
    version_text = str(version or "").strip()
    if version_text:
        return f"/tools/Xilinx/Vitis_HLS/{version_text}/bin/vitis_hls"
    return ""


def _infer_vitis_hls_env_setup(install_path: str, version: str) -> str:
    path_text = str(install_path or "").strip()
    if "/Vitis/" in path_text:
        return path_text.replace("/Vitis/", "/Vitis_HLS/").rstrip("/") + "/bin/setupEnv.sh"
    version_text = str(version or "").strip()
    if version_text:
        return f"/tools/Xilinx/Vitis_HLS/{version_text}/bin/setupEnv.sh"
    return ""


def _remote_vitis_version_request(args: argparse.Namespace, run_dir: Path, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    commands = [
        f"python .\\scripts\\remote_vitis_acceptance.py --mode vitis --server {args.server} --profile {args.profile} --vitis-version {item['version']} --readiness {args.readiness} --example-spec {args.example_spec} --json"
        for item in candidates
    ]
    return {
        "version": 1,
        "action": "ask_remote_vitis_version",
        "primary_source": "multiple_remote_vitis_versions",
        "question": "Multiple Vitis versions were detected on the selected remote server. Choose one before HLS validation or development continues.",
        "server": args.server,
        "profile": args.profile,
        "readiness": args.readiness,
        "example_spec": args.example_spec,
        "candidate_versions": candidates,
        "user_config_path": str(user_config_path()),
        "recommended_commands": commands,
        "output": str(run_dir / "remote_vitis_version_request.json"),
    }


def _generate_local_hls_artifacts(run_dir: Path, *, comment_language: str, example_spec: str = "hls_vector_scale_mock_spec.json") -> Path:
    spec_path = skill_config_path("examples_dir") / example_spec
    if not spec_path.exists() or spec_path.name != example_spec:
        raise RemoteAcceptanceError(f"Unknown HLS acceptance example spec: {example_spec}")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    result = run_hls_workflow(spec, out_dir=run_dir / "local-generation", provider_name="mock", readiness="static", run_external=False, comment_language=comment_language)
    if result["status"] != PASS_STATUS:
        raise RemoteAcceptanceError(f"Local artifact generation failed: {result['status']}")
    return Path(result["run_dir"]) / "attempt-001" / "hls" / "artifacts"


def _load_example_spec(example_spec: str) -> dict[str, Any]:
    spec_path = skill_config_path("examples_dir") / example_spec
    if not spec_path.exists() or spec_path.name != example_spec:
        raise RemoteAcceptanceError(f"Unknown HLS acceptance example spec: {example_spec}")
    return json.loads(spec_path.read_text(encoding="utf-8"))


def _create_board_package(run_dir: Path, artifact_dir: Path, *, example_spec: str) -> tuple[Path, dict[str, Any]]:
    spec = _load_example_spec(example_spec)
    board_config = board_acceptance_config(spec)
    if str(board_config.get("profile") or "").strip() != BOARD_RUNNABLE_PROFILE:
        raise RemoteAcceptanceError(f"Example spec {example_spec} is not declared board-runnable.")
    top_function = str(spec.get("interfaces", {}).get("top_function") or spec.get("name") or "kernel")
    host_template = str(board_config.get("host_template") or "").strip()
    host_source = _render_board_host(example_spec, top_function, host_template)
    board_dir = run_dir / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    host_path = board_dir / "host.cpp"
    host_path.write_text(host_source, encoding="utf-8", newline="\n")
    runner = run_dir / "run_board_validation.sh"
    runner.write_text(_board_runner_script(top_function), encoding="utf-8", newline="\n")
    metadata = {
        "example_spec": example_spec,
        "top_function": top_function,
        "host_template": host_template,
        "profile": str(board_config.get("profile") or ""),
    }
    package_path = run_dir / "board_artifacts.tar.gz"
    with tarfile.open(package_path, "w:gz") as tar:
        for path in sorted(artifact_dir.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=Path("artifacts") / path.relative_to(artifact_dir))
        tar.add(host_path, arcname="board/host.cpp")
        tar.add(runner, arcname="run_board_validation.sh")
    return package_path, metadata


def _create_vitis_package(run_dir: Path, artifact_dir: Path) -> Path:
    runner = run_dir / "run_vitis.sh"
    runner.write_text(_remote_runner_script(), encoding="utf-8", newline="\n")
    package_path = run_dir / "hls_artifacts.tar.gz"
    with tarfile.open(package_path, "w:gz") as tar:
        for path in sorted(artifact_dir.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=Path("artifacts") / path.relative_to(artifact_dir))
        tar.add(runner, arcname="run_vitis.sh")
    return package_path


def _render_board_host(example_spec: str, top_function: str, template_name: str) -> str:
    template_path = resolve_host_template_path(SKILL_ROOT, template_name)
    text = template_path.read_text(encoding="utf-8")
    rendered = text.replace("{{TOP_FUNCTION}}", top_function)
    if "{{TOP_FUNCTION}}" in rendered:
        raise RemoteAcceptanceError(f"Board host template {template_name!r} was not rendered completely for {example_spec}.")
    return rendered


def _transfer_package_by_request_commands(helper: ErieHelper, settings: Path, server: str, remote_dir: str, package_path: Path) -> list[str]:
    encoded = base64.b64encode(package_path.read_bytes()).decode("ascii")
    requests: list[str] = []
    remote_b64 = f"{remote_dir}/hls_artifacts.tar.gz.b64"
    requests.append(helper.request_and_run(settings, server, "command", f": > {shlex.quote(remote_b64)}", "initialize remote package payload"))
    for index in range(0, len(encoded), 7000):
        chunk = encoded[index : index + 7000]
        requests.append(helper.request_and_run(settings, server, "command", f"printf %s {shlex.quote(chunk)} >> {shlex.quote(remote_b64)}", "append remote package payload chunk"))
    return requests


def _remote_vitis_command(remote_dir: str, profile: dict[str, Any], readiness: str) -> str:
    settings_script = shlex.quote(str(profile["settings_script"]))
    env_setup_script = shlex.quote(str(profile.get("env_setup_script") or ""))
    expected_tool = shlex.quote(str(profile.get("tool_path") or profile["expected_tool"]))
    target_part = shlex.quote(str(profile.get("target_part", "")))
    readiness_arg = shlex.quote(readiness)
    remote = shlex.quote(remote_dir)
    return (
        f"cd {remote} && base64 -d hls_artifacts.tar.gz.b64 > hls_artifacts.tar.gz && "
        "tar -xzf hls_artifacts.tar.gz && "
        f"HLS_SETTINGS_SCRIPT={settings_script} HLS_ENV_SETUP_SCRIPT={env_setup_script} "
        f"HLS_EXPECTED_TOOL={expected_tool} HLS_TARGET_PART={target_part} HLS_READINESS={readiness_arg} bash run_vitis.sh"
    )


def _remote_board_command(remote_dir: str, profile: dict[str, Any], metadata: dict[str, Any]) -> str:
    settings_script = shlex.quote(str(profile["settings_script"]))
    platform_name = shlex.quote(str(profile.get("platform_spec") or profile.get("remote_xpfm") or profile["platform_name"]))
    target_part = shlex.quote(str(profile.get("target_part", "")))
    top_function = shlex.quote(str(metadata["top_function"]))
    xrt_setup_script = str(profile.get("xrt_setup_script") or "").strip()
    xrt_setup_arg = shlex.quote(xrt_setup_script)
    vpp_tool = shlex.quote(str(profile.get("vpp_path") or "v++"))
    xrt_tool = shlex.quote(str(profile.get("xrt_tool_path") or ""))
    remote = shlex.quote(remote_dir)
    return (
        f"cd {remote} && base64 -d hls_artifacts.tar.gz.b64 > board_artifacts.tar.gz && "
        "tar -xzf board_artifacts.tar.gz && "
        f"HLS_SETTINGS_SCRIPT={settings_script} HLS_PLATFORM_NAME={platform_name} "
        f"HLS_TARGET_PART={target_part} HLS_TOP_FUNCTION={top_function} "
        f"HLS_XRT_SETUP_SCRIPT={xrt_setup_arg} HLS_VPP_TOOL={vpp_tool} HLS_XRT_TOOL={xrt_tool} bash run_board_validation.sh"
    )


def _run_server_vitis_phase(
    helper: ErieHelper,
    settings: Path,
    server: str,
    profile: dict[str, Any],
    readiness: str,
    package_path: Path,
    config: dict[str, Any],
    run_dir: Path,
    *,
    phase_label: str,
    cleanup_remote: bool,
    remote_workdir: str,
) -> dict[str, Any]:
    layout = remote_directory_layout_for_workdir(remote_workdir, f"{run_dir.name}-{phase_label}")
    request_paths: list[str] = []
    request_paths.extend(_ensure_remote_project_layout(helper, settings, server, layout))
    request_paths.extend(_transfer_package_by_request_commands(helper, settings, server, layout["active_run_relative"], package_path))
    command = _remote_vitis_command(layout["active_run_dir"], profile, readiness)
    detached = helper.exec_detached(server, f"run Vitis HLS {phase_label}", command, settings=settings)
    job_result = helper.wait_for_job(server, detached["job_id"], settings=settings, max_wait_s=max(helper.timeout, 1800))
    request_paths.append(detached["manifest"])
    if job_result["status"] != "succeeded":
        tail = _safe_tail_log(helper, server, detached["job_id"], settings)
        details = job_result["output"].strip()
        raise RemoteAcceptanceError(
            f"Detached Vitis HLS {phase_label} job failed for server {server}.\n{details}\n{tail}"
        )
    cleanup_performed = False
    archived_after_verification = False
    if config["directory_contract"]["archive_after_verification"]:
        request_paths.append(_archive_remote_run(helper, settings, server, layout))
        archived_after_verification = True
    return {
        "status": PASS_STATUS,
        "server": server,
        "phase": phase_label,
        "vitis_version": profile.get("version"),
        "target_part": profile.get("target_part"),
        "run_id": layout["run_id"],
        "remote_project_root": layout["project_root_relative"],
        "remote_project_root_abs": layout["project_root"],
        "remote_conda_prefix": layout["conda_prefix_relative"],
        "remote_conda_prefix_abs": layout["conda_prefix"],
        "remote_run_dir": layout["active_run_relative"],
        "remote_run_dir_abs": layout["active_run_dir"],
        "remote_backup_dir": layout["backup_run_relative"],
        "remote_backup_dir_abs": layout["backup_run_dir"],
        "remote_dir": layout["backup_run_relative"] if archived_after_verification else layout["active_run_relative"],
        "job_id": detached["job_id"],
        "requests": request_paths,
        "cleanup_performed": cleanup_performed,
        "remote_artifacts_retained": True,
        "archived_after_verification": archived_after_verification,
        "archive_trigger": config["directory_contract"]["archive_trigger"],
        "job_status": job_result["status"],
    }


def _safe_tail_log(helper: ErieHelper, server: str, job_id: str, settings: Path) -> str:
    try:
        return helper.tail_log(server, job_id, settings=settings, lines=80)
    except RemoteAcceptanceError as exc:
        return f"tail_log_unavailable: {exc}"


def _remote_runner_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
: "${HLS_SETTINGS_SCRIPT:?}"
: "${HLS_ENV_SETUP_SCRIPT:=}"
: "${HLS_EXPECTED_TOOL:?}"
: "${HLS_READINESS:?}"
HLS_TARGET_PART="${HLS_TARGET_PART:-}"
source "$HLS_SETTINGS_SCRIPT" >/dev/null 2>&1 || true
if [ -n "$HLS_ENV_SETUP_SCRIPT" ] && [ -f "$HLS_ENV_SETUP_SCRIPT" ]; then
  set +u
  source "$HLS_ENV_SETUP_SCRIPT" >/dev/null 2>&1 || true
  set -u
fi
if [[ "$HLS_EXPECTED_TOOL" == */* ]] && [ -x "$HLS_EXPECTED_TOOL" ]; then
  tool_path="$HLS_EXPECTED_TOOL"
else
  tool_path="$(command -v "$HLS_EXPECTED_TOOL" || true)"
fi
if [ -z "$tool_path" ]; then
  echo "HLS_REMOTE_STATUS blocked_vitis_server"
  exit 44
fi
cd artifacts
python3 - "$PWD/hls_config.cfg" "$PWD/remote_vitis.tcl" "remote_vitis_project" "$HLS_READINESS" "$HLS_TARGET_PART" <<'PY'
from pathlib import Path
import sys

cfg_path = Path(sys.argv[1])
tcl_path = Path(sys.argv[2])
project = Path(sys.argv[3])
readiness = sys.argv[4]
target_part = sys.argv[5]
entries = {"syn.file": [], "tb.file": []}
for raw in cfg_path.read_text(encoding="utf-8", errors="ignore").splitlines():
    line = raw.strip()
    if not line or line.startswith("[") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if key in {"syn.file", "tb.file"}:
        entries.setdefault(key, []).append(value)
    else:
        entries[key] = value

def q(value):
    return "{" + str(value).replace("}", "\\\\}") + "}"

lines = [
    f"open_project -reset {q(project)}",
    f"set_top {q(entries.get('syn.top', 'kernel'))}",
]
for item in entries.get("syn.file", []):
    lines.append(f"add_files {q(Path.cwd() / item)}")
for item in entries.get("tb.file", []):
    lines.append(f"add_files -tb {q(Path.cwd() / item)}")
lines.append("open_solution -reset {solution1}")
if entries.get("part"):
    lines.append(f"set_part {q(entries['part'])}")
elif target_part:
    lines.append(f"set_part {q(target_part)}")
if entries.get("clock"):
    lines.append(f"create_clock -period {entries['clock']}")
order = {"static": 0, "compile": 1, "execute": 2, "implement": 3, "cosim": 4}
level = order.get(readiness, 4)
if level >= 1:
    lines.append("csim_design")
if level >= 3:
    lines.append("csynth_design")
if level >= 4:
    lines.append("cosim_design")
lines.append("exit")
tcl_path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
PY
if [ "${tool_path##*/}" = "vitis-run" ]; then
  vitis-run --mode hls --tcl "$PWD/remote_vitis.tcl"
else
  "$tool_path" -f "$PWD/remote_vitis.tcl"
fi
echo "HLS_REMOTE_STATUS passed"
"""


def _board_runner_script(top_function: str) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail
: "${{HLS_SETTINGS_SCRIPT:?}}"
: "${{HLS_PLATFORM_NAME:?}}"
: "${{HLS_TOP_FUNCTION:?}}"
HLS_TARGET_PART="${{HLS_TARGET_PART:-}}"
HLS_XRT_SETUP_SCRIPT="${{HLS_XRT_SETUP_SCRIPT:-}}"
HLS_VPP_TOOL="${{HLS_VPP_TOOL:-v++}}"
HLS_XRT_TOOL="${{HLS_XRT_TOOL:-}}"
source "$HLS_SETTINGS_SCRIPT" >/dev/null 2>&1 || true
if [ -n "$HLS_XRT_SETUP_SCRIPT" ] && [ -f "$HLS_XRT_SETUP_SCRIPT" ]; then
  source "$HLS_XRT_SETUP_SCRIPT" >/dev/null 2>&1 || true
fi
if ! command -v "$HLS_VPP_TOOL" >/dev/null 2>&1 && [ ! -x "$HLS_VPP_TOOL" ]; then
  echo "{BOARD_STATUS_MARKER} blocked_vpp"
  exit 45
fi
if ! command -v g++ >/dev/null 2>&1; then
  echo "{BOARD_STATUS_MARKER} blocked_gpp"
  exit 46
fi
if ! command -v xrt-smi >/dev/null 2>&1 && ! command -v xbutil >/dev/null 2>&1 && {{ [ -z "$HLS_XRT_TOOL" ] || [ ! -x "$HLS_XRT_TOOL" ]; }}; then
  echo "{BOARD_STATUS_MARKER} blocked_xrt"
  exit 47
fi
XRT_INCLUDE_DIR="${{XILINX_XRT:-/opt/xilinx/xrt}}/include"
XRT_LIB_DIR="${{XILINX_XRT:-/opt/xilinx/xrt}}/lib"
export LD_LIBRARY_PATH="$XRT_LIB_DIR${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
cd artifacts
SRC_FILE="$(find src -maxdepth 1 -type f \\( -name '*.cpp' -o -name '*.cc' -o -name '*.cxx' \\) | head -n 1)"
if [ -z "$SRC_FILE" ]; then
  echo "{BOARD_STATUS_MARKER} missing_kernel_source"
  exit 48
fi
"$HLS_VPP_TOOL" -c -t hw --platform "$HLS_PLATFORM_NAME" -k "$HLS_TOP_FUNCTION" "$SRC_FILE" -o kernel.xo
"$HLS_VPP_TOOL" -l -t hw --platform "$HLS_PLATFORM_NAME" kernel.xo -o kernel.xclbin
g++ -std=c++17 -O2 ../board/host.cpp -I"$XRT_INCLUDE_DIR" -L"$XRT_LIB_DIR" -Wl,-rpath,"$XRT_LIB_DIR" -lxrt_coreutil -pthread -o host.exe
set +e
./host.exe kernel.xclbin 2>&1 | tee board_run.log
host_rc=${{PIPESTATUS[0]}}
set -e
if [ "$host_rc" -ne 0 ] && grep -qi "Permission denied Device index 0" board_run.log && command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
  set +e
  sudo -n env LD_LIBRARY_PATH="$LD_LIBRARY_PATH" XILINX_XRT="${{XILINX_XRT:-/opt/xilinx/xrt}}" ./host.exe kernel.xclbin 2>&1 | tee board_run.log
  host_rc=${{PIPESTATUS[0]}}
  set -e
fi
if [ "$host_rc" -ne 0 ]; then
  exit "$host_rc"
fi
grep -q "{BOARD_STATUS_MARKER} passed" board_run.log
echo "{BOARD_STATUS_MARKER} passed"
"""


def _write_erie_settings_overlay(config: dict[str, Any], run_dir: Path) -> Path:
    base_settings_path = Path(config["erie_settings_path"])
    settings = json.loads(base_settings_path.read_text(encoding="utf-8"))
    settings.setdefault("paths", {})
    settings["paths"]["default_server_list"] = str(_resolve_erie_server_list(settings, base_settings_path, Path(config["erie_skill_dir"])))
    settings["paths"]["requests_dir"] = str(run_dir / "requests")
    settings["paths"]["downloads_dir"] = str(run_dir / "downloads")
    settings["paths"]["validation_tmp_dir"] = str(run_dir / "tmp")
    upload_roots = [str(skill_root().parents[1])]
    for item in settings["paths"].get("upload_roots", []):
        if isinstance(item, str) and item not in upload_roots:
            upload_roots.append(item)
    settings["paths"]["upload_roots"] = upload_roots
    path = run_dir / "erie_settings.overlay.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _resolve_erie_server_list(settings: dict[str, Any], settings_path: Path, erie_skill_dir: Path) -> Path:
    raw = str(settings.get("paths", {}).get("default_server_list") or "").strip()
    if not raw:
        raise RemoteAcceptanceError("erie-remote-ssh settings are missing paths.default_server_list. Ask the user to configure the remote server list before continuing.")
    replacements = {
        "skill_dir": str(erie_skill_dir),
        "settings_dir": str(settings_path.parent),
        "home": str(Path.home()),
    }
    for key, value in replacements.items():
        raw = raw.replace("${" + key + "}", value)
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def _new_run_dir(config: dict[str, Any], prefix: str) -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = skill_root() / str(config["local_run_root"]) / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_report(run_dir: Path, result: dict[str, Any]) -> None:
    _write_json(run_dir / "result.json", result)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _planned_steps(
    mode: str,
    server: str,
    profile: str,
    readiness: str,
    *,
    cleanup_remote: bool = False,
    example_spec: str = "",
    validate_server: str | None = None,
    topology: str = "single_server",
) -> list[str]:
    steps = ["erie discover", "erie list", f"erie check {server}", f"erie workspace-check {server}"]
    if topology == "split_build_validate" and validate_server:
        steps.extend([f"erie check {validate_server}", f"erie workspace-check {validate_server}"])
    if mode == "link":
        steps.append("erie exec read-only UTF-8 link probe")
    elif mode == "board":
        profile_label = profile or "<user-configured-profile>"
        steps.extend(
            [
                f"erie exec board profile probe {profile_label}",
                "erie exec hardware fingerprint probe for 9950X/U55C evidence",
                f"generate local HLS mock artifacts from {example_spec or 'default example'}",
                "render validation-only board host scaffold",
                "ensure governed remote project root and project-local conda prefix",
                "prepare governed remote run directory under runs/<run-id>",
                "erie request command payload transfer",
                "erie exec detached board compile/link/host-run sequence",
                "archive verified remote run into backups/<run-id>",
            ]
        )
    else:
        profile_label = profile or "<user-configured-profile>"
        steps.extend(
            [
                f"erie exec Vitis profile probe {profile_label}",
                f"generate local HLS mock artifacts from {example_spec or 'default example'}",
                "ensure governed remote project root and project-local conda prefix",
                "prepare governed remote run directory under runs/<run-id>",
                "erie request command payload transfer",
                f"erie request command Vitis {readiness}",
                "archive verified remote run into backups/<run-id>",
            ]
        )
        if topology == "split_build_validate" and validate_server:
            steps.extend(["erie exec validation server device probe", "prepare governed validation run directory", "erie request command payload transfer validation", f"erie request command validation Vitis {readiness}"])
        if cleanup_remote:
            steps.append("keep archived backup and skip active-directory deletion because archive is mandatory")
    return steps


def _parse_request_path(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("request:"):
            return line.split(":", 1)[1].strip()
    raise RemoteAcceptanceError(f"Could not find request path in erie output: {stdout}")


def _field_from_output(output: str, key: str) -> str:
    prefix = f"{key}: "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line.split(prefix, 1)[1].strip()
    return ""


def _reject_decode_noise(output: str) -> None:
    if "UnicodeDecodeError" in output or "_readerthread" in output:
        raise RemoteAcceptanceError(f"erie-remote-ssh output decoding failed. {UTF8_HINT}")


def _format_result(result: dict[str, Any]) -> str:
    lines = [f"status: {result.get('status')}"]
    for key in (
        "mode",
        "topology",
        "server",
        "build_server",
        "validate_server",
        "profile",
        "vitis_version",
        "readiness",
        "example_spec",
        "run_dir",
        "run_id",
        "remote_project_root",
        "remote_conda_prefix",
        "remote_run_dir",
        "remote_backup_dir",
        "remote_dir",
        "remote_vitis_version_request",
        "remote_vitis_profile_request",
    ):
        if result.get(key) is not None:
            lines.append(f"{key}: {result[key]}")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    if result.get("missing_fields"):
        lines.append("missing_fields: " + ", ".join(str(item) for item in result["missing_fields"]))
    if result.get("probe"):
        lines.append(f"probe: {result['probe'].get('status')}")
    if result.get("remote_artifacts_retained") is not None:
        lines.append(f"remote_artifacts_retained: {result['remote_artifacts_retained']}")
    if result.get("cleanup_performed") is not None:
        lines.append(f"cleanup_performed: {result['cleanup_performed']}")
    if result.get("archived_after_verification") is not None:
        lines.append(f"archived_after_verification: {result['archived_after_verification']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
