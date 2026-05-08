"""Vitis HLS report parsing helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def collect_hls_report_metrics(root: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for path in sorted(root.glob("**/*")):
        if not path.is_file() or path.suffix.lower() not in {".rpt", ".log"}:
            continue
        lowered = path.name.lower()
        if "csynth" in lowered or "synth" in lowered:
            _merge(metrics, {"csynth": parse_hls_report(path.read_text(encoding="utf-8", errors="ignore"))})
        elif "cosim" in lowered:
            _merge(metrics, {"cosim": parse_hls_report(path.read_text(encoding="utf-8", errors="ignore"))})
    return _drop_empty(metrics)


def parse_hls_report(text: str) -> dict[str, Any]:
    return _drop_empty(
        {
            "latency": _parse_latency(text),
            "interval": _parse_interval(text),
            "resources": _parse_resources(text),
            "timing": _parse_timing(text),
            "cosim": _parse_cosim(text),
        }
    )


def _parse_latency(text: str) -> dict[str, int]:
    latency: dict[str, int] = {}
    patterns = [
        (r"Latency\s*\(cycles\)\s*[:=]\s*min\s*=?\s*(\d+)\s*max\s*=?\s*(\d+)", ("min", "max")),
        (r"\bLatency\b[^\n|]*\|\s*(\d+)\s*\|\s*(\d+)", ("min", "max")),
        (r"\bLatency\s*[:=]\s*(\d+)", ("value",)),
    ]
    for pattern, keys in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            for key, value in zip(keys, match.groups()):
                latency[key] = int(value)
            return latency
    return latency


def _parse_interval(text: str) -> dict[str, int]:
    interval: dict[str, int] = {}
    patterns = [
        (r"(?:Interval|II)\s*[:=]\s*min\s*=?\s*(\d+)\s*max\s*=?\s*(\d+)", ("min", "max")),
        (r"\b(?:Interval|II)\b[^\n|]*\|\s*(\d+)\s*\|\s*(\d+)", ("min", "max")),
        (r"\bII\s*=?\s*(\d+)", ("value",)),
    ]
    for pattern, keys in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            for key, value in zip(keys, match.groups()):
                interval[key] = int(value)
            return interval
    return interval


def _parse_resources(text: str) -> dict[str, int]:
    resources: dict[str, int] = {}
    aliases = {
        "bram": r"BRAM(?:_18K)?",
        "dsp": r"DSP(?:48E)?",
        "ff": r"FF",
        "lut": r"LUT",
        "uram": r"URAM",
    }
    for key, pattern in aliases.items():
        match = re.search(rf"\b{pattern}\b\s*[:=|]\s*(\d+)", text, flags=re.IGNORECASE)
        if match:
            resources[key] = int(match.group(1))
    table_match = re.search(
        r"\|\s*BRAM_?18K\s*\|\s*DSP(?:48E)?\s*\|\s*FF\s*\|\s*LUT\s*\|.*?\n\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if table_match:
        resources.update(
            {
                "bram": int(table_match.group(1)),
                "dsp": int(table_match.group(2)),
                "ff": int(table_match.group(3)),
                "lut": int(table_match.group(4)),
            }
        )
    return resources


def _parse_timing(text: str) -> dict[str, float]:
    timing: dict[str, float] = {}
    patterns = {
        "wns": r"\bWNS\b\s*[:=|]\s*(-?\d+(?:\.\d+)?)",
        "tns": r"\bTNS\b\s*[:=|]\s*(-?\d+(?:\.\d+)?)",
        "estimated_clock_period_ns": r"(?:Estimated\s+)?Clock\s+Period(?:\s*\(ns\))?\s*[:=|]\s*(\d+(?:\.\d+)?)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            timing[key] = float(match.group(1))
    return timing


def _parse_cosim(text: str) -> dict[str, Any]:
    lowered = text.lower()
    if "cosim" not in lowered and "co-sim" not in lowered:
        return {}
    if "pass" in lowered:
        return {"status": "pass"}
    if "fail" in lowered:
        return {"status": "fail"}
    return {"status": "unknown"}


def _merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if not value:
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key].update(value)
        else:
            target[key] = value


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in ({}, [], None)}

