"""Static interface-contract extraction for Python and HLS artifacts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .vectors import extract_vector_hashes, find_vector_contracts

INTERFACE_TARGETS = ("python", "hls")


def audit_interface(target: str, root: Path) -> dict[str, Any]:
    normalized = _require_target(target)
    contract = _python_contract(root) if normalized == "python" else _hls_contract(root)
    contract["interface_sha256"] = _stable_hash(contract)
    return contract


def _require_target(target: str) -> str:
    normalized = target.lower()
    if normalized not in INTERFACE_TARGETS:
        raise ValueError("This skill is HLS-only; interface target must be `python` or `hls`.")
    return normalized


def _python_contract(root: Path) -> dict[str, Any]:
    functions: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for path in sorted(root.glob("**/*.py")):
        rel_path = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError as exc:
            issues.append({"severity": "error", "source": "current_module_issue", "message": f"Python parse error: {exc}", "path": rel_path})
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                functions.append({"name": node.name, "args": [arg.arg for arg in node.args.args], "path": rel_path})
    vector_contracts = find_vector_contracts(root)
    return {
        "version": 1,
        "target": "python",
        "source_root": root.name,
        "top": functions[0]["name"] if functions else None,
        "exported_functions": functions,
        "has_run_tests": any(item["name"] == "run_tests" for item in functions),
        "case_ids": _case_ids(vector_contracts),
        "vector_hashes": _vector_hashes(vector_contracts),
        "issues": issues,
    }


def _hls_contract(root: Path) -> dict[str, Any]:
    text_by_path = _read_files(root, ("*.cpp", "*.cc", "*.cxx", "*.h", "*.hpp"))
    combined = "\n".join(text_by_path.values())
    functions = _extract_cpp_functions(combined)
    cfg = _extract_hls_cfg(root)
    top = cfg.get("syn.top") or _first_non_test_function(functions)
    pragmas = _extract_hls_pragmas(combined)
    arguments = _hls_argument_contract(functions, top, pragmas)
    vector_contracts = find_vector_contracts(root)
    case_ids = _case_ids(vector_contracts) or _scan_case_ids(combined)
    issues: list[dict[str, str]] = []
    if cfg.get("syn.top") and functions and cfg.get("syn.top") not in {item["name"] for item in functions}:
        issues.append({"severity": "error", "source": "current_module_issue", "message": "cfg syn.top does not match any HLS source function.", "path": cfg.get("path", "")})
    if not cfg.get("syn.file"):
        issues.append({"severity": "warning", "source": "toolchain_issue", "message": "cfg syn.file is missing.", "path": cfg.get("path", "")})
    return {
        "version": 1,
        "target": "hls",
        "source_root": root.name,
        "top": top,
        "functions": functions,
        "arguments": arguments,
        "control_mode": _hls_control_mode(pragmas),
        "pragmas": pragmas,
        "cfg": cfg,
        "case_ids": case_ids,
        "vector_hashes": _vector_hashes(vector_contracts) or _scan_vector_hashes(text_by_path),
        "issues": issues,
    }


def _read_files(root: Path, suffix_globs: tuple[str, ...]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for pattern in suffix_globs:
        for path in sorted(root.glob(f"**/{pattern}")):
            texts[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8", errors="ignore")
    return texts


def _extract_cpp_functions(text: str) -> list[dict[str, Any]]:
    functions: list[dict[str, Any]] = []
    pattern = re.compile(r"(?:^|\n)\s*(?:extern\s+\"C\"\s+)?(?:[\w:<>*&\s]+?)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^;{}]*)\)\s*(?:\{|;)", re.MULTILINE)
    for match in pattern.finditer(text):
        name = match.group(1)
        if name in {"if", "for", "while", "switch", "return"}:
            continue
        functions.append({"name": name, "args": _parse_cpp_args(match.group(2))})
    return _dedupe_by_name(functions)


def _parse_cpp_args(args_text: str) -> list[dict[str, str]]:
    args: list[dict[str, str]] = []
    for raw_arg in [item.strip() for item in _split_cpp_args(args_text) if item.strip() and item.strip() != "void"]:
        name_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*$", raw_arg)
        name = name_match.group(1) if name_match else raw_arg
        arg_type = raw_arg[: name_match.start(1)].strip() if name_match else ""
        args.append({"name": name, "type": arg_type})
    return args


def _split_cpp_args(args_text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    angle_depth = 0
    paren_depth = 0
    bracket_depth = 0
    for char in args_text:
        if char == "<":
            angle_depth += 1
        elif char == ">" and angle_depth > 0:
            angle_depth -= 1
        elif char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth > 0:
            paren_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth > 0:
            bracket_depth -= 1
        if char == "," and angle_depth == 0 and paren_depth == 0 and bracket_depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _extract_hls_cfg(root: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = {"syn.files": [], "tb.files": []}
    for path in sorted(root.glob("**/*.cfg")):
        cfg["path"] = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = re.match(r"\s*((?:syn|tb)\.file|syn\.top|part|clock)\s*=\s*(\S+)\s*$", line)
            if not match:
                continue
            key, value = match.group(1), match.group(2)
            if key == "syn.file":
                cfg.setdefault("syn.files", []).append(value)
                cfg.setdefault("syn.file", value)
            elif key == "tb.file":
                cfg.setdefault("tb.files", []).append(value)
                cfg.setdefault("tb.file", value)
            else:
                cfg[key] = value
        break
    return cfg


def _extract_hls_pragmas(text: str) -> list[dict[str, str]]:
    pragmas: list[dict[str, str]] = []
    for line in text.splitlines():
        if "#pragma HLS INTERFACE" not in line:
            continue
        pragmas.append({"line": line.strip(), "port": _pragma_value(line, "port"), "mode": _pragma_value(line, "mode") or _pragma_mode(line), "bundle": _pragma_value(line, "bundle")})
    return pragmas


def _hls_argument_contract(functions: list[dict[str, Any]], top: str | None, pragmas: list[dict[str, str]]) -> list[dict[str, str]]:
    arguments = next((item["args"] for item in functions if item["name"] == top), [])
    by_port: dict[str, list[dict[str, str]]] = {}
    for pragma in pragmas:
        port = str(pragma.get("port") or "")
        if port:
            by_port.setdefault(port, []).append(pragma)
    enriched: list[dict[str, str]] = []
    for argument in arguments:
        if not isinstance(argument, dict):
            continue
        name = str(argument.get("name") or "")
        matches = by_port.get(name, [])
        modes = [str(item.get("mode") or "") for item in matches if item.get("mode")]
        bundles = [str(item.get("bundle") or "") for item in matches if item.get("bundle")]
        enriched.append({"name": name, "type": str(argument.get("type") or ""), "interface": modes[0] if modes else "", "bundle": bundles[0] if bundles else ""})
    return enriched


def _hls_control_mode(pragmas: list[dict[str, str]]) -> str | None:
    for pragma in pragmas:
        if str(pragma.get("port") or "") == "return" and pragma.get("mode"):
            return str(pragma["mode"])
    return None


def _pragma_value(line: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*([A-Za-z0-9_]+)", line)
    return match.group(1) if match else ""


def _pragma_mode(line: str) -> str:
    match = re.search(r"#pragma\s+HLS\s+INTERFACE\s+([A-Za-z0-9_]+)", line)
    return match.group(1) if match else ""


def _first_non_test_function(functions: list[dict[str, Any]]) -> str | None:
    for item in functions:
        if item["name"] != "main" and not item["name"].endswith("_tb"):
            return item["name"]
    return functions[0]["name"] if functions else None


def _dedupe_by_name(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name"))
        if name in seen:
            continue
        seen.add(name)
        result.append(item)
    return result


def _case_ids(contracts: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for contract in contracts:
        for case_id in contract.get("case_ids", []) or []:
            if case_id not in ids:
                ids.append(case_id)
    return ids


def _vector_hashes(contracts: list[dict[str, Any]]) -> list[str]:
    hashes: list[str] = []
    for contract in contracts:
        value = contract.get("sha256")
        if value and value not in hashes:
            hashes.append(str(value))
    return hashes


def _scan_case_ids(text: str) -> list[str]:
    ids: list[str] = []
    for match in re.finditer(r"\bcase[_-][A-Za-z0-9_:-]+\b", text, flags=re.IGNORECASE):
        value = match.group(0)
        if value not in ids:
            ids.append(value)
    return ids


def _scan_vector_hashes(text_by_path: dict[str, str]) -> list[str]:
    hashes: list[str] = []
    for text in text_by_path.values():
        for value in extract_vector_hashes(text):
            if value not in hashes:
                hashes.append(value)
    return hashes


def _stable_hash(contract: dict[str, Any]) -> str:
    payload = {key: value for key, value in contract.items() if key not in {"interface_sha256", "root", "source_root"}}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
