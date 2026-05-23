"""User-level HLS generator preferences."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

USER_CONFIG_ENV = "HLS_GENERATOR_USER_CONFIG"
COMMENT_LANGUAGES = ("en", "zh")


def user_config_path() -> Path:
    override = os.environ.get(USER_CONFIG_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".hls-generator" / "config.json").resolve()


def load_user_config() -> dict[str, Any]:
    path = user_config_path()
    if not path.exists():
        return {"version": 1}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid HLS generator user config JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"HLS generator user config must be a JSON object: {path}")
    if int(data.get("version", 1)) != 1:
        raise ValueError(f"Unsupported HLS generator user config version in {path}: {data.get('version')!r}")
    data.setdefault("version", 1)
    return data


def save_user_config(config: dict[str, Any]) -> Path:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(config)
    payload["version"] = 1
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def get_comment_language(config: dict[str, Any] | None = None) -> str | None:
    value = str((config or load_user_config()).get("comment_language") or "").strip().lower()
    return value if value in COMMENT_LANGUAGES else None


def set_comment_language(language: str) -> Path:
    normalized = require_comment_language(language)
    config = load_user_config()
    config["comment_language"] = normalized
    config["comment_language_selected_at"] = _utc_now()
    return save_user_config(config)


def resolve_comment_language(value: str | None) -> str | None:
    normalized = str(value or "auto").strip().lower()
    if normalized == "auto":
        return get_comment_language()
    return require_comment_language(normalized)


def require_comment_language(language: str) -> str:
    normalized = str(language or "").strip().lower()
    if normalized not in COMMENT_LANGUAGES:
        raise ValueError(f"Comment language must be one of {', '.join(COMMENT_LANGUAGES)}.")
    return normalized


def comment_language_request() -> dict[str, Any]:
    return {
        "version": 1,
        "action": "ask_comment_language",
        "primary_source": "comment_language_auto_unconfigured",
        "question": "Choose the comment language for generated HLS C/C++ code before generation continues.",
        "options": [
            {"value": "en", "label": "English comments"},
            {"value": "zh", "label": "Chinese comments"},
        ],
        "user_config_path": str(user_config_path()),
        "persistence": "The chosen value is saved as comment_language in the user config.",
        "recommended_commands": [
            "python -m runtime.hls_generator user-config --set-comment-language en",
            "python -m runtime.hls_generator user-config --set-comment-language zh",
        ],
    }


def get_vitis_selection(server: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    selections = (config or load_user_config()).get("vitis_version_selection", {})
    if not isinstance(selections, dict):
        return None
    selected = selections.get(server)
    return selected if isinstance(selected, dict) else None


def set_vitis_selection(server: str, selection: dict[str, Any]) -> Path:
    if not server:
        raise ValueError("Vitis version selection requires a server id or name.")
    config = load_user_config()
    selections = config.setdefault("vitis_version_selection", {})
    if not isinstance(selections, dict):
        selections = {}
        config["vitis_version_selection"] = selections
    sanitized = {
        "version": str(selection.get("version") or ""),
        "settings_script": str(selection.get("settings_script") or ""),
        "expected_tool": str(selection.get("expected_tool") or ""),
        "target_part": str(selection.get("target_part") or ""),
        "expected_tool_path": str(selection.get("expected_tool_path") or ""),
        "env_setup_script": str(selection.get("env_setup_script") or ""),
        "tool_path": str(selection.get("tool_path") or ""),
        "vpp_path": str(selection.get("vpp_path") or ""),
        "xrt_tool_path": str(selection.get("xrt_tool_path") or ""),
        "xrt_setup_script": str(selection.get("xrt_setup_script") or ""),
        "xbmgmt_tool_path": str(selection.get("xbmgmt_tool_path") or ""),
        "selected_at": _utc_now(),
    }
    if not sanitized["version"] or not sanitized["settings_script"] or not sanitized["expected_tool"]:
        raise ValueError("Vitis selection requires version, settings_script, and expected_tool.")
    selections[server] = sanitized
    return save_user_config(config)


def get_board_platform_selection(server: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    selections = (config or load_user_config()).get("board_platform_selection", {})
    if not isinstance(selections, dict):
        return None
    selected = selections.get(server)
    return selected if isinstance(selected, dict) else None


def set_board_platform_selection(server: str, selection: dict[str, Any]) -> Path:
    if not server:
        raise ValueError("Board platform selection requires a server id or name.")
    platform_name = str(selection.get("platform_name") or "").strip()
    remote_platform_root = str(selection.get("remote_platform_root") or "").strip()
    remote_xpfm = str(selection.get("remote_xpfm") or "").strip()
    source = str(selection.get("source") or "").strip()
    if not platform_name:
        raise ValueError("Board platform selection requires platform_name.")
    config = load_user_config()
    selections = config.setdefault("board_platform_selection", {})
    if not isinstance(selections, dict):
        selections = {}
        config["board_platform_selection"] = selections
    selections[server] = {
        "platform_name": platform_name,
        "remote_platform_root": remote_platform_root,
        "remote_xpfm": remote_xpfm,
        "source": source,
        "selected_at": _utc_now(),
    }
    return save_user_config(config)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
