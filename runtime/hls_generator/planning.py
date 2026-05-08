"""HLS spec decomposition helpers."""

from __future__ import annotations

import copy
from typing import Any

from .spec import normalize_info_items, normalize_spec


def decompose_spec(
    spec: dict[str, Any],
    target: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a normalized HLS implementation plan."""

    normalized = normalize_spec(spec, target=target)
    plan = copy.deepcopy(normalized)
    if not plan.get("subfunctions"):
        inputs, outputs = _hls_io(plan)
        plan["subfunctions"] = [
            {
                "name": plan["name"],
                "inputs": inputs,
                "outputs": outputs,
                "behavior": normalize_info_items(plan.get("behavior", []), "behavior"),
                "constraints": normalize_info_items(plan.get("constraints", []), "constraints"),
                "dependencies": [],
                "source_references": _source_refs(evidence),
                "test_intent": normalize_info_items(
                    [
                        "Cover normal cases, boundary cases, and every behavior item with Python vectors and an HLS C++ testbench."
                    ],
                    "test_intent",
                ),
            }
        ]
    plan["workflow"] = {
        "stages": ["requirements", "codegen_plan", "tests", "python", "hls"],
        **(plan.get("workflow") or {}),
    }
    return plan


def _hls_io(spec: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    inputs: list[Any] = []
    outputs: list[Any] = []
    for argument in spec.get("interfaces", {}).get("arguments", []):
        if not isinstance(argument, dict):
            continue
        direction = str(argument.get("direction") or "input").lower()
        if direction == "output":
            outputs.append(argument)
        elif direction == "input":
            inputs.append(argument)
        else:
            inputs.append(argument)
            outputs.append(argument)
    return inputs, outputs


def _source_refs(evidence: dict[str, Any] | None) -> list[Any]:
    if not evidence:
        return []
    refs = []
    for item in evidence.get("items", [])[:8]:
        if isinstance(item, dict):
            refs.append(
                {
                    "source_id": item.get("source_id"),
                    "location": item.get("location"),
                    "kind": item.get("kind", "text"),
                }
            )
    return refs
