"""Structured HLS generation spec handling."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

TARGETS = ("hls",)
SPEC_FIELDS = (
    "name",
    "target",
    "design_requirements",
    "streamability",
    "interface_family",
    "interface_profile",
    "pipeline_required",
    "codegen_plan_required",
    "codegen_plan_path",
    "description",
    "interfaces",
    "behavior",
    "clock",
    "reset",
    "constraints",
    "outputs",
    "notes",
    "subfunctions",
    "workflow",
    "performance",
    "hls_profile",
)
SUBFUNCTION_FIELDS = (
    "name",
    "inputs",
    "outputs",
    "behavior",
    "constraints",
    "dependencies",
    "source_references",
    "test_intent",
)
INFO_DICTIONARY_FIELDS = ("behavior", "constraints", "test_intent")
HLS_SOURCE_SUFFIXES = {".cpp", ".cc", ".cxx", ".h", ".hpp"}
HLS_CONFIG_SUFFIXES = {".cfg"}
REJECTED_HARDWARE_LANGUAGES = {"verilog", "systemverilog", "sv", "rtl"}


class SpecError(ValueError):
    """Raised when a generation spec is invalid."""


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r"\W+", "_", name.strip()).strip("_")
    if not cleaned:
        return "hls_kernel"
    if cleaned[0].isdigit():
        cleaned = f"design_{cleaned}"
    return cleaned


def scaffold_spec(target: str = "hls", name: str | None = None) -> dict[str, Any]:
    _require_target(target)
    spec_name = sanitize_name(name or "hls_kernel")
    top = spec_name if spec_name.endswith("_kernel") else f"{spec_name}_kernel"
    return {
        "name": spec_name,
        "target": "hls",
        "design_requirements": {},
        "streamability": "unknown",
        "interface_family": None,
        "interface_profile": {},
        "pipeline_required": True,
        "codegen_plan_required": True,
        "codegen_plan_path": None,
        "description": "Implement a Vitis HLS C++ kernel.",
        "interfaces": {
            "top_function": top,
            "arguments": [
                {
                    "name": "input",
                    "type": "const ap_uint<32> *",
                    "direction": "input",
                    "interface": "m_axi",
                    "bundle": "gmem0",
                },
                {
                    "name": "output",
                    "type": "ap_uint<32> *",
                    "direction": "output",
                    "interface": "m_axi",
                    "bundle": "gmem1",
                },
                {
                    "name": "length",
                    "type": "int",
                    "direction": "input",
                    "interface": "s_axilite",
                },
            ],
            "control": "s_axilite",
        },
        "behavior": [
            "Describe kernel computation, memory access pattern, throughput goal, and edge cases here."
        ],
        "clock": {"period_ns": 10.0, "uncertainty_ns": 1.0},
        "reset": {"strategy": "tool_default"},
        "constraints": [
            "Use Vitis HLS compatible C++.",
            "Use fixed-width ap_int/ap_uint/ap_fixed types where appropriate.",
            "Add interface pragmas and pipeline/dataflow pragmas justified by the access pattern.",
            "Avoid dynamic memory, recursion, exceptions, RTTI, and unsupported standard library features.",
        ],
        "outputs": [
            {"path": f"src/{top}.h", "kind": "header", "language": "cpp"},
            {"path": f"src/{top}.cpp", "kind": "source", "language": "cpp"},
            {"path": f"tb/{top}_tb.cpp", "kind": "testbench", "language": "cpp"},
            {"path": "hls_config.cfg", "kind": "config", "language": "ini"},
        ],
        "notes": [],
        "subfunctions": [],
        "workflow": {},
        "performance": {},
        "hls_profile": {},
    }


def normalize_spec(raw: dict[str, Any], target: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError("Spec must be a JSON object.")
    requested_target = _require_target(str(target or raw.get("target") or "hls"))
    raw_target = raw.get("target")
    if raw_target and str(raw_target).lower() != requested_target:
        raise SpecError(f"Spec target {raw_target!r} does not match requested target {requested_target!r}.")
    _reject_legacy_target_fields(raw)

    name = sanitize_name(str(raw.get("name") or scaffold_spec(requested_target)["name"]))
    spec = scaffold_spec(requested_target, name=name)
    for key, value in raw.items():
        if key in SPEC_FIELDS:
            spec[key] = copy.deepcopy(value)

    spec["name"] = sanitize_name(str(spec["name"]))
    spec["target"] = "hls"
    spec["design_requirements"] = _normalize_design_requirements(spec.get("design_requirements"))
    spec["streamability"] = _normalize_streamability(spec.get("streamability"))
    spec["interface_family"] = _normalize_interface_family(spec.get("interface_family"))
    spec["interface_profile"] = _normalize_interface_profile(spec.get("interface_profile"))
    spec["pipeline_required"] = _normalize_bool(spec.get("pipeline_required"), "pipeline_required", default=True)
    spec["codegen_plan_required"] = _normalize_bool(spec.get("codegen_plan_required"), "codegen_plan_required", default=True)
    spec["codegen_plan_path"] = None if spec.get("codegen_plan_path") in (None, "") else str(spec.get("codegen_plan_path"))
    spec["subfunctions"] = [normalize_subfunction(item, index) for index, item in enumerate(spec.get("subfunctions", []))]
    _validate_shape(spec)
    return spec


def normalize_subfunction(subfunction: dict[str, Any], index: int = 0) -> dict[str, Any]:
    if not isinstance(subfunction, dict):
        raise SpecError("Each subfunction must be an object.")
    normalized = copy.deepcopy(subfunction)
    normalized["name"] = sanitize_name(str(normalized.get("name") or f"subfunction_{index + 1}"))
    for field in ("inputs", "outputs", "dependencies", "source_references"):
        normalized[field] = _as_list(normalized.get(field, []))
    for field in INFO_DICTIONARY_FIELDS:
        normalized[field] = normalize_info_items(normalized.get(field, []), field)
    return normalized


def normalize_info_items(value: Any, field: str) -> list[dict[str, Any]]:
    return [_normalize_info_item(item, field, index) for index, item in enumerate(_as_list(value))]


def read_spec(path: Path, target: str | None = None) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"Invalid JSON in {path}: {exc}") from exc
    return normalize_spec(raw, target=target)


def write_spec(path: Path, spec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _normalize_info_item(item: Any, field: str, index: int) -> dict[str, Any]:
    default_id = f"{field}_{index + 1}"
    if isinstance(item, dict):
        text_value = item.get("text", item.get("description", item.get("functionality", "")))
        if not text_value and len(item) == 1:
            text_value = next(iter(item.values()))
        return {
            "id": sanitize_name(str(item.get("id") or default_id)),
            "text": str(text_value),
            "evidence": _as_list(item.get("evidence", [])),
            "verification_cases": _as_list(item.get("verification_cases", [])),
        }
    return {"id": default_id, "text": str(item), "evidence": [], "verification_cases": []}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return copy.deepcopy(value)
    return [copy.deepcopy(value)]


def _require_target(target: str) -> str:
    normalized = target.lower()
    if normalized != "hls":
        raise SpecError("This skill is HLS-only; target must be `hls`.")
    return normalized


def _reject_legacy_target_fields(raw: dict[str, Any]) -> None:
    legacy = [key for key in ("rtl_dialect", "rtl_style_profile") if key in raw and raw.get(key) not in (None, "")]
    if legacy:
        raise SpecError("This skill is HLS-only; RTL dialect/style fields are not supported.")


def _normalize_design_requirements(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise SpecError("Spec design_requirements must be an object.")
    return copy.deepcopy(value)


def _normalize_streamability(value: Any) -> str:
    if value in (None, ""):
        return "unknown"
    normalized = str(value).lower()
    if normalized not in {"streamable", "non_streamable", "unknown"}:
        raise SpecError("Spec streamability must be `streamable`, `non_streamable`, or `unknown`.")
    return normalized


def _normalize_interface_family(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).lower()
    if normalized not in {"native", "axi_stream", "axi4", "custom"}:
        raise SpecError("Spec interface_family must be one of `native`, `axi_stream`, `axi4`, or `custom`.")
    return normalized


def _normalize_interface_profile(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise SpecError("Spec interface_profile must be an object.")
    return copy.deepcopy(value)


def _normalize_bool(value: Any, field: str, *, default: bool) -> bool:
    if value in (None, ""):
        return default
    if not isinstance(value, bool):
        raise SpecError(f"Spec {field} must be a boolean.")
    return value


def _validate_shape(spec: dict[str, Any]) -> None:
    missing = [field for field in SPEC_FIELDS if field not in spec]
    if missing:
        raise SpecError(f"Spec is missing required fields: {', '.join(missing)}.")
    if not spec["description"]:
        raise SpecError("Spec description must not be empty.")
    if not isinstance(spec["interfaces"], dict):
        raise SpecError("Spec interfaces must be an object.")
    if not isinstance(spec["behavior"], list):
        raise SpecError("Spec behavior must be a list.")
    if not isinstance(spec["constraints"], list):
        raise SpecError("Spec constraints must be a list.")
    if not isinstance(spec["outputs"], list) or not spec["outputs"]:
        raise SpecError("Spec outputs must be a non-empty list.")
    for key in ("notes", "subfunctions"):
        if not isinstance(spec.get(key, []), list):
            raise SpecError(f"Spec {key} must be a list.")
    for key in ("workflow", "performance", "hls_profile", "design_requirements", "interface_profile"):
        if not isinstance(spec.get(key, {}), dict):
            raise SpecError(f"Spec {key} must be an object.")
    for output in spec["outputs"]:
        _validate_output(output)
    for subfunction in spec.get("subfunctions", []):
        _validate_subfunction(subfunction)


def _validate_output(output: Any) -> None:
    if not isinstance(output, dict) or not output.get("path"):
        raise SpecError("Each output must be an object with a path.")
    path = str(output["path"])
    suffix = Path(path).suffix.lower()
    language = str(output.get("language") or "").lower()
    if suffix in {".v", ".sv"} or language in REJECTED_HARDWARE_LANGUAGES:
        raise SpecError("This skill is HLS-only; Verilog/SystemVerilog outputs are not allowed.")
    if suffix not in HLS_SOURCE_SUFFIXES | HLS_CONFIG_SUFFIXES:
        raise SpecError(f"HLS output path {path!r} must be C/C++ source/header or .cfg.")
    if output.get("kind") == "config" and suffix not in HLS_CONFIG_SUFFIXES:
        raise SpecError("HLS config outputs must use a .cfg suffix.")


def _validate_subfunction(subfunction: Any) -> None:
    if not isinstance(subfunction, dict):
        raise SpecError("Each subfunction must be an object.")
    missing = [field for field in SUBFUNCTION_FIELDS if field not in subfunction]
    if missing:
        raise SpecError(f"Subfunction is missing required fields: {', '.join(missing)}.")
    for field in SUBFUNCTION_FIELDS[1:]:
        if not isinstance(subfunction[field], list):
            raise SpecError(f"Subfunction field {field} must be a list.")
    for field in INFO_DICTIONARY_FIELDS:
        for item in subfunction[field]:
            if not isinstance(item, dict):
                raise SpecError(f"Subfunction field {field} entries must be objects.")
