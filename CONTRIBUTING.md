# Contributing / 贡献指南

Thank you for improving Erie HLS Generator. This repository is an agent skill first: changes should help an AI coding agent perform Vitis HLS work more reliably, not only add standalone Python behavior.

感谢你改进 Erie HLS Generator。本仓库首先是一个 Agent Skill：任何变更都应提升 AI 编程代理执行 Vitis HLS 工作流的可靠性，而不只是扩展独立 Python 脚本。

## Contribution Principles

- Keep `SKILL.md` concise. Move detailed background, tool behavior, and long examples into `references/`.
- Keep deterministic workflow logic in `runtime/` and stable host-facing APIs in `integration/`.
- Do not claim Vitis validation passed unless `vitis-run` or `vitis_hls` actually ran.
- Keep generated outputs, temporary reports, local credentials, and machine-specific paths out of commits.
- Preserve the HLS-only boundary: this skill should not become a handwritten RTL generator.

## Suggested Workflow

1. Open an issue describing the agent behavior, workflow gap, or validation problem.
2. Make a focused change with a clear before/after behavior.
3. Run the relevant static validation and smoke checks.
4. Include command output or validation evidence in the pull request.

## Validation

Useful local commands:

```powershell
python -m runtime.hls_generator --version
python -m runtime.hls_generator scaffold --target hls --name vector_scale --out .\reports\hls\spec.json
python -m runtime.hls_generator validate --target hls --spec .\reports\hls\spec.json --path .\reports\hls\generated --readiness static --no-external
python .\smoke\run_smoke.py
```

External AMD/Xilinx tooling is optional for many changes, but required before claiming hardware-tool acceptance.

