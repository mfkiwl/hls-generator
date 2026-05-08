"""Render run-local Vitis HLS Tcl from normalized cfg entries."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .hls_cfg import cfg_relative_path_issue

_READINESS_ORDER = {"static": 0, "compile": 1, "execute": 2, "implement": 3, "cosim": 4}


def readiness_at_least(readiness: str, stage: str) -> bool:
    return _READINESS_ORDER[readiness] >= _READINESS_ORDER[stage]


def render_vitis_hls_tcl(
    spec: dict[str, Any],
    root: Path,
    entries: dict[str, Any],
    readiness: str,
    tcl_config: dict[str, str],
) -> tuple[str, Path]:
    top = str(entries.get("syn.top") or spec.get("interfaces", {}).get("top_function") or spec.get("name") or "kernel")
    project = Path(tempfile.mkdtemp(prefix=tcl_config["project_dir_prefix"], dir=root))
    flow = _flow_option(entries)
    lines = [
        f"open_project -reset{flow} {_tcl_quote(project.name)}",
        f"set_top {_tcl_quote(top)}",
    ]
    file_options = entries.get("files", {})
    for source in entries.get("syn.files", []):
        lines.append(_add_files_line(root, str(source), cflags=file_options.get("cflags")))
    for tb in entries.get("tb.files", []):
        lines.append(_add_files_line(root, str(tb), tb=True, cflags=file_options.get("cflags"), csimflags=file_options.get("csimflags")))
    lines.append(f"open_solution -reset{flow} {_tcl_quote(tcl_config['solution_name'])}")
    if entries.get("part"):
        lines.append(f"set_part {_tcl_quote(str(entries['part']))}")
    if entries.get("clock"):
        lines.append(f"create_clock -period {entries['clock']}")
    if entries.get("clock_uncertainty"):
        lines.append(f"set_clock_uncertainty {entries['clock_uncertainty']}")
    lines.extend(_config_lines(entries))
    csim_config_line = _csim_config_line(entries.get("csim", {}))
    if csim_config_line:
        lines.append(csim_config_line)
    cosim_config_line = _cosim_config_line(entries.get("cosim", {}))
    if cosim_config_line:
        lines.append(cosim_config_line)
    lines.extend(_directive_lines(entries))
    if readiness_at_least(readiness, "compile"):
        lines.append(_csim_line(entries.get("csim", {})))
    if readiness_at_least(readiness, "implement"):
        lines.append("csynth_design")
        lines.extend(_report_lines(tcl_config["solution_name"]))
    if readiness_at_least(readiness, "cosim"):
        lines.append(_cosim_line(entries.get("cosim", {})))
        export_config_line = _export_config_line(entries.get("export", {}))
        if export_config_line:
            lines.append(export_config_line)
        export_line = _export_line(entries.get("export", {}))
        if export_line:
            lines.append(export_line)
    lines.append("exit")
    return "\n".join(lines) + "\n", project


def _flow_option(entries: dict[str, Any]) -> str:
    flow = str(entries.get("flow_target") or "").strip().lower()
    if not flow:
        return ""
    if flow not in {"vivado", "vitis"}:
        raise ValueError(f"Unsupported Vitis HLS flow_target {flow!r}.")
    return f" -flow_target {flow}"


def _config_lines(entries: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    flag_only = {("compile", "enable_auto_rewind"), ("compile", "unsafe_math_optimizations")}
    for section in ("compile", "schedule", "interface", "rtl", "dataflow"):
        values = entries.get(section, {})
        if not values:
            continue
        args: list[str] = []
        for key, value in values.items():
            flag = "-" + str(key).replace("_", "_")
            if str(value).lower() == "true" and (section, str(key)) in flag_only:
                args.append(flag)
            elif str(value).lower() == "false":
                args.extend([flag, "false"])
            else:
                args.extend([flag, str(value)])
        lines.append(f"config_{section} {' '.join(args)}")
    return lines


def _add_files_line(root: Path, path: str, *, tb: bool = False, cflags: str | None = None, csimflags: str | None = None) -> str:
    args = ["add_files"]
    if tb:
        args.append("-tb")
    if cflags:
        args.extend(["-cflags", _tcl_quote(str(cflags))])
    if csimflags:
        args.extend(["-csimflags", _tcl_quote(str(csimflags))])
    args.append(_tcl_path_expr(root, path))
    return " ".join(args)


def _tcl_path_expr(root: Path, path: str) -> str:
    issue = cfg_relative_path_issue(path)
    if issue:
        raise ValueError(issue)
    resolved = (root / path).resolve().as_posix()
    if any(marker in path for marker in ("*", "?", "[")):
        return f"[glob -nocomplain {_tcl_quote(resolved)}]"
    return _tcl_quote(resolved)


def _directive_lines(entries: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for directive in entries.get("directives", []):
        name = str(directive["name"])
        location = str(directive.get("location") or "")
        args = [str(item) for item in directive.get("args", [])]
        suffix = (" " + " ".join(args)) if args else ""
        lines.append(f"set_directive_{name}{suffix} {_tcl_quote(location)}")
    return lines


def _csim_line(values: dict[str, str]) -> str:
    args: list[str] = []
    if str(values.get("clean", "")).lower() == "true":
        args.append("-clean")
    if str(values.get("compile_only", "")).lower() == "true":
        args.append("-compile_only")
    if str(values.get("O", values.get("o", ""))).lower() == "true":
        args.append("-O")
    if values.get("argv"):
        args.extend(["-argv", _tcl_quote(str(values["argv"]))])
    return "csim_design" + ((" " + " ".join(args)) if args else "")


def _csim_config_line(values: dict[str, str]) -> str:
    args: list[str] = []
    if values.get("ldflags"):
        args.extend(["-ldflags", _tcl_quote(str(values["ldflags"]))])
    return "config_csim " + " ".join(args) if args else ""


def _cosim_config_line(values: dict[str, str]) -> str:
    args: list[str] = []
    if values.get("enable_tasks_with_m_axi"):
        args.extend(["-enable_tasks_with_m_axi", str(values["enable_tasks_with_m_axi"])])
    return "config_cosim " + " ".join(args) if args else ""


def _cosim_line(values: dict[str, str]) -> str:
    args: list[str] = []
    for key in ("rtl", "tool", "trace_level"):
        if values.get(key):
            args.extend([f"-{key}", str(values[key])])
    for key in ("wave_debug", "random_stall"):
        if str(values.get(key, "")).lower() == "true":
            args.append(f"-{key}")
    return "cosim_design" + ((" " + " ".join(args)) if args else "")


def _export_line(values: dict[str, str]) -> str:
    if not values:
        return ""
    args: list[str] = []
    for key in ("format", "rtl", "vendor", "library", "version", "display_name"):
        if values.get(key):
            value = str(values[key])
            args.extend([f"-{key}", _tcl_quote(value) if key in {"vendor", "library", "version", "display_name"} else value])
    return "export_design" + ((" " + " ".join(args)) if args else "")


def _export_config_line(values: dict[str, str]) -> str:
    args: list[str] = []
    for key in ("vivado_synth_strategy", "ip_xdc_file"):
        if values.get(key):
            args.extend([f"-{key}", _tcl_quote(str(values[key]))])
    return "config_export" + ((" " + " ".join(args)) if args else "")


def _report_lines(solution_name: str) -> list[str]:
    return [
        "file mkdir ./report",
        f"report_utilization -file ./report/{solution_name}_utilization.rpt",
        f"report_timing -file ./report/{solution_name}_timing.rpt",
        f"report_directive -file ./report/{solution_name}_directive.rpt",
        f"report_dataflow -file ./report/{solution_name}_dataflow.rpt",
        f"report_interface -file ./report/{solution_name}_interface.rpt",
    ]


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("}", "\\}") + "}"
