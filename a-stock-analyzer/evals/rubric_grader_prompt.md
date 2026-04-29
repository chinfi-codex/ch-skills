# A-Stock Analyzer Rubric Grader

Use this prompt when grading a completed eval run for `a-stock-analyzer`.

## Inputs

You should receive:

- `eval_metadata`: The eval item from `evals/evals.json`, including `expectations`, `coverage_points`, `proactive_hooks`, and `rubric_focus`.
- `transcript_path`: Execution transcript. Required for skill-level instruction following.
- `outputs_dir`: Directory containing the final answer and any saved artifacts.
- `baseline_outputs_dir` (optional): Baseline output for the same eval. Use it only for innovation/delta judgments.

## Grading Principles

Grade hard assertions and quality rubrics separately:

- `expectations` are boolean. They catch objective failures and must use `text`, `passed`, and `evidence`.
- `rubric_scores` are 1-5 scores. They capture quality gradients and must include evidence.
- Do not reward fluent but unsupported claims. For stock research, unsupported confident prose is a weakness.
- If the transcript is unavailable, `instruction_following.skill_level.score` should not exceed 3 unless the output itself gives direct evidence that the skill workflow was followed.
- If a score cannot be judged, set `"score": null` and add `not_scored_reason`.

## Boolean Expectations

For each item in `eval_metadata.expectations`, decide:

- `passed: true` only when transcript or outputs provide clear evidence.
- `passed: false` when evidence is missing, contradicted, or superficial.

## Rubric Scores

### 1. Instruction Following

Score both layers separately:

- `user_level`: Did the output obey the user's explicit constraints?
- `skill_level`: Did the run follow the skill workflow, such as resolving stock code, using the atomic data fetcher, using the right data scope, and respecting non-investment-advice boundaries?

Anchors:

- 1: Ignores at least one critical user or skill constraint.
- 3: Follows the main task but misses one or two secondary constraints.
- 5: Fully follows explicit prompt constraints and the a-stock-analyzer workflow.

### 2. Data Completeness

Use `coverage_points` as the checklist. Compute `coverage_rate = covered / total`.

Suggested mapping:

- 1: coverage < 0.50
- 2: 0.50 <= coverage < 0.70
- 3: 0.70 <= coverage < 0.85
- 4: 0.85 <= coverage < 1.00
- 5: coverage = 1.00, or every missing item has a justified fallback

Evidence must identify covered and missing points. Do not count hallucinated or unsourced data as covered.

### 3. Output Quality

Score four subdimensions:

- `accuracy`: factual/data correctness, no fabrication, correct caveats.
- `logic`: conclusions follow from evidence, no unsupported leaps.
- `professionalism`: investment-research tone, correct concepts, bounded claims.
- `readability`: clear structure, concise scan-friendly sections.

Anchors:

- 1: Obvious factual errors, invented data, or conclusions unsupported by evidence.
- 3: Mostly usable but vague, uneven, or weakly reasoned.
- 5: Accurate, evidence-led, logically tight, professional, and easy to scan.

### 4. Proactive Thinking

Use `proactive_hooks` as the checklist. Compute `hooks_caught`.

Anchors:

- 1: Misses all hooks and performs the task literally.
- 3: Catches some hooks, but analysis is shallow or incomplete.
- 5: Catches most hooks and turns them into useful, bounded analysis.

### 5. Innovation

Innovation is meaningful mainly relative to baseline.

When `baseline_outputs_dir` is available:

- Identify useful insights in with-skill output that are absent from baseline.
- Count only concrete, evidence-backed, non-obvious insights.

When no baseline is available:

- You may score based on absolute output, but lower confidence and state that it is not baseline-calibrated.

Anchors:

- 1: Mechanical summary with no useful incremental discovery.
- 3: One or two interesting points, but underdeveloped.
- 5: Concrete, non-obvious insights that a user would plausibly reuse.

### 6. Traceability

For each key claim, check whether it has a data period, fetched-data evidence, filing/report reference, web citation, or explicit uncertainty note.

Anchors:

- 1: Key claims lack dates, sources, or data periods.
- 3: Most claims are traceable, but several important claims lack evidence.
- 5: Every key conclusion has clear evidence or an uncertainty note.

## Weighted Total

Use `benchmark.rubric_weights` from `evals/evals.json` if provided. Compute a weighted average over non-null top-level metric scores.

Top-level scores:

- `instruction_following`: mean of `user_level.score` and `skill_level.score`.
- `data_completeness`: its score.
- `output_quality`: mean of accuracy, logic, professionalism, readability.
- `proactive_thinking`: its score.
- `innovation`: its score, if scored.
- `traceability`: its score.

## Output Format

Write `grading.json` as a sibling of `outputs_dir`.

```json
{
  "expectations": [
    {
      "text": "先使用 search 解析股票名称或明确说明已识别出的 A 股代码。",
      "passed": true,
      "evidence": "Transcript shows data_fetcher.py search was run for 中际旭创 and output uses 300308.SZ."
    }
  ],
  "summary": {
    "passed": 7,
    "failed": 1,
    "total": 8,
    "pass_rate": 0.875
  },
  "rubric_scores": {
    "instruction_following": {
      "user_level": {"score": 5, "evidence": "..."},
      "skill_level": {"score": 4, "evidence": "..."}
    },
    "data_completeness": {
      "score": 4,
      "coverage_rate": 0.9,
      "covered": ["..."],
      "missing": ["..."],
      "evidence": "..."
    },
    "output_quality": {
      "accuracy": {"score": 4, "evidence": "..."},
      "logic": {"score": 4, "evidence": "..."},
      "professionalism": {"score": 5, "evidence": "..."},
      "readability": {"score": 5, "evidence": "..."}
    },
    "proactive_thinking": {
      "score": 4,
      "hooks_caught": 2,
      "hooks_total": 3,
      "evidence": "..."
    },
    "innovation": {
      "score": 3,
      "vs_baseline_delta": "+1 useful insight",
      "evidence": "..."
    },
    "traceability": {
      "score": 4,
      "evidence": "..."
    }
  },
  "weighted_total": 4.1,
  "execution_metrics": {},
  "timing": {},
  "claims": [],
  "user_notes_summary": {
    "uncertainties": [],
    "needs_review": [],
    "workarounds": []
  },
  "eval_feedback": {
    "suggestions": [],
    "overall": "No major eval gaps found."
  }
}
```
