"""Cross-stage HLS interface verifier gate."""

from __future__ import annotations

import re
from typing import Any

from .planning import decompose_spec


def verify_stage(plan: dict[str, Any], from_contract: dict[str, Any], to_contract: dict[str, Any]) -> dict[str, Any]:
    normalized_plan = decompose_spec(plan)
    issues: list[dict[str, Any]] = []
    issues.extend(_contract_issues(from_contract, "from"))
    issues.extend(_contract_issues(to_contract, "to"))
    issues.extend(plan_contract_interface_issues(normalized_plan, to_contract))
    issues.extend(_check_cases_and_vectors(from_contract, to_contract))
    semantic_summary = _semantic_summary(from_contract, to_contract)
    issues.extend(_semantic_issues(semantic_summary))
    error_sources = _error_sources(issues)
    ready = not any(issue.get("severity") == "error" for issue in issues)
    return {
        "version": 1,
        "ready": ready,
        "from": _contract_summary(from_contract),
        "to": _contract_summary(to_contract),
        "issues": issues,
        "error_sources": error_sources,
        "recommended_action": _recommended_action(error_sources),
        "semantic_ready": semantic_summary.get("semantic_ready"),
        "mismatched_cases": semantic_summary.get("mismatched_cases", []),
        "checkpoint_drift": semantic_summary.get("checkpoint_drift", []),
        "failed_cases": semantic_summary.get("failed_cases", []),
        "localization_confidence": semantic_summary.get("localization_confidence"),
    }


def plan_contract_interface_issues(plan: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    if contract.get("target") != "hls":
        return []
    issues = _exact_named_interface_issues(
        plan.get("interfaces", {}).get("arguments", []),
        contract.get("arguments", []),
        fields=("type", "interface", "bundle"),
    )
    expected_top = plan.get("interfaces", {}).get("top_function") or plan.get("name")
    if expected_top and contract.get("top") != expected_top:
        issues.append({"severity": "error", "source": "current_module_issue", "message": f"HLS top mismatch: expected {expected_top!r}, observed {contract.get('top')!r}."})
    control_expected = str(plan.get("interfaces", {}).get("control") or "").strip().lower()
    control_observed = str(contract.get("control_mode") or "").strip().lower()
    if control_expected and not control_observed:
        issues.append({"severity": "error", "source": "current_module_issue", "message": "HLS control interface is missing from the downstream contract."})
    elif control_expected and control_observed and control_expected != control_observed:
        issues.append({"severity": "error", "source": "current_module_issue", "message": f"HLS control interface drifted from {control_expected!r} to {control_observed!r}."})
    return issues


def _contract_issues(contract: dict[str, Any], side: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for issue in contract.get("issues", []) or []:
        if isinstance(issue, dict):
            issues.append({"severity": issue.get("severity", "warning"), "source": issue.get("source", "current_module_issue"), "message": f"{side} contract issue: {issue.get('message', 'unspecified issue')}", "path": issue.get("path")})
    return issues


def _exact_named_interface_issues(expected_items: Any, observed_items: Any, *, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    expected_by_name = {str(item.get("name")): item for item in expected_items or [] if isinstance(item, dict) and item.get("name")}
    observed_by_name = {str(item.get("name")): item for item in observed_items or [] if isinstance(item, dict) and item.get("name")}
    issues: list[dict[str, Any]] = []
    missing = [name for name in expected_by_name if name not in observed_by_name]
    if missing:
        issues.append({"severity": "error", "source": "current_module_issue", "message": "HLS argument contract is missing declared entries: " + ", ".join(missing) + "."})
    unexpected = [name for name in observed_by_name if name not in expected_by_name]
    if unexpected:
        issues.append({"severity": "error", "source": "current_module_issue", "message": "HLS argument contract added undeclared entries: " + ", ".join(unexpected) + "."})
    for name, expected_item in expected_by_name.items():
        observed_item = observed_by_name.get(name)
        if not observed_item:
            continue
        for field in fields:
            expected_value = _normalized_interface_field(expected_item.get(field), field)
            observed_value = _normalized_interface_field(observed_item.get(field), field)
            if expected_value and not observed_value:
                issues.append({"severity": "error", "source": "current_module_issue", "message": f"HLS argument {name!r} is missing required field {field!r} in the downstream contract."})
            elif expected_value and observed_value and expected_value != observed_value:
                issues.append({"severity": "error", "source": "current_module_issue", "message": f"HLS argument {name!r} {field} drifted from {expected_value!r} to {observed_value!r}."})
    return issues


def _normalized_interface_field(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    if field == "type":
        canonical = re.sub(r"\s+", " ", str(value)).strip()
        return re.sub(r"\s*([*&])\s*", r"\1", canonical)
    if field in {"interface", "control_mode"}:
        return str(value).strip().lower()
    return str(value).strip()


def _check_cases_and_vectors(from_contract: dict[str, Any], to_contract: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    from_cases = [str(item) for item in from_contract.get("case_ids", []) or []]
    to_cases = [str(item) for item in to_contract.get("case_ids", []) or []]
    if from_cases:
        missing = [case for case in from_cases if case not in to_cases]
        if missing:
            issues.append({"severity": "error", "source": "testbench_issue", "message": "HLS testbench is missing reference vector case ids: " + ", ".join(missing)})
    from_hashes = [str(item) for item in from_contract.get("vector_hashes", []) or []]
    to_hashes = [str(item) for item in to_contract.get("vector_hashes", []) or []]
    if from_hashes and to_hashes and not set(from_hashes).intersection(to_hashes):
        issues.append({"severity": "error", "source": "testbench_issue", "message": "Reference vector hash drifted between stages."})
    if from_hashes and not to_hashes:
        issues.append({"severity": "error", "source": "testbench_issue", "message": "HLS testbench does not carry the reference vector hash."})
    return issues


def _semantic_summary(from_contract: dict[str, Any], to_contract: dict[str, Any]) -> dict[str, Any]:
    del from_contract
    metrics = to_contract.get("metrics", {}) if isinstance(to_contract.get("metrics"), dict) else {}
    semantic = metrics.get("semantic_execution", {}) if isinstance(metrics.get("semantic_execution"), dict) else {}
    if semantic:
        return {
            "semantic_ready": semantic.get("semantic_ready"),
            "mismatched_cases": semantic.get("mismatched_cases", []),
            "checkpoint_drift": semantic.get("checkpoint_drift", []),
            "failed_cases": semantic.get("failed_cases", []),
            "localization_confidence": semantic.get("localization_confidence"),
        }
    return {}


def _semantic_issues(semantic_summary: dict[str, Any]) -> list[dict[str, Any]]:
    if not semantic_summary:
        return []
    issues: list[dict[str, Any]] = []
    for item in semantic_summary.get("mismatched_cases", []) or []:
        issues.append({"severity": "error", "source": "current_module_issue", "message": "Semantic output drift was detected across stages.", "case_id": item.get("case_id") if isinstance(item, dict) else None})
    for case_id in semantic_summary.get("failed_cases", []) or []:
        issues.append({"severity": "error", "source": "current_module_issue", "message": "Semantic transcript reported FAIL for a reference case.", "case_id": case_id})
    return issues


def _error_sources(issues: list[dict[str, Any]]) -> list[str]:
    sources: list[str] = []
    for issue in issues:
        source = str(issue.get("source") or "current_module_issue")
        if source not in sources:
            sources.append(source)
    return sources


def _recommended_action(error_sources: list[str]) -> str:
    for source, action in (
        ("spec_issue", "revise_plan"),
        ("dependency_issue", "fix_dependency"),
        ("testbench_issue", "fix_testbench"),
        ("current_module_issue", "regenerate_current"),
        ("insufficient_debug", "augment_tests"),
        ("toolchain_issue", "fix_toolchain"),
        ("needs_human_intervention", "ask_human"),
    ):
        if source in error_sources:
            return action
    return "regenerate_current"


def _contract_summary(contract: dict[str, Any]) -> dict[str, Any]:
    return {"target": contract.get("target"), "top": contract.get("top"), "interface_sha256": contract.get("interface_sha256"), "case_ids": contract.get("case_ids", []), "vector_hashes": contract.get("vector_hashes", [])}
