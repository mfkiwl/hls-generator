"""Vitis HLS .cfg parsing and normalization helpers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .vitis_rules import require_allowed_config_option, require_allowed_config_section, require_allowed_directive

KNOWN_SECTIONS = {"hls", "files", "compile", "interface", "rtl", "dataflow", "schedule", "csim", "cosim", "export", "directive"}


def parse_hls_cfg_entries(cfg_text: str) -> dict[str, Any]:
    """Parse both existing syn.* configs and UG-style sectioned HLS configs."""
    entries: dict[str, Any] = {
        "syn.files": [],
        "tb.files": [],
        "files": {},
        "compile": {},
        "interface": {},
        "rtl": {},
        "dataflow": {},
        "schedule": {},
        "csim": {},
        "cosim": {},
        "export": {},
        "directives": [],
        "parse_errors": [],
        "raw_sections": {},
    }
    section = "hls"
    for raw in cfg_text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        section_match = re.match(r"^\[([A-Za-z0-9_.-]+)\]$", line)
        if section_match:
            section = section_match.group(1).strip().lower()
            entries["raw_sections"].setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, value = [item.strip() for item in line.split("=", 1)]
        if not key:
            continue
        _store_entry(entries, section, key, value)
    return entries


def clock_period_ns(value: Any) -> float | None:
    if value in (None, ""):
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(?:ns)?\s*$", str(value), flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def cfg_relative_path_issue(path: str) -> str | None:
    normalized = str(path).replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(str(path))
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return f"HLS cfg file path must be relative and stay inside generated artifacts: {path}"
    if any(part in {"", ".", ".."} for part in posix.parts):
        return f"HLS cfg file path must not contain empty, current, or parent path segments: {path}"
    return None


def _store_entry(entries: dict[str, Any], section: str, key: str, value: str) -> None:
    lower_key = key.lower()
    entries["raw_sections"].setdefault(section, {}).setdefault(lower_key, []).append(value)
    if section not in KNOWN_SECTIONS:
        entries["parse_errors"].append(f"Unsupported Vitis HLS cfg section {section!r}.")
        return
    if lower_key == "syn.file" or (section == "files" and lower_key in {"src", "source"}):
        _append_unique(entries["syn.files"], value)
        entries.setdefault("syn.file", value)
        return
    if lower_key == "tb.file" or (section == "files" and lower_key in {"tb", "testbench"}):
        _append_unique(entries["tb.files"], value)
        entries.setdefault("tb.file", value)
        return
    if section == "files" and lower_key in {"cflags", "csimflags"}:
        entries["files"][lower_key] = value
        return
    if lower_key == "syn.top" or (section == "hls" and lower_key == "top"):
        entries["syn.top"] = value
        return
    if lower_key in {"part", "clock", "flow_target", "clock_uncertainty"}:
        entries[lower_key] = value
        return
    if section in {"compile", "interface", "rtl", "dataflow", "schedule"}:
        try:
            normalized_section = require_allowed_config_section(section)
            normalized_key = require_allowed_config_option(normalized_section, lower_key)
        except ValueError as exc:
            entries["parse_errors"].append(str(exc))
            return
        entries[normalized_section][normalized_key] = value
        return
    if section in {"csim", "cosim", "export"}:
        try:
            normalized_key = require_allowed_config_option(section, lower_key)
        except ValueError as exc:
            entries["parse_errors"].append(str(exc))
            return
        entries[section][normalized_key] = value
        return
    if section == "directive":
        try:
            entries["directives"].append(_parse_directive(lower_key, value))
        except ValueError as exc:
            entries["parse_errors"].append(str(exc))


def _parse_directive(name: str, value: str) -> dict[str, Any]:
    normalized = require_allowed_directive(name)
    parts = value.split()
    location = parts[0] if parts else ""
    args = parts[1:] if len(parts) > 1 else []
    return {"name": normalized, "location": location, "args": args, "raw": value}


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _strip_comment(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#") or stripped.startswith(";"):
        return ""
    return line.split("#", 1)[0].split(";", 1)[0]
