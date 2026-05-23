# HLS Imported Template Notes

Source note: this file records reusable HLS template guidance. Keep it generic, durable, and free of one-off source paths in generated outputs.

## 2D Block Transform Skeleton

- Prefer a read-reorder-compute-write decomposition when the kernel naturally applies one pass across rows and another across columns.
- Keep the intermediate boundary explicit with names such as `read_block`, `row_pass`, `transpose_or_reorder`, `col_pass`, and `write_block`.
- Use the skeleton to express ownership and reviewability first; do not hard-code a tutorial algorithm body into the generic template family.

## Outer Pipeline And Inner Concurrency

- When an outer loop is pipelined, assume the inner access pattern may need concurrent reads or writes even if the source code looks sequential.
- Treat inferred inner concurrency as the reason to inspect load/store bottlenecks before choosing `ARRAY_PARTITION` or `ARRAY_RESHAPE`.
- Bind the chosen dimension, factor, and local buffer comment to schedule-viewer or equivalent report evidence rather than to stylistic preference.

## Partition Versus Reshape

- Use `ARRAY_PARTITION` when the design needs more independent banks or element-level parallel access.
- Use `ARRAY_RESHAPE` when adjacent elements should move together through a widened local word or storage view.
- Do not apply both directives to the same local buffer in one solution.

## Optional Storage Binding

- Treat `BIND_STORAGE` as an explicit follow-up optimization for a named storage structure, not as a default pragma to emit automatically.
- If storage binding is used, keep the storage type and implementation reason visible in comments or validation notes.
- Do not introduce storage binding unless the report evidence shows that port structure, not algorithm structure, is the limiting factor.

## Dataflow Review Discipline

- `DATAFLOW` is not complete when the pragma appears; the stage split must still survive co-simulation, FIFO sizing review, and stall analysis.
- Require explicit channel depth assumptions whenever producer and consumer latency can differ.
- Keep read, compute, and write stages small enough that deadlock or backpressure issues can be traced to one boundary.

## Floating-Point And Fixed-Point Caution

- Tutorial floating-point examples inform portability and QoR review, but they do not justify silently converting a design into fixed-point.
- Fixed-point templates must state numeric range, integer bits, quantization mode, overflow mode, and error budget explicitly.
- Device migration review should compare interval, latency, slack, and resource deltas while preserving interface and numeric intent.
