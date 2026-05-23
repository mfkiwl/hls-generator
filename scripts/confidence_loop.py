#!/usr/bin/env python3
"""Run repeatable Erie HLS Generator confidence gates."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from runtime.hls_generator import __version__  # noqa: E402
from integration.hls_adapter import run_hls_workflow, validate_hls_artifacts  # noqa: E402
from runtime.hls_generator.board_acceptance import partition_example_specs_by_board_acceptance  # noqa: E402
from runtime.hls_generator.config import generated_roots, skill_config_path, skill_dependencies_config  # noqa: E402
from runtime.hls_generator.remote_directory_contract import validate_remote_result_contract  # noqa: E402
from runtime.hls_generator.route_contract import load_remote_route_contract, validate_remote_route_target  # noqa: E402
from runtime.hls_generator.skill_dependencies import check_skill_dependencies  # noqa: E402

FORBIDDEN_REFERENCE_TERMS = ("vitis-hls-introductory-examples",)
COPYRIGHT_TERM_PARTS = (
    ("off", "icial"),
    ("tuto", "rials"),
    ("Vitis-", "Tuto", "rials"),
    ("UG", "1399"),
)
TEXT_SCAN_EXTENSIONS = {".md", ".py", ".json", ".yaml", ".yml", ".txt"}
SKIP_SCAN_DIRS = {".git", "__pycache__", ".pytest_cache", "reports", "tests", "smoke", *generated_roots()}
RELEASE_SENSITIVITY_PATTERNS = (
    re.compile(re.escape("/" + "tools" + "/Xilinx/"), re.IGNORECASE),
    re.compile(re.escape(r"C:" + "\\" + "Users" + "\\"), re.IGNORECASE),
    re.compile(re.escape("server_list" + ".local" + ".json"), re.IGNORECASE),
    re.compile(re.escape("xcu50" + "-fsvh2104-2-e"), re.IGNORECASE),
)
RELEASE_SENSITIVITY_EXEMPT_REL_PATHS = {
    "scripts/remote_vitis_acceptance.py",
    "smoke/run_smoke.py",
    "tests/test_remote_vitis_acceptance.py",
    "tests/test_user_config.py",
}
PASS_STATUS = "passed"


def repo_root() -> Path:
    return SKILL_ROOT.parents[1]


def _cleanup_ephemeral_validation_dirs() -> None:
    for base in (repo_root(), SKILL_ROOT):
        cache_dir = base / ".pytest_cache"
        if not cache_dir.exists():
            continue
        for path in sorted(cache_dir.rglob("*"), reverse=True):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            except (FileNotFoundError, OSError):
                continue
        try:
            cache_dir.rmdir()
        except (FileNotFoundError, OSError):
            continue


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run Erie HLS Generator local and optional remote confidence gates.")
    parser.add_argument("--server", help="Optional erie-remote-ssh server for real remote Vitis validation.")
    parser.add_argument("--build-server", help="Optional split-topology build server for real remote Vitis validation.")
    parser.add_argument("--validate-server", help="Optional split-topology validation server for real remote Vitis validation.")
    parser.add_argument("--vitis-version", help="Explicit remote Vitis version to use for remote matrix validation.")
    parser.add_argument("--readiness", default="cosim", choices=("static", "compile", "execute", "implement", "cosim"))
    parser.add_argument("--example-spec", action="append", help="Example spec to use for optional remote validation. Can be repeated.")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--skip-compileall", action="store_true")
    parser.add_argument("--skip-quick-validate", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--skip-remote", action="store_true")
    parser.add_argument("--gate-timeout-s", type=int, default=900, help="Timeout for each local confidence gate command.")
    parser.add_argument("--json-out", help="Write JSON summary to this path.")
    args = parser.parse_args(argv)

    run_root = SKILL_ROOT / "reports" / "confidence-loop" / f"{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-pid{os.getpid()}"
    run_root.mkdir(parents=True, exist_ok=True)

    gates: dict[str, dict[str, Any]] = {}
    if not args.skip_smoke:
        gates["smoke"] = _run_command([sys.executable, "smoke/run_smoke.py"], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    if not args.skip_compileall:
        gates["compileall"] = _run_command([sys.executable, "-m", "compileall", "runtime/hls_generator"], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    if not args.skip_quick_validate:
        gates["quick_validate"] = _run_command([sys.executable, "scripts/quick_validate.py", str(SKILL_ROOT)], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    if not args.skip_pytest:
        gates["pytest"] = _run_command([sys.executable, "-m", "pytest", "-q", "tests"], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    _cleanup_ephemeral_validation_dirs()
    gates["verify_agents"] = _run_command([sys.executable, "scripts/verify_agents.py", str(repo_root())], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    gates["manage_docs_verify"] = _run_command([sys.executable, "scripts/manage_docs.py", "verify", str(repo_root())], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    gates["manage_dirs_verify"] = _run_command([sys.executable, "scripts/manage_dirs.py", "verify", str(repo_root())], cwd=SKILL_ROOT, timeout_s=args.gate_timeout_s)
    gates["skill_dependencies"] = _skill_dependency_gate()
    gates["copyright_term_scan"] = _copyright_term_scan()
    gates["release_sensitivity_scan"] = _release_sensitivity_scan()
    gates["forbidden_reference_names"] = _forbidden_reference_name_scan()
    example_specs = _example_spec_names()
    if gates["skill_dependencies"]["status"] == "passed":
        examples_gate, example_specs = _validate_examples(run_root)
    else:
        examples_gate = {"status": "skipped", "reason": "blocked_dependency", "results": []}
    gates["example_mock_validation"] = examples_gate
    gates["comment_policy"] = _comment_policy_gate(run_root) if gates["skill_dependencies"]["status"] == "passed" else {"status": "skipped", "reason": "blocked_dependency"}
    gates["forward_test"] = _forward_test_gate(run_root) if gates["skill_dependencies"]["status"] == "passed" else {"status": "skipped", "reason": "blocked_dependency", "results": []}
    remote_results: list[dict[str, Any]] = []
    split_remote_requested = bool(args.build_server and args.validate_server and not args.skip_remote)
    remote_requested = bool((args.server or split_remote_requested) and not args.skip_remote)
    gates["route_contract"] = _route_contract_gate(
        args.server,
        args.build_server,
        args.validate_server,
        remote_requested=remote_requested,
    )
    board_partition = _board_acceptance_partition_gate()
    gates["board_acceptance_declarations"] = board_partition
    if split_remote_requested:
        selected_remote_specs = args.example_spec or example_specs
        if gates["route_contract"]["status"] == "passed":
            gates["remote_vitis_acceptance"] = _run_split_remote_acceptance(
                args.build_server,
                args.validate_server,
                args.readiness,
                selected_remote_specs,
                vitis_version=args.vitis_version,
            )
            remote_results = gates["remote_vitis_acceptance"].get("results", [])
    elif remote_requested and gates["route_contract"]["status"] == "passed":
        selected_remote_specs = args.example_spec or example_specs
        gates["remote_vitis_acceptance"] = _run_remote_acceptance(args.server, args.readiness, selected_remote_specs, vitis_version=args.vitis_version)
        remote_results = gates["remote_vitis_acceptance"]["results"]
    gates["remote_directory_contract"] = _remote_directory_contract_gate(remote_results, remote_requested=remote_requested)
    gates["remote_board_acceptance"] = _remote_board_acceptance_gate(
        args.server,
        args.readiness,
        vitis_version=args.vitis_version,
        remote_requested=remote_requested and not split_remote_requested,
        remote_vitis_gate=gates.get("remote_vitis_acceptance"),
        board_partition=board_partition,
        selected_specs=args.example_spec or example_specs,
    )

    confidence_status, confidence_scope, residual_risks, returncode = _confidence_outcome(
        gates,
        remote_requested=remote_requested,
        remote_skipped=bool(args.skip_remote),
    )
    payload = {
        "version": 1,
        "confidence_status": confidence_status,
        "confidence_scope": confidence_scope,
        "run_root": str(run_root),
        "gates": gates,
        "example_specs": example_specs,
        "remote_results": remote_results,
        "residual_risks": residual_risks,
    }
    if args.json_out:
        output_path = _resolve_json_output(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return returncode


def _run_command(command: list[str], *, cwd: Path, timeout_s: int = 900) -> dict[str, Any]:
    result = _run_process(command, cwd=cwd, timeout_s=timeout_s)
    if result["timed_out"]:
        return {
            "status": "timeout",
            "command": command,
            "returncode": None,
            "timeout_s": timeout_s,
            "stdout_tail": _tail(result["stdout"]),
            "stderr_tail": _tail(result["stderr"]),
        }
    return {
        "status": "passed" if result["returncode"] == 0 else "failed",
        "command": command,
        "returncode": result["returncode"],
        "timeout_s": timeout_s,
        "stdout_tail": _tail(result["stdout"]),
        "stderr_tail": _tail(result["stderr"]),
    }


def _run_process(command: list[str], *, cwd: Path, timeout_s: int) -> dict[str, Any]:
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return {"timed_out": False, "returncode": process.returncode, "stdout": stdout or "", "stderr": stderr or ""}
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return {"timed_out": True, "returncode": None, "stdout": stdout or "", "stderr": stderr or ""}


def _terminate_process_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False)
        return
    try:
        os.killpg(pid, 9)
    except ProcessLookupError:
        return


def _quick_validate_path() -> Path:
    return Path.home() / ".codex" / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"


def _skill_dependency_gate() -> dict[str, Any]:
    try:
        dependencies = skill_dependencies_config()
        report = check_skill_dependencies(dependencies, scopes={"core"})
    except ValueError as exc:
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "passed" if report["status"] == "ok" else "failed",
        "dependency_count": len(dependencies),
        "report": report,
    }


def _copyright_term_scan(*, root: Path | None = None) -> dict[str, Any]:
    scan_root = (root or SKILL_ROOT).resolve()
    matches: list[str] = []
    term_patterns = [(term, re.compile(re.escape(term), re.IGNORECASE)) for term in _copyright_terms()]
    for path in [scan_root, *scan_root.rglob("*")]:
        if path != scan_root and any(part in SKIP_SCAN_DIRS for part in path.relative_to(scan_root).parts):
            continue
        if path != scan_root:
            for term, pattern in term_patterns:
                if pattern.search(path.relative_to(scan_root).as_posix()):
                    matches.append(f"path:{path.relative_to(scan_root).as_posix()}:{term}")
        if not path.is_file() or path.suffix.lower() not in TEXT_SCAN_EXTENSIONS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for term, pattern in term_patterns:
            if pattern.search(text):
                matches.append(f"content:{path.relative_to(scan_root).as_posix()}:{term}")
    return {
        "status": "passed" if not matches else "failed",
        "root": str(scan_root),
        "matches": matches,
    }


def _forbidden_reference_name_scan() -> dict[str, Any]:
    result = subprocess.run(
        ["rg", "-n", *sum((["--glob", item] for item in _scan_exclude_globs()), []), "|".join(FORBIDDEN_REFERENCE_TERMS), "."],
        cwd=SKILL_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    unexpected = [line for line in lines if not line.split(":", 1)[0].replace("\\", "/").startswith("ref/")]
    return {
        "status": "passed" if result.returncode in {0, 1} and not unexpected else "failed",
        "command": ["rg", "-n", *sum((["--glob", item] for item in _scan_exclude_globs()), []), "|".join(FORBIDDEN_REFERENCE_TERMS), "."],
        "matches": lines,
        "unexpected_matches": unexpected,
    }


def _release_sensitivity_scan(*, root: Path | None = None) -> dict[str, Any]:
    scan_root = (root or SKILL_ROOT).resolve()
    roots = [scan_root]
    if root is None:
        release_dir = repo_root() / "dist" / f"erie-hls-generator-v{__version__}"
        release_zip = repo_root() / "dist" / f"erie-hls-generator-v{__version__}.zip"
        if release_dir.exists():
            roots.append(release_dir)
        if release_zip.exists():
            roots.append(release_zip)
    matches: list[str] = []
    for active_root in roots:
        if active_root.is_file() and active_root.suffix.lower() == ".zip":
            matches.extend(_scan_release_zip(active_root))
            continue
        for path in [active_root, *active_root.rglob("*")]:
            if path != active_root and any(part in SKIP_SCAN_DIRS for part in path.relative_to(active_root).parts):
                continue
            rel_path = path.relative_to(active_root).as_posix() if path != active_root else "."
            for pattern in RELEASE_SENSITIVITY_PATTERNS:
                if pattern.search(rel_path):
                    matches.append(f"path:{active_root.name}:{rel_path}:{pattern.pattern}")
            if not path.is_file() or path.suffix.lower() not in TEXT_SCAN_EXTENSIONS:
                continue
            if _release_sensitivity_is_exempt(rel_path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in RELEASE_SENSITIVITY_PATTERNS:
                if pattern.search(text):
                    matches.append(f"content:{active_root.name}:{rel_path}:{pattern.pattern}")
    return {
        "status": "passed" if not matches else "failed",
        "roots": [str(item) for item in roots],
        "matches": matches,
    }


def _scan_release_zip(archive_path: Path) -> list[str]:
    matches: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            rel_name = name.rstrip("/")
            if not rel_name:
                continue
            rel_path = rel_name.replace("\\", "/")
            for pattern in RELEASE_SENSITIVITY_PATTERNS:
                if pattern.search(rel_path):
                    matches.append(f"path:{archive_path.name}:{rel_path}:{pattern.pattern}")
            if Path(rel_path).suffix.lower() not in TEXT_SCAN_EXTENSIONS or rel_name.endswith("/"):
                continue
            if _release_sensitivity_is_exempt(rel_path):
                continue
            text = archive.read(name).decode("utf-8", errors="replace")
            for pattern in RELEASE_SENSITIVITY_PATTERNS:
                if pattern.search(text):
                    matches.append(f"content:{archive_path.name}:{rel_path}:{pattern.pattern}")
    return matches


def _release_sensitivity_is_exempt(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    skill_marker = f"/{SKILL_ROOT.relative_to(repo_root()).as_posix().rstrip('/')}/"
    if skill_marker in f"/{normalized}":
        normalized = f"/{normalized}".split(skill_marker, 1)[1]
    return normalized in RELEASE_SENSITIVITY_EXEMPT_REL_PATHS


def _relative_match_path(line: str) -> str:
    path_text = line.split(":", 1)[0].replace("\\", "/")
    if path_text.startswith("./"):
        path_text = path_text[2:]
    root = SKILL_ROOT.as_posix().rstrip("/") + "/"
    if path_text.startswith(root):
        return path_text[len(root) :]
    marker = SKILL_ROOT.relative_to(repo_root()).as_posix().rstrip("/") + "/"
    if marker in path_text:
        return path_text.split(marker, 1)[1]
    return path_text


def _validate_examples(run_root: Path) -> tuple[dict[str, Any], list[str]]:
    examples_dir = skill_config_path("examples_dir")
    spec_paths = sorted(examples_dir.glob("*.json"))
    results: list[dict[str, Any]] = []
    for spec_path in spec_paths:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        out_dir = run_root / spec_path.stem
        result = run_hls_workflow(spec, out_dir=out_dir, provider_name="mock", readiness="static", run_external=False, comment_language="zh")
        artifact_dir = Path(result["run_dir"]) / "attempt-001" / "hls" / "artifacts"
        report = validate_hls_artifacts(spec, artifact_dir, readiness="static", run_external=False, comment_language="zh") if artifact_dir.exists() else {"ok": False, "errors": 1, "warnings": 0}
        results.append(
            {
                "spec": spec_path.name,
                "workflow_status": result.get("status"),
                "validation_ok": bool(report.get("ok")),
                "errors": report.get("errors"),
                "warnings": report.get("warnings"),
            }
        )
    passed = all(item["workflow_status"] == "passed" and item["validation_ok"] for item in results)
    return {"status": "passed" if passed else "failed", "results": results}, [path.name for path in spec_paths]


def _example_spec_names() -> list[str]:
    examples_dir = skill_config_path("examples_dir")
    return [path.name for path in sorted(examples_dir.glob("*.json"))]


def _forward_test_gate(run_root: Path) -> dict[str, Any]:
    spec_names = [
        "hls_2d_block_transform_spec.json",
        "hls_array_reshape_vector_scale_spec.json",
        "hls_axi4_burst_vector_scale_spec.json",
        "hls_dataflow_axis_spec.json",
        "hls_directio_freerun_axis_spec.json",
        "hls_fixed_point_scale_spec.json",
        "hls_host_kernel_split_spec.json",
        "hls_minimal_vitis_pipeline_spec.json",
        "hls_multi_m_axi_add_spec.json",
        "hls_partition_vector_scale_spec.json",
        "hls_streamofblocks_axis_spec.json",
        "hls_task_graph_axis_spec.json",
    ]
    results: list[dict[str, Any]] = []
    for spec_name in spec_names:
        spec_path = skill_config_path("examples_dir") / spec_name
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        out_dir = run_root / "forward-test" / spec_path.stem
        result = run_hls_workflow(spec, out_dir=out_dir, provider_name="mock", readiness="static", run_external=False, comment_language="zh")
        artifact_dir = Path(result["run_dir"]) / "attempt-001" / "hls" / "artifacts"
        report = validate_hls_artifacts(spec, artifact_dir, readiness="static", run_external=False, comment_language="zh") if artifact_dir.exists() else {"ok": False, "errors": 1, "warnings": 0}
        results.append(
            {
                "spec": spec_name,
                "workflow_status": result.get("status"),
                "validation_ok": bool(report.get("ok")),
                "errors": report.get("errors"),
                "warnings": report.get("warnings"),
                "mode": "near_real_spec_static",
            }
        )
    passed = all(item["workflow_status"] == "passed" and item["validation_ok"] for item in results)
    return {"status": "passed" if passed else "failed", "results": results}


def _comment_policy_gate(run_root: Path) -> dict[str, Any]:
    spec_path = skill_config_path("examples_dir") / "hls_vector_scale_spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_dir = run_root / "comment-policy"
    result = run_hls_workflow(spec, out_dir=out_dir / "good", provider_name="mock", readiness="static", run_external=False, comment_language="en")
    artifact_dir = Path(result["run_dir"]) / "attempt-001" / "hls" / "artifacts"
    good_report = validate_hls_artifacts(spec, artifact_dir, readiness="static", run_external=False, comment_language="en") if artifact_dir.exists() else {"ok": False, "issues": [], "metrics": {}}

    bad_dir = out_dir / "bad"
    if artifact_dir.exists():
        shutil.copytree(artifact_dir, bad_dir)
        for path in sorted(bad_dir.glob("**/*")):
            if path.suffix.lower() not in {".h", ".hpp", ".cpp", ".cc", ".cxx"}:
                continue
            text = re.sub(r"//.*$", "// generic generated line, not hardware intent", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
            path.write_text(text, encoding="utf-8")
    bad_report = validate_hls_artifacts(spec, bad_dir, readiness="static", run_external=False, comment_language="en") if bad_dir.exists() else {"ok": True, "issues": [], "metrics": {}}
    bad_messages = "\n".join(str(issue.get("message", "")) for issue in bad_report.get("issues", []))
    passed = (
        result.get("status") == "passed"
        and bool(good_report.get("ok"))
        and good_report.get("metrics", {}).get("comment_policy", {}).get("policy") == "typed_hls_comment_placement"
        and not bool(bad_report.get("ok"))
        and "comment policy" in bad_messages.lower()
        and "generic" in bad_messages.lower()
    )
    return {
        "status": "passed" if passed else "failed",
        "good_workflow_status": result.get("status"),
        "good_validation_ok": bool(good_report.get("ok")),
        "bad_validation_ok": bool(bad_report.get("ok")),
        "bad_issue_count": len(bad_report.get("issues", [])),
    }


def _route_contract_gate(
    server: str | None,
    build_server: str | None,
    validate_server: str | None,
    *,
    remote_requested: bool,
) -> dict[str, Any]:
    contract = load_remote_route_contract(SKILL_ROOT)
    if not remote_requested:
        return {"status": "passed", "mode": "not_requested", "contract": contract}
    issues = validate_remote_route_target(
        contract,
        server=server,
        build_server=build_server,
        validate_server=validate_server,
    )
    return {
        "status": "passed" if not issues else "failed",
        "mode": "remote_requested",
        "contract": contract,
        "issues": issues,
    }


def _board_acceptance_partition_gate() -> dict[str, Any]:
    partition = partition_example_specs_by_board_acceptance(skill_config_path("examples_dir"))
    invalid_specs = partition["invalid_specs"]
    return {
        "status": "passed" if not invalid_specs else "failed",
        **partition,
    }


def _remote_directory_contract_gate(remote_results: list[dict[str, Any]], *, remote_requested: bool) -> dict[str, Any]:
    if not remote_requested:
        return {"status": "passed", "mode": "static_contract_only", "results": []}
    if not remote_results:
        return {"status": "failed", "mode": "remote_required", "results": [], "issues": ["remote results missing"]}
    results: list[dict[str, Any]] = []
    for item in remote_results:
        errors = validate_remote_result_contract(item)
        results.append(
            {
                "example_spec": str(item.get("example_spec") or item.get("phase") or ""),
                "run_id": item.get("run_id"),
                "status": "passed" if not errors else "failed",
                "issues": errors,
            }
        )
    passed = all(entry["status"] == "passed" for entry in results)
    return {"status": "passed" if passed else "failed", "mode": "remote_result_validation", "results": results}


def _run_remote_command(command: list[str], *, timeout_s: int = 900) -> dict[str, Any]:
    result = _run_process(command, cwd=SKILL_ROOT, timeout_s=timeout_s)
    if result["timed_out"]:
        return {
            "status": "timeout",
            "command": command,
            "returncode": None,
            "timeout_s": timeout_s,
            "stdout_tail": _tail(result["stdout"]),
            "stderr_tail": _tail(result["stderr"]),
        }
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError:
        payload = {"status": "failed", "stdout_tail": _tail(result["stdout"]), "stderr_tail": _tail(result["stderr"])}
    payload["returncode"] = result["returncode"]
    payload["timeout_s"] = timeout_s
    return payload


def _run_remote_acceptance(server: str, readiness: str, example_specs: list[str], *, vitis_version: str | None = None) -> dict[str, Any]:
    link_payload = _run_remote_command(
        [
            sys.executable,
            "scripts/remote_vitis_acceptance.py",
            "--mode",
            "link",
            "--server",
            server,
            "--timeout",
            "300",
            "--json",
        ]
    )
    if link_payload.get("status") != "passed":
        return {
            "status": "failed",
            "server": server,
            "vitis_version": vitis_version,
            "link": link_payload,
            "results": [],
        }
    vitis_results = [_run_remote(server, readiness, spec_name, vitis_version=vitis_version) for spec_name in example_specs]
    passed = link_payload.get("status") == "passed" and all(item.get("status") == "passed" and item.get("remote_artifacts_retained") is True for item in vitis_results)
    return {
        "status": "passed" if passed else "failed",
        "server": server,
        "vitis_version": vitis_version,
        "link": link_payload,
        "results": vitis_results,
    }


def _run_remote(server: str, readiness: str, spec_name: str, *, vitis_version: str | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/remote_vitis_acceptance.py",
        "--mode",
        "vitis",
        "--server",
        server,
        "--readiness",
        readiness,
        "--example-spec",
        spec_name,
        "--comment-language",
        "zh",
        "--json",
    ]
    if vitis_version:
        command.extend(["--vitis-version", vitis_version])
    payload = _run_remote_command(command, timeout_s=5400)
    payload["example_spec"] = spec_name
    return payload


def _run_remote_board(server: str, readiness: str, spec_name: str, *, vitis_version: str | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/remote_vitis_acceptance.py",
        "--mode",
        "board",
        "--server",
        server,
        "--readiness",
        readiness,
        "--example-spec",
        spec_name,
        "--comment-language",
        "zh",
        "--timeout",
        "5400",
        "--json",
    ]
    if vitis_version:
        command.extend(["--vitis-version", vitis_version])
    payload = _run_remote_command(command, timeout_s=5400)
    payload["example_spec"] = spec_name
    return payload


def _run_split_remote(build_server: str, validate_server: str, readiness: str, spec_name: str, *, vitis_version: str | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/remote_vitis_acceptance.py",
        "--mode",
        "vitis",
        "--build-server",
        build_server,
        "--validate-server",
        validate_server,
        "--readiness",
        readiness,
        "--example-spec",
        spec_name,
        "--comment-language",
        "zh",
        "--json",
    ]
    if vitis_version:
        command.extend(["--vitis-version", vitis_version])
    payload = _run_remote_command(command)
    payload["example_spec"] = spec_name
    return payload


def _run_split_remote_acceptance(build_server: str, validate_server: str, readiness: str, example_specs: list[str], *, vitis_version: str | None = None) -> dict[str, Any]:
    if not example_specs:
        return {
            "status": "failed",
            "topology": "split_build_validate",
            "build_server": build_server,
            "validate_server": validate_server,
            "vitis_version": vitis_version,
            "results": [],
        }
    first_result = _run_split_remote(build_server, validate_server, readiness, example_specs[0], vitis_version=vitis_version)
    if first_result.get("status") != "passed":
        return {
            "status": "failed",
            "topology": "split_build_validate",
            "build_server": build_server,
            "validate_server": validate_server,
            "vitis_version": vitis_version,
            "results": [],
            "first_result": first_result,
        }
    remaining = [_run_split_remote(build_server, validate_server, readiness, spec_name, vitis_version=vitis_version) for spec_name in example_specs[1:]]
    results = [first_result, *remaining]
    passed = all(item.get("status") == "passed" and item.get("remote_artifacts_retained") is True for item in results)
    return {
        "status": "passed" if passed else "failed",
        "topology": "split_build_validate",
        "build_server": build_server,
        "validate_server": validate_server,
        "vitis_version": vitis_version,
        "results": results,
    }


def _remote_board_acceptance_gate(
    server: str | None,
    readiness: str,
    *,
    vitis_version: str | None,
    remote_requested: bool,
    remote_vitis_gate: dict[str, Any] | None,
    board_partition: dict[str, Any],
    selected_specs: list[str],
) -> dict[str, Any]:
    invalid_specs = board_partition.get("invalid_specs", [])
    if invalid_specs:
        return {"status": "failed", "reason": "invalid_board_acceptance_metadata", "invalid_specs": invalid_specs}
    board_specs = [entry for entry in board_partition.get("board_specs", []) if entry["spec"] in set(selected_specs)]
    exempt_specs = [entry for entry in board_partition.get("exempt_specs", []) if entry["spec"] in set(selected_specs)]
    if not remote_requested:
        return {
            "status": "passed",
            "mode": "declarations_only",
            "board_specs": board_specs,
            "exempt_specs": exempt_specs,
            "results": [],
        }
    if not server:
        return {"status": "failed", "reason": "board acceptance requires a single remote server", "results": []}
    if not remote_vitis_gate or remote_vitis_gate.get("status") != "passed":
        return {"status": "blocked", "reason": "board acceptance requires successful remote vitis acceptance first", "results": []}
    if not board_specs:
        return {"status": "passed", "mode": "no_board_specs_selected", "board_specs": [], "exempt_specs": exempt_specs, "results": []}
    results = [_run_remote_board(server, readiness, entry["spec"], vitis_version=vitis_version) for entry in board_specs]
    statuses = {str(item.get("status")) for item in results}
    if statuses == {PASS_STATUS}:
        status = "passed"
    elif any(item in statuses for item in {"blocked_board_validation", "blocked_remote_profile_config", "blocked_remote_version_choice"}):
        status = "blocked"
    else:
        status = "failed"
    return {
        "status": status,
        "mode": "remote_board_validation",
        "board_specs": board_specs,
        "exempt_specs": exempt_specs,
        "results": results,
    }


def _confidence_outcome(
    gates: dict[str, dict[str, Any]],
    *,
    remote_requested: bool,
    remote_skipped: bool,
) -> tuple[str, str, list[str], int]:
    local_gate_names = [name for name in gates if name not in {"remote_vitis_acceptance", "remote_board_acceptance"}]
    local_passed = all(gates[name]["status"] == "passed" for name in local_gate_names)
    remote_gate = gates.get("remote_vitis_acceptance")
    board_gate = gates.get("remote_board_acceptance")
    route_gate = gates.get("route_contract")
    if remote_requested:
        if route_gate and route_gate.get("status") != "passed":
            risks = _residual_risks("blocked_remote_validation", remote_requested=True, remote_skipped=False, gates=gates)
            return "blocked_remote_validation", "final", risks, 1
        if local_passed and remote_gate and remote_gate.get("status") == "passed" and board_gate and board_gate.get("status") == "passed":
            return "factual_high_confidence", "final", [], 0
        if board_gate and board_gate.get("status") == "blocked":
            risks = _residual_risks("blocked_remote_validation", remote_requested=True, remote_skipped=False, gates=gates)
            return "blocked_remote_validation", "final", risks, 1
        risks = _residual_risks("needs_attention", remote_requested=True, remote_skipped=False, gates=gates)
        return "needs_attention", "final", risks, 1
    if remote_skipped:
        risks = _residual_risks("local_high_confidence" if local_passed else "needs_attention", remote_requested=False, remote_skipped=True, gates=gates)
        return ("local_high_confidence", "local", risks, 0) if local_passed else ("needs_attention", "local", risks, 1)
    risks = _residual_risks("blocked_remote_validation", remote_requested=False, remote_skipped=False, gates=gates)
    return ("blocked_remote_validation", "final", risks, 1) if local_passed else ("needs_attention", "final", risks, 1)


def _residual_risks(confidence_status: str, *, remote_requested: bool, remote_skipped: bool, gates: dict[str, dict[str, Any]]) -> list[str]:
    risks: list[str] = []
    if confidence_status == "needs_attention":
        risks.append("At least one confidence gate failed; inspect gates for details.")
    if confidence_status == "blocked_remote_validation":
        route_gate = gates.get("route_contract")
        remote_gate = gates.get("remote_vitis_acceptance")
        board_gate = gates.get("remote_board_acceptance")
        if route_gate and route_gate.get("status") == "failed":
            risks.append("Remote route target does not match the AGENTS contract primary server.")
        if remote_gate and remote_gate.get("status") not in {None, "passed"}:
            risks.append("Remote Vitis acceptance did not pass on the routed server.")
        if board_gate and board_gate.get("status") == "blocked":
            board_results = board_gate.get("results", [])
            platform_blocked = False
            suggested_platform = ""
            for item in board_results:
                if str(item.get("status")) != "blocked_board_validation":
                    continue
                if "platform_probe" in set(str(reason) for reason in item.get("blocking_reasons", [])):
                    platform_blocked = True
                    probe = item.get("platform_probe", {}) if isinstance(item.get("platform_probe"), dict) else {}
                    suggested_platform = str(probe.get("suggested_platform_name") or "")
                    break
            if platform_blocked:
                if suggested_platform:
                    risks.append(f"Board acceptance is blocked; the routed host shows an active U55C shell but no matching installed platform/xpfm was found. Suggested platform package: {suggested_platform}.")
                else:
                    risks.append("Board acceptance is blocked; the routed host shows board-level evidence but no matching installed platform/xpfm was found.")
            else:
                risks.append("Board acceptance is blocked; hardware fingerprint or board profile evidence is incomplete.")
    if remote_requested:
        return risks
    if remote_skipped:
        risks.append("Final confidence requires remote Vitis acceptance.")
    else:
        risks.append("Remote Vitis acceptance was not executed.")
    return risks


def _copyright_terms() -> tuple[str, ...]:
    return tuple("".join(parts) for parts in COPYRIGHT_TERM_PARTS)


def _scan_exclude_globs() -> tuple[str, ...]:
    return (
        "!ref/**",
        "!.git/**",
        "!reports/**",
        "!tests/**",
        "!smoke/**",
        "!scripts/confidence_loop.py",
    )


def _tail(text: str, *, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


def _resolve_json_output(path_text: str) -> Path:
    output_path = Path(path_text)
    if output_path.is_absolute():
        return output_path
    parts = output_path.parts
    skill_prefix = tuple(SKILL_ROOT.relative_to(repo_root()).parts)
    if len(parts) >= len(skill_prefix) and tuple(part.lower() for part in parts[: len(skill_prefix)]) == tuple(part.lower() for part in skill_prefix):
        output_path = Path(*parts[len(skill_prefix) :]) if len(parts) > len(skill_prefix) else Path()
    elif parts and parts[0].lower() == SKILL_ROOT.name.lower():
        output_path = Path(*parts[1:]) if len(parts) > 1 else Path()
    return (SKILL_ROOT / output_path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
