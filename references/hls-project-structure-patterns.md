# HLS Project Structure Patterns

This file records reusable HLS engineering structure rules for this skill. It captures project-layout, module-boundary, and artifact-handling patterns that improve generation guidance without changing the HLS-only output surface.

## Minimal Vitis Kernel Flow

- A minimal accelerator-oriented project should keep the kernel compile/link flow readable:
  - one top kernel source
  - one kernel Makefile
  - one compile cfg file when compile options need to stay stable
  - one host-side load or launch path outside the generated HLS outputs
- Preserve the compile/link split explicitly:
  - `v++ -c` is the kernel compile boundary that produces `.xo`
  - `v++ -l` is the kernel link boundary that produces `.xclbin`
- Keep the generated HLS top function focused on interface intent and computation; do not mix package-stage or host-stage orchestration details into the HLS source.

## HLS In A Larger Vitis Project

- Real projects often organize around:
  - `kernel/`
  - `host/`
  - `package/`
  - one root orchestration script
- For this skill, treat that layout as engineering context rather than as generated output.
- Use the layout to explain where HLS artifacts fit, but keep the generated artifact surface limited to:
  - HLS headers
  - HLS sources
  - C++ testbenches
  - `.cfg` files for the HLS flow

## Kernel Variant Tree

- As a project grows, the root orchestration contract often stays stable while specialized kernel subtargets move into the kernel subtree.
- A practical variant tree may include:
  - a main `kernel/Makefile`
  - specialized `kernel_search/Makefile`
  - specialized `kernel_construct/Makefile`
  - additional variant directories when a family of accelerators shares the same wider project shell
- Treat this as a structure pattern: root flow remains readable, specialization moves downward into focused kernel subdirectories.

## Hotspot File Organization

- Complex pragma usage should concentrate in a small number of hotspot source files rather than being spread uniformly across every file.
- Helper headers and support sources should clarify data structures, helper stages, or decomposition boundaries without each becoming pragma-heavy on their own.
- Typical hotspot responsibilities include:
  - `m_axi` interface definitions
  - `DATAFLOW` stage boundaries
  - local buffer partition/reshape choices
  - storage binding and aggregate directives
  - stream depth and loop-tripcount decisions

## Helper Header Boundaries

- Helper headers should carry reusable type, helper-function, and stage-interface definitions.
- The main kernel source should remain the place where the top function, critical pragmas, and overall stage ownership are most visible.
- When a design becomes multi-module, prefer a clear division between:
  - top-level source that owns the interface contract
  - helper headers that support stage logic
  - hotspot sources that carry dense optimization directives

## Bundle And Depth Stability

- `bundle=` names, `depth=` values, and top-function naming should remain stable enough that downstream project integration can plan around them.
- Inside HLS C/C++:
  - use explicit `m_axi` bundles
  - keep `depth=` concrete for co-simulation and reviewability
- Outside HLS C/C++:
  - wider project flows may map bundles to platform memory resources or other connectivity policies
- This skill should support that downstream planning by keeping names and memory intent clear, not by generating those wider project files itself.

## Dual Host Entrypoints

- Some projects separate `host/` and `host_sw/` responsibilities.
- Treat this as a background engineering pattern that explains why a wider project may have more than one host-side build entrypoint.
- Do not turn that pattern into a reason for this skill to generate host code or package logic.

## Out-Of-Scope Engineering Context

- The following remain outside this skill's generated artifact boundary:
  - host code generation
  - package script generation
  - connectivity cfg generation for wider Vitis link/package flows
  - auto-generated implementation, cosim, or project byproduct trees
- These topics may inform references, examples, naming, and prompt guidance, but they must not silently expand the skill's output surface.
