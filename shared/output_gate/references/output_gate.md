# Production output gate

Runtime rules for a Skill package that ships `capabilities.yaml`, `outputs.yaml`
and a compiled `gate-plan.json`:

1. Invoke executable actions through `scripts/_shared/skill_runtime/runner.py` by
   capability ID. Do not call a terminal renderer or publisher directly.
2. Write model-authored final content under a `.staging/` directory, then use a
   registered `finalize` capability to validate and atomically promote it.
3. A terminal artifact is deliverable only when its SHA-256 and gate-audit
   SHA-256 match a successful receipt and the audit reports `gate_pass: true`.
4. Evidence supplied to a capability with `requires_receipts` must match the
   exact path and SHA-256 of an output recorded by a successful, same-date
   prerequisite receipt. Same-date file naming by itself is not provenance.
5. Keep audits run-scoped and immutable. Passing audits live under
   `reports/.audits/<run_id>/`; a failed candidate keeps its audit beside the
   staged artifact and must never overwrite an audit for an existing delivery.
6. `--capture-stdout` is only for non-terminal capabilities. Terminal outputs
   must use the runtime-bound staging path and cannot create a second artifact
   through stdout capture.
7. On failure, do not publish or clean up evidence, staging files, receipts or
   audits. Report the first gate problems verbatim.
8. A completion response must name the artifact, audit path, run ID and gate
   result. Natural-language claims of success are not a substitute for them.

Deterministic gates validate actions and artifacts. Subjective analytical
quality remains the model's responsibility and belongs in a separate rubric.
