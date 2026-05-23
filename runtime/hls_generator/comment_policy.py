"""Typed comment-placement policy for generated HLS C/C++ artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HLS_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh"}

_GENERIC_COMMENT_PATTERNS = (
    "generic generated line",
    "not hardware intent",
    "keep the generated hls artifact line reviewable",
    "preserve the generated data movement or computation step",
    "open or close the generated hardware scope",
    "misplaced top function",
)

_FUNCTION_KEYWORDS = (
    "function",
    "top",
    "hardware",
    "boundary",
    "kernel",
    "entrypoint",
    "declaration",
    "contract",
    "testbench",
    "函数",
    "边界",
    "入口",
    "声明",
)

_TYPE_KEYWORDS = (
    "type",
    "struct",
    "field",
    "width",
    "protocol",
    "contract",
    "metadata",
    "sample",
    "类型",
    "字段",
    "位宽",
    "契约",
)

_INCLUDE_KEYWORDS = (
    "include",
    "dependency",
    "provide",
    "import",
    "reuse",
    "header",
    "library",
    "emit",
    "report",
    "fixed-width",
    "依赖",
    "引入",
    "头文件",
)

_MACRO_KEYWORDS = (
    "constant",
    "compile",
    "factor",
    "width",
    "parameter",
    "config",
    "contract",
    "guard",
    "single-included",
    "常量",
    "编译",
    "参数",
    "契约",
    "包含",
    "声明",
    "一次",
)

_PRAGMA_KEYWORDS = (
    "map",
    "expose",
    "use",
    "request",
    "constrain",
    "interface",
    "pipeline",
    "axi",
    "bundle",
    "control",
    "cycle",
    "safe",
    "independent",
    "dataflow",
    "stream",
    "partition",
    "reshape",
    "unroll",
    "depth",
    "映射",
    "接口",
    "流水",
    "控制",
    "约束",
)

_LOCAL_KEYWORDS = (
    "setup",
    "datapath",
    "loop",
    "transaction",
    "sample",
    "buffer",
    "case",
    "expected",
    "kernel call",
    "call",
    "emit",
    "status",
    "iterate",
    "pass",
    "fail",
    "check",
    "compare",
    "write",
    "read",
    "scale",
    "token",
    "设置",
    "循环",
    "遍历",
    "用例",
    "期望",
    "调用",
    "通过",
    "失败",
    "输出",
    "状态",
    "标记",
    "读",
    "写",
)

_FILE_HEADER_KEYWORDS = (
    "file",
    "header",
    "source",
    "testbench",
    "declare",
    "implement",
    "validates",
    "kernel",
    "文件",
    "头文件",
    "源码",
    "测试",
)

_CONTROL_PREFIXES = ("if", "for", "while", "switch", "return", "catch")


@dataclass(frozen=True)
class CommentPolicyIssue:
    message: str
    path: str
    line: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "detail": self.detail}


def validate_hls_comment_policy(root: Path, hls_files: list[Path], *, top_function: str) -> tuple[list[CommentPolicyIssue], dict[str, Any]]:
    issues: list[CommentPolicyIssue] = []
    metrics: dict[str, Any] = {
        "policy": "typed_hls_comment_placement",
        "checked_files": [],
        "checked_structures": 0,
        "issues": [],
    }
    for path in hls_files:
        if path.suffix.lower() not in HLS_SOURCE_SUFFIXES:
            continue
        rel_path = path.relative_to(root).as_posix()
        file_issues, checked = _validate_file(path.read_text(encoding="utf-8", errors="ignore").splitlines(), rel_path, top_function=top_function)
        issues.extend(file_issues)
        metrics["checked_files"].append(rel_path)
        metrics["checked_structures"] += checked
    metrics["issues"] = [issue.to_dict() for issue in issues]
    return issues, metrics


def _validate_file(lines: list[str], rel_path: str, *, top_function: str) -> tuple[list[CommentPolicyIssue], int]:
    issues: list[CommentPolicyIssue] = []
    checked = 0
    function_depth = 0
    type_depth = 0

    header_issue = _file_header_issue(lines, rel_path)
    if header_issue:
        issues.append(header_issue)
    else:
        checked += 1

    for index, raw_line in enumerate(lines):
        line_number = index + 1
        code = _code_part(raw_line).strip()
        if not code or _is_comment_only(raw_line):
            continue
        if _is_trivial_line(code):
            function_depth = _updated_depth(function_depth, code)
            type_depth = _updated_depth(type_depth, code)
            continue

        line_issues, did_check = _validate_code_line(
            lines,
            index,
            rel_path,
            top_function=top_function,
            in_function=function_depth > 0,
            in_type=type_depth > 0,
        )
        issues.extend(line_issues)
        checked += did_check

        if _is_type_definition(code):
            type_depth = max(type_depth, _brace_delta(code))
        elif type_depth > 0:
            type_depth = _updated_depth(type_depth, code)

        if _is_function_signature(code):
            function_depth = max(function_depth, _brace_delta(code))
        elif function_depth > 0:
            function_depth = _updated_depth(function_depth, code)

    return issues, checked


def _validate_code_line(
    lines: list[str],
    index: int,
    rel_path: str,
    *,
    top_function: str,
    in_function: bool,
    in_type: bool,
) -> tuple[list[CommentPolicyIssue], int]:
    raw_line = lines[index]
    line_number = index + 1
    code = _code_part(raw_line).strip()
    issues: list[CommentPolicyIssue] = []
    checked = 0

    inline = _inline_comment(raw_line)
    preceding = _preceding_comment(lines, index)

    for comment in [item for item in (inline, preceding) if item]:
        if _is_generic_comment(comment):
            issues.append(_issue(rel_path, line_number, "Comment policy rejects generic or misplaced comment text.", comment))
            checked += 1

    if _is_include(code):
        checked += 1
        issues.extend(_require_inline(rel_path, line_number, inline, _INCLUDE_KEYWORDS, "include dependency"))
    elif _is_pragma_once(code):
        checked += 1
        issues.extend(_require_inline(rel_path, line_number, inline, _MACRO_KEYWORDS, "include guard"))
    elif _is_macro(code):
        checked += 1
        issues.extend(_require_inline(rel_path, line_number, inline, _MACRO_KEYWORDS, "macro contract"))
    elif _is_hls_pragma(code):
        checked += 1
        issues.extend(_require_inline(rel_path, line_number, inline, _PRAGMA_KEYWORDS, "HLS pragma intent"))
    elif _is_type_definition(code):
        checked += 1
        issues.extend(_require_preceding(rel_path, line_number, preceding, _TYPE_KEYWORDS, "type contract"))
    elif _is_function_signature(code):
        checked += 1
        issues.extend(_require_preceding(rel_path, line_number, preceding, _FUNCTION_KEYWORDS, "function contract"))
    elif in_function and _is_loop(code):
        checked += 1
        issues.extend(_require_nearby(rel_path, line_number, inline, preceding, _LOCAL_KEYWORDS, "loop intent"))
    elif in_function and _is_local_declaration(code) and not in_type:
        checked += 1
        issues.extend(_require_nearby(rel_path, line_number, inline, preceding, _LOCAL_KEYWORDS, "local variable purpose"))
    elif in_function and _is_critical_assignment(code):
        checked += 1
        issues.extend(_require_nearby(rel_path, line_number, inline, preceding, _LOCAL_KEYWORDS, "datapath assignment intent"))
    elif _is_testbench_top_call(code, top_function):
        checked += 1
        issues.extend(_require_nearby(rel_path, line_number, inline, preceding, _LOCAL_KEYWORDS, "kernel call intent"))
    elif _is_pass_fail_line(code):
        checked += 1
        issues.extend(_require_nearby(rel_path, line_number, inline, preceding, _LOCAL_KEYWORDS, "PASS/FAIL behavior"))

    return issues, checked


def _file_header_issue(lines: list[str], rel_path: str) -> CommentPolicyIssue | None:
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if not _is_comment_only(line):
            return _issue(rel_path, index + 1, "Comment policy requires a short file-header comment before generated C/C++ code.", line.strip())
        comment = _comment_only_text(line)
        if _is_generic_comment(comment) or not _contains_any(comment, _FILE_HEADER_KEYWORDS):
            return _issue(rel_path, index + 1, "Comment policy file header must describe the file role.", comment)
        return None
    return _issue(rel_path, 1, "Comment policy requires non-empty generated C/C++ content.", "")


def _require_inline(rel_path: str, line_number: int, inline: str | None, keywords: tuple[str, ...], label: str) -> list[CommentPolicyIssue]:
    if not inline:
        return [_issue(rel_path, line_number, f"Comment policy requires inline {label} comment.", "")]
    if not _contains_any(inline, keywords):
        return [_issue(rel_path, line_number, f"Comment policy inline comment does not describe {label}.", inline)]
    return []


def _require_preceding(rel_path: str, line_number: int, preceding: str | None, keywords: tuple[str, ...], label: str) -> list[CommentPolicyIssue]:
    if not preceding:
        return [_issue(rel_path, line_number, f"Comment policy requires an immediately preceding {label} comment.", "")]
    if not _contains_any(preceding, keywords):
        return [_issue(rel_path, line_number, f"Comment policy preceding comment does not describe {label}.", preceding)]
    return []


def _require_nearby(rel_path: str, line_number: int, inline: str | None, preceding: str | None, keywords: tuple[str, ...], label: str) -> list[CommentPolicyIssue]:
    comment = inline or preceding
    if not comment:
        return [_issue(rel_path, line_number, f"Comment policy requires nearby {label} comment.", "")]
    if not _contains_any(comment, keywords):
        return [_issue(rel_path, line_number, f"Comment policy nearby comment does not describe {label}.", comment)]
    return []


def _issue(rel_path: str, line_number: int, message: str, detail: str) -> CommentPolicyIssue:
    return CommentPolicyIssue(message=message, path=rel_path, line=line_number, detail=detail)


def _code_part(line: str) -> str:
    if "//" in line:
        return line.split("//", 1)[0]
    if "/*" in line:
        return line.split("/*", 1)[0]
    return line


def _inline_comment(line: str) -> str | None:
    if "//" in line:
        return line.split("//", 1)[1].strip()
    match = re.search(r"/\*(.*?)\*/", line)
    return match.group(1).strip() if match else None


def _is_comment_only(line: str) -> bool:
    return line.strip().startswith(("//", "/*", "*"))


def _comment_only_text(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("//"):
        return stripped[2:].strip()
    if stripped.startswith("/*"):
        return stripped.strip("/* ").strip()
    if stripped.startswith("*"):
        return stripped.strip("* ").strip()
    return stripped


def _preceding_comment(lines: list[str], index: int) -> str | None:
    if index == 0:
        return None
    previous = lines[index - 1]
    if not previous.strip() or not _is_comment_only(previous):
        return None
    return _comment_only_text(previous)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _is_generic_comment(comment: str) -> bool:
    lowered = comment.lower()
    return any(pattern in lowered for pattern in _GENERIC_COMMENT_PATTERNS)


def _is_include(code: str) -> bool:
    return code.startswith("#include")


def _is_pragma_once(code: str) -> bool:
    return code.startswith("#pragma once")


def _is_macro(code: str) -> bool:
    return code.startswith("#define")


def _is_hls_pragma(code: str) -> bool:
    return code.startswith("#pragma HLS")


def _is_type_definition(code: str) -> bool:
    return bool(re.match(r"^(?:typedef\b|using\b|struct\b|class\b|enum\b)", code))


def _is_function_signature(code: str) -> bool:
    if not ("(" in code and ")" in code and (code.endswith(";") or code.endswith("{"))):
        return False
    if code.startswith(("hls::stream", "hls::task")):
        return False
    if code.split("(", 1)[0].strip().split(" ")[0] in _CONTROL_PREFIXES:
        return False
    return bool(re.match(r"^(?:[\w:<>~,\*&\[\]\s]+)\s+[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?\s*\([^;{}]*\)\s*(?:const\s*)?(?:;|\{)$", code))


def _is_loop(code: str) -> bool:
    return bool(re.match(r"^(?:for|while)\s*\(", code))


def _is_local_declaration(code: str) -> bool:
    if _is_function_signature(code) or _is_type_definition(code) or code.startswith("#"):
        return False
    return bool(re.match(r"^(?:const\s+)?(?:ap_u?int<[^>]+>|ap_fixed<[^>]+>|bool|int|unsigned|float|double|size_t|sample_word_t|hls::stream<[^>]+>|hls::task)\s+[\w\[\]]+", code))


def _is_critical_assignment(code: str) -> bool:
    if "==" in code or "!=" in code or "<=" in code or ">=" in code:
        return False
    if "=" not in code:
        return False
    return "[" in code or ".write" in code or ".read" in code or "output" in code.lower() or "observed" in code.lower()


def _is_testbench_top_call(code: str, top_function: str) -> bool:
    return bool(top_function and f"{top_function}(" in code and not _is_function_signature(code))


def _is_pass_fail_line(code: str) -> bool:
    return "PASS" in code or "FAIL" in code


def _is_trivial_line(code: str) -> bool:
    stripped = code.strip()
    return stripped in {"{", "}", "};", "};"} or stripped.startswith("return ")


def _brace_delta(code: str) -> int:
    return code.count("{") - code.count("}")


def _updated_depth(depth: int, code: str) -> int:
    return max(0, depth + _brace_delta(code))
