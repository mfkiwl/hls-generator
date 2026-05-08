"""Vitis HLS profile checks and repair-prompt generation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_FORBIDDEN_FEATURES = (
    "std::vector",
    "new",
    "malloc",
    "free",
    "throw",
    "catch",
    "std::map",
    "std::unordered_map",
    "std::list",
    "std::deque",
    "std::string",
)


def validate_hls_profile(profile: dict[str, Any], root: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    if not profile:
        return []
    source_text = _source_text(root)
    cfg_text = _cfg_text(root)
    issues: list[dict[str, Any]] = []
    issues.extend(_check_forbidden_features(profile, source_text))
    issues.extend(_check_interface_modes(profile, source_text))
    issues.extend(_check_required_pragmas(profile, source_text, spec))
    issues.extend(_check_static_arrays(profile, source_text))
    issues.extend(_check_cfg(profile, cfg_text))
    return issues


def build_hls_optimizer_prompt(validation_json: dict[str, Any], profile: dict[str, Any]) -> str:
    profile_json = json.dumps(profile, indent=2, ensure_ascii=False, sort_keys=True)
    issues = _profile_related_issues(validation_json)
    issues_json = json.dumps(issues, indent=2, ensure_ascii=False)
    return f"""# HLS profile repair prompt

You are repairing Vitis HLS C++ artifacts to satisfy the project HLS profile. Do not change the algorithm unless an issue explicitly requires it.

## HLS profile

```json
{profile_json}
```

## Profile-related validation issues

```json
{issues_json}
```

## Repair constraints

- Align `hls_config.cfg` with `syn.top` and every required `syn.file`.
- Emit `#pragma HLS INTERFACE` pragmas for all external arguments using only allowed interface modes.
- Remove forbidden C++ features from kernel code: dynamic memory, exceptions, recursion, and unsupported STL containers.
- Replace dynamic arrays with static bounded arrays or stream/buffer structures that Vitis HLS can synthesize.
- Preserve the manifest/code-fence output contract and regenerate only the affected HLS files.
"""


def _check_forbidden_features(profile: dict[str, Any], source_text: str) -> list[dict[str, Any]]:
    features = profile.get("forbidden_features") or DEFAULT_FORBIDDEN_FEATURES
    issues: list[dict[str, Any]] = []
    patterns = {
        "std::vector": r"\bstd::vector\b",
        "new": r"\bnew\s+[A-Za-z_]",
        "malloc": r"\bmalloc\s*\(",
        "free": r"\bfree\s*\(",
        "throw": r"\bthrow\b",
        "catch": r"\bcatch\s*\(",
        "std::map": r"\bstd::map\b",
        "std::unordered_map": r"\bstd::unordered_map\b",
        "std::list": r"\bstd::list\b",
        "std::deque": r"\bstd::deque\b",
        "std::string": r"\bstd::string\b",
    }
    for feature in features:
        pattern = patterns.get(str(feature), re.escape(str(feature)))
        if re.search(pattern, source_text):
            issues.append(_issue("error", f"HLS profile violation: forbidden feature {feature!r} was found."))
    return issues


def _check_interface_modes(profile: dict[str, Any], source_text: str) -> list[dict[str, Any]]:
    allowed = profile.get("allowed_interface_modes") or profile.get("interface_modes") or []
    if not allowed:
        return []
    allowed_set = {str(item) for item in allowed}
    issues: list[dict[str, Any]] = []
    for line in source_text.splitlines():
        if "#pragma HLS INTERFACE" not in line:
            continue
        mode = _pragma_mode(line)
        if mode and mode not in allowed_set:
            issues.append(_issue("error", f"HLS profile violation: interface mode {mode!r} is not allowed."))
    return issues


def _check_required_pragmas(profile: dict[str, Any], source_text: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    if profile.get("require_interface_pragmas", True) is False:
        return []
    issues: list[dict[str, Any]] = []
    for argument in spec.get("interfaces", {}).get("arguments", []) or []:
        if not isinstance(argument, dict) or not argument.get("name"):
            continue
        name = str(argument["name"])
        if not re.search(rf"#pragma\s+HLS\s+INTERFACE[^\n]*\bport\s*=\s*{re.escape(name)}\b", source_text):
            issues.append(_issue("error", f"HLS profile violation: missing interface pragma for argument {name!r}."))
    return issues


def _check_static_arrays(profile: dict[str, Any], source_text: str) -> list[dict[str, Any]]:
    if profile.get("require_static_arrays", True) is False and profile.get("static_memory_rule") != "static_bound":
        return []
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_:<>]*\s+[A-Za-z_][A-Za-z0-9_]*\s*\[[A-Za-z_][A-Za-z0-9_]*\]\s*;", source_text):
        return [_issue("error", "HLS profile violation: dynamic stack array was found; use static bounds.")]
    return []


def _check_cfg(profile: dict[str, Any], cfg_text: str) -> list[dict[str, Any]]:
    if profile.get("require_syn_file", True) is False:
        return []
    if not re.search(r"(?m)^\s*syn\.file\s*=", cfg_text):
        return [_issue("error", "HLS profile violation: cfg is missing syn.file.")]
    return []


def _pragma_mode(line: str) -> str:
    mode = _pragma_value(line, "mode")
    if mode:
        return mode
    match = re.search(r"#pragma\s+HLS\s+INTERFACE\s+([A-Za-z0-9_]+)", line)
    return match.group(1) if match else ""


def _pragma_value(line: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*([A-Za-z0-9_]+)", line)
    return match.group(1) if match else ""


def _profile_related_issues(validation_json: dict[str, Any]) -> list[dict[str, Any]]:
    issues = validation_json.get("issues", []) if isinstance(validation_json, dict) else []
    selected: list[dict[str, Any]] = []
    for issue in issues or []:
        text = json.dumps(issue, ensure_ascii=False).lower() if isinstance(issue, dict) else str(issue).lower()
        if "hls profile" in text or "pragma" in text or "syn.file" in text or "std::vector" in text or "dynamic" in text:
            selected.append(issue if isinstance(issue, dict) else {"message": str(issue)})
    return selected


def _source_text(root: Path) -> str:
    texts: list[str] = []
    for pattern in ("**/*.cpp", "**/*.cc", "**/*.cxx", "**/*.h", "**/*.hpp"):
        for path in sorted(root.glob(pattern)):
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(texts)


def _cfg_text(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in sorted(root.glob("**/*.cfg")))


def _issue(severity: str, message: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "message": message,
        "stage": "static",
        "source": "current_module_issue",
    }

