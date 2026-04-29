# A-Stock Analyzer Evals

This eval suite combines boolean expectations with 1-5 rubric scoring.

- `evals.json`: Test prompts, hard expectations, expected data coverage, proactive-thinking hooks, and rubric weights.
- `rubric_grader_prompt.md`: Grader prompt template for producing `grading.json` with `rubric_scores`.
- `scripts/aggregate_rubric_benchmark.py`: Aggregates `grading.json` files into `benchmark.json` and `benchmark.md`.

## Directory Layout

Use a workspace next to the skill directory:

```text
a-stock-analyzer-workspace/
└── iteration-1/
    └── eval-1-basic-ai-optical-module/
        ├── eval_metadata.json
        ├── with_skill/
        │   ├── outputs/
        │   │   └── report.md
        │   ├── transcript.md
        │   ├── timing.json
        │   └── grading.json
        └── without_skill/
            ├── outputs/
            │   └── report.md
            ├── transcript.md
            ├── timing.json
            └── grading.json
```

`with_skill` should read and follow `a-stock-analyzer/SKILL.md`.
`without_skill` should answer the same prompt without being given the skill path.

## Eval Metadata

For each eval directory, create `eval_metadata.json` from the matching item in `evals.json`.

Minimum shape:

```json
{
  "eval_id": 1,
  "eval_name": "basic-ai-optical-module",
  "prompt": "...",
  "expectations": ["..."],
  "coverage_points": ["..."],
  "proactive_hooks": ["..."],
  "rubric_focus": ["data_completeness", "output_quality"]
}
```

## Grading

Use `rubric_grader_prompt.md` to grade each completed run.

The grader must write `grading.json` next to `outputs/`, for example:

```text
iteration-1/eval-1-basic-ai-optical-module/with_skill/grading.json
```

`grading.json` should contain:

- `expectations`: boolean pass/fail results.
- `summary`: pass-rate summary.
- `rubric_scores`: 1-5 anchored scoring.
- `weighted_total`: weighted rubric average.
- `timing` and `execution_metrics` when available.

## Aggregate Benchmark

From the skills repository root:

```powershell
python .\a-stock-analyzer\evals\scripts\aggregate_rubric_benchmark.py `
  .\a-stock-analyzer-workspace\iteration-1 `
  --evals .\a-stock-analyzer\evals\evals.json `
  --skill-name a-stock-analyzer `
  --skill-path .\a-stock-analyzer
```

This creates:

```text
a-stock-analyzer-workspace/iteration-1/benchmark.json
a-stock-analyzer-workspace/iteration-1/benchmark.md
```

The generated `benchmark.json` is compatible with `skill-creator` review pages because it preserves the standard fields and adds rubric data under extra keys.

## Review Page

Generate a static review page:

```powershell
python C:\Users\chenh\.agents\skills\skill-creator-1.0.0\eval-viewer\generate_review.py `
  .\a-stock-analyzer-workspace\iteration-1 `
  --skill-name a-stock-analyzer `
  --benchmark .\a-stock-analyzer-workspace\iteration-1\benchmark.json `
  --static .\a-stock-analyzer-workspace\iteration-1\review.html
```

Open `review.html` to inspect outputs and benchmark results.
