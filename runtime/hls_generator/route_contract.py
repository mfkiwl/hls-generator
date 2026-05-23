"""Helpers for parsing AGENTS remote-route contracts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


REMOTE_ROUTE_NAME = "remote-hls-validation"


def repo_root_from_skill_root(skill_root: Path) -> Path:
    return skill_root.parents[1]


def root_agents_path(skill_root: Path) -> Path:
    return repo_root_from_skill_root(skill_root) / "AGENTS.md"


def parse_remote_route_contract(agents_text: str, *, route_name: str = REMOTE_ROUTE_NAME) -> dict[str, Any]:
    route_pattern = re.compile(
        rf"Task route `{re.escape(route_name)}`:\s*primary `([^`]+)`;\s*fallbacks:\s*([^\n]+)\.",
        re.IGNORECASE,
    )
    route_match = route_pattern.search(agents_text)
    if route_match is None:
        raise ValueError(f"Could not find route definition for {route_name!r}.")
    primary = route_match.group(1).strip()
    fallbacks_text = route_match.group(2).strip()
    fallbacks = [] if fallbacks_text.lower() == "none" else [item.strip(" `") for item in fallbacks_text.split(",") if item.strip()]

    server_pattern = re.compile(r"Registered server `([^`]+)`: ([^\n]+)")
    registered_servers = [
        {"id": match.group(1).strip(), "description": match.group(2).strip()}
        for match in server_pattern.finditer(agents_text)
    ]
    return {
        "route_name": route_name,
        "primary": primary,
        "fallbacks": fallbacks,
        "registered_servers": registered_servers,
    }


def load_remote_route_contract(skill_root: Path, *, route_name: str = REMOTE_ROUTE_NAME) -> dict[str, Any]:
    agents_path = root_agents_path(skill_root)
    return {
        "agents_path": str(agents_path),
        **parse_remote_route_contract(agents_path.read_text(encoding="utf-8"), route_name=route_name),
    }


def validate_remote_route_target(
    contract: dict[str, Any],
    *,
    server: str | None = None,
    build_server: str | None = None,
    validate_server: str | None = None,
) -> list[str]:
    primary = str(contract.get("primary") or "").strip()
    if not primary:
        return ["route contract primary server is empty"]
    errors: list[str] = []
    if server:
        if server != primary:
            errors.append(f"server must match AGENTS route primary {primary}")
        return errors
    if build_server or validate_server:
        if build_server != primary:
            errors.append(f"build_server must match AGENTS route primary {primary}")
        if validate_server != primary:
            errors.append(f"validate_server must match AGENTS route primary {primary}")
        return errors
    errors.append("remote route validation requires server or split-topology targets")
    return errors
