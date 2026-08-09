# Production output gate

Runtime rules for a Skill package that ships `capabilities.yaml`, `outputs.yaml`
and a compiled `gate-plan.json`:

1. Invoke executable actions through `scripts/_shared/skill_runtime/runner.py` by
   capability ID. Do not call a terminal renderer or publisher directly.
2. Write model-authored final content under a `.staging/` directory, then use a
   registered `finalize` capability to validate and atomically promote it.
3. A terminal artifact is deliverable only when its SHA-256 and gate-audit
   SHA-256 match a successful receipt and the audit reports `gate_pass: true`.
4. On failure, do not publish or clean up evidence, staging files, receipts or
   audits. Report the first gate problems verbatim.
5. A completion response must name the artifact, audit path, run ID and gate
   result. Natural-language claims of success are not a substitute for them.

Deterministic gates validate actions and artifacts. Subjective analytical
quality remains the model's responsibility and belongs in a separate rubric.
