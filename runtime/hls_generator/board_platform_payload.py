"""Local board platform payload validation and packaging helpers."""

from __future__ import annotations

import json
import os
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PASS_STATUS = "passed"
FAILED_STATUS = "failed"
U55C_PLATFORM_NAME = "xilinx_u55c_gen3x16_xdma_3_202210_1"
EXTRA_REQUIRED_RELATIVE_PATHS = ("license/LICENSE",)


def default_local_u55c_payload_root() -> Path:
    override = os.environ.get("ERIE_HLS_U55C_PLATFORM_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    source_path = Path(__file__).resolve()
    for parent in source_path.parents:
        if parent.name == "Skills":
            return (parent / "VitisDeveloper" / "skills" / ".dependencies" / "board" / "xilinx" / "u55c").resolve()
    return (source_path.parents[2] / ".dependencies" / "board" / "xilinx" / "u55c").resolve()


def validate_local_board_platform_payload(root: str | Path, *, expected_platform_name: str = U55C_PLATFORM_NAME) -> dict[str, Any]:
    payload_root = Path(root).expanduser().resolve()
    errors: list[str] = []
    dependency_path = payload_root / ".dependency_source.json"
    dependency_source: dict[str, Any] = {}
    if not payload_root.exists():
        errors.append(f"missing root: {payload_root}")
    if dependency_path.exists():
        try:
            loaded = json.loads(dependency_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                dependency_source = loaded
            else:
                errors.append(".dependency_source.json root must be an object")
        except json.JSONDecodeError as exc:
            errors.append(f"invalid .dependency_source.json: {exc}")
    else:
        errors.append("missing .dependency_source.json")
    platform_name = str(dependency_source.get("platform_name") or expected_platform_name).strip()
    if platform_name != expected_platform_name:
        errors.append(f"platform_name mismatch: expected {expected_platform_name}, got {platform_name or '<empty>'}")
    xpfm_path = payload_root / f"{platform_name}.xpfm"
    if not xpfm_path.exists():
        errors.append(f"missing xpfm: {xpfm_path.name}")
        referenced_paths: list[str] = []
    else:
        referenced_paths = _xpfm_referenced_relative_paths(xpfm_path, errors)
    required_paths = sorted({f"{Path(item).as_posix()}" for item in [*referenced_paths, *EXTRA_REQUIRED_RELATIVE_PATHS]})
    missing_paths = [rel_path for rel_path in required_paths if not (payload_root / rel_path).exists()]
    errors.extend(f"missing required payload file: {rel_path}" for rel_path in missing_paths)
    return {
        "status": PASS_STATUS if not errors else FAILED_STATUS,
        "root": str(payload_root),
        "platform_name": platform_name,
        "expected_platform_name": expected_platform_name,
        "dependency_source_path": str(dependency_path),
        "dependency_source": dependency_source,
        "xpfm": str(xpfm_path),
        "required_relative_paths": required_paths,
        "missing_relative_paths": missing_paths,
        "total_bytes": _directory_size(payload_root) if payload_root.exists() else 0,
        "errors": errors,
    }


def create_platform_archive(payload: dict[str, Any], output_dir: str | Path) -> Path:
    if payload.get("status") != PASS_STATUS:
        raise ValueError(f"Cannot archive invalid platform payload: {payload.get('errors')}")
    root = Path(str(payload["root"])).resolve()
    platform_name = str(payload["platform_name"])
    archive_dir = Path(output_dir).resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{platform_name}.tar.gz"
    if archive_path.exists():
        archive_path.unlink()
    with tarfile.open(archive_path, "w:gz", compresslevel=1) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            archive.add(path, arcname=(Path(platform_name) / path.relative_to(root)).as_posix())
    return archive_path


def prepare_local_u55c_platform_archive(output_dir: str | Path, *, local_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(local_root).expanduser().resolve() if local_root else default_local_u55c_payload_root()
    payload = validate_local_board_platform_payload(root, expected_platform_name=U55C_PLATFORM_NAME)
    if payload["status"] != PASS_STATUS:
        return {**payload, "archive_path": ""}
    archive_path = create_platform_archive(payload, output_dir)
    return {**payload, "archive_path": str(archive_path)}


def _xpfm_referenced_relative_paths(xpfm_path: Path, errors: list[str]) -> list[str]:
    try:
        tree = ET.parse(xpfm_path)
    except ET.ParseError as exc:
        errors.append(f"invalid xpfm xml: {exc}")
        return []
    references: list[str] = []
    for element in tree.iter():
        path_value = _namespaced_attr(element.attrib, "path")
        name_value = _namespaced_attr(element.attrib, "name")
        if not path_value or not name_value:
            continue
        if not name_value.lower().endswith((".xsa", ".spfm")):
            continue
        references.append((Path(path_value) / name_value).as_posix())
    if not references:
        errors.append("xpfm did not reference any XSA/SPFM payload files")
    return references


def _namespaced_attr(attributes: dict[str, str], local_name: str) -> str:
    for key, value in attributes.items():
        if key == local_name or key.endswith("}" + local_name):
            return str(value).strip()
    return ""


def _directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
