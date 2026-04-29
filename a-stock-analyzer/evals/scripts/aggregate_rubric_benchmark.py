#!/usr/bin/env python3
"""
Aggregate a-stock-analyzer grading.json files into a benchmark with rubric stats.

This complements skill-creator's aggregate_benchmark.py. It keeps the viewer-compatible
fields (pass_rate, time, tokens, expectations) and adds rubric_scores plus weighted_total.

Supported layouts:

  iteration-1/
    eval-1-basic-ai-optical-module/
      with_skill/grading.json
      without_skill/grading.json

  iteration-1/
    eval-1-basic-ai-optical-module/
      with_skill/run-1/grading.json
      with_skill/run-2/grading.json
      without_skill/run-1/grading.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS = {
    "instruction_following": 1.0,
    "data_completeness": 1.2,
    "output_quality": 1.4,
    "proactive_thinking": 1.0,
    "innovation": 0.6,
    "traceability": 1.1,
}

CONFIG_PRIORITY = {
    "with_skill": 0,
    "new_skill": 0,
    "old_skill": 1,
    "without_skill": 2,
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def stats(values: list[float]) -> dict[str, float]:
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}

    mean = sum(values) / len(values)
    if len(values) > 1:
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def numeric_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def mean_scores(values: list[float | None]) -> float | None:
    cleaned = [v for v in values if isinstance(v, (int, float))]
    if not cleaned:
        return None
    return round(sum(cleaned) / len(cleaned), 4)


def nested_score(payload: dict[str, Any], key: str) -> float | None:
    item = payload.get(key, {})
    if isinstance(item, dict):
        return numeric_score(item.get("score"))
    return None


def top_level_rubric_scores(rubric_scores: dict[str, Any]) -> dict[str, float | None]:
    instruction = rubric_scores.get("instruction_following", {})
    output_quality = rubric_scores.get("output_quality", {})

    scores = {
        "instruction_following": mean_scores([
            nested_score(instruction, "user_level") if isinstance(instruction, dict) else None,
            nested_score(instruction, "skill_level") if isinstance(instruction, dict) else None,
        ]),
        "data_completeness": nested_score(rubric_scores, "data_completeness"),
        "output_quality": mean_scores([
            nested_score(output_quality, "accuracy") if isinstance(output_quality, dict) else None,
            nested_score(output_quality, "logic") if isinstance(output_quality, dict) else None,
            nested_score(output_quality, "professionalism") if isinstance(output_quality, dict) else None,
            nested_score(output_quality, "readability") if isinstance(output_quality, dict) else None,
        ]),
        "proactive_thinking": nested_score(rubric_scores, "proactive_thinking"),
        "innovation": nested_score(rubric_scores, "innovation"),
        "traceability": nested_score(rubric_scores, "traceability"),
    }
    return scores


def weighted_total(top_scores: dict[str, float | None], weights: dict[str, float]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for metric, score in top_scores.items():
        if score is None:
            continue
        weight = float(weights.get(metric, 1.0))
        numerator += score * weight
        denominator += weight
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def eval_id_from_dir(eval_dir: Path, fallback: int) -> int:
    metadata = eval_dir / "eval_metadata.json"
    if metadata.exists():
        try:
            value = load_json(metadata).get("eval_id")
            if isinstance(value, int):
                return value
        except (json.JSONDecodeError, OSError):
            pass

    match = re.search(r"eval-(\d+)", eval_dir.name)
    if match:
        return int(match.group(1))
    return fallback


def eval_name_from_dir(eval_dir: Path, eval_id: int, eval_names: dict[int, str]) -> str:
    metadata = eval_dir / "eval_metadata.json"
    if metadata.exists():
        try:
            value = load_json(metadata).get("eval_name")
            if isinstance(value, str) and value:
                return value
        except (json.JSONDecodeError, OSError):
            pass

    if eval_id in eval_names:
        return eval_names[eval_id]
    return re.sub(r"^eval-\d+-?", "", eval_dir.name) or eval_dir.name


def iter_grading_files(config_dir: Path) -> list[tuple[int, Path]]:
    direct = config_dir / "grading.json"
    results: list[tuple[int, Path]] = []
    if direct.exists():
        results.append((1, direct))

    for run_dir in sorted(config_dir.glob("run-*")):
        if not run_dir.is_dir():
            continue
        grading = run_dir / "grading.json"
        if not grading.exists():
            continue
        match = re.search(r"run-(\d+)", run_dir.name)
        run_number = int(match.group(1)) if match else len(results) + 1
        results.append((run_number, grading))

    return results


def load_eval_config(evals_path: Path | None) -> tuple[str, dict[int, str], dict[str, float], int]:
    if not evals_path or not evals_path.exists():
        return "a-stock-analyzer", {}, DEFAULT_WEIGHTS, 3

    payload = load_json(evals_path)
    skill_name = payload.get("skill_name", "a-stock-analyzer")
    eval_names = {
        int(item["id"]): item.get("eval_name", f"eval-{item['id']}")
        for item in payload.get("evals", [])
        if isinstance(item, dict) and "id" in item
    }
    benchmark = payload.get("benchmark", {})
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(benchmark.get("rubric_weights", {}))
    runs_per_configuration = int(benchmark.get("default_runs_per_configuration", 3))
    return skill_name, eval_names, weights, runs_per_configuration


def collect_runs(iteration_dir: Path, evals_path: Path | None) -> tuple[list[dict[str, Any]], dict[str, float], str, int]:
    skill_name, eval_names, weights, runs_per_configuration = load_eval_config(evals_path)
    runs: list[dict[str, Any]] = []

    eval_dirs = sorted([p for p in iteration_dir.glob("eval-*") if p.is_dir()])
    for fallback, eval_dir in enumerate(eval_dirs, start=1):
        eval_id = eval_id_from_dir(eval_dir, fallback)
        eval_name = eval_name_from_dir(eval_dir, eval_id, eval_names)

        config_dirs = [p for p in eval_dir.iterdir() if p.is_dir()]
        config_dirs.sort(key=lambda p: (CONFIG_PRIORITY.get(p.name, 50), p.name))

        for config_dir in config_dirs:
            for run_number, grading_file in iter_grading_files(config_dir):
                grading = load_json(grading_file)
                summary = grading.get("summary", {})
                timing = grading.get("timing", {})
                timing_file = grading_file.parent / "timing.json"
                if not timing and timing_file.exists():
                    try:
                        timing = load_json(timing_file)
                    except (json.JSONDecodeError, OSError):
                        timing = {}

                metrics = grading.get("execution_metrics", {})
                rubric_scores = grading.get("rubric_scores", {})
                top_scores = top_level_rubric_scores(rubric_scores) if isinstance(rubric_scores, dict) else {}
                total = numeric_score(grading.get("weighted_total"))
                if total is None:
                    total = weighted_total(top_scores, weights)

                tokens = timing.get("total_tokens")
                if not isinstance(tokens, (int, float)):
                    tokens = metrics.get("total_tokens", metrics.get("output_chars", 0))

                runs.append({
                    "eval_id": eval_id,
                    "eval_name": eval_name,
                    "configuration": config_dir.name,
                    "run_number": run_number,
                    "result": {
                        "pass_rate": summary.get("pass_rate", 0.0),
                        "passed": summary.get("passed", 0),
                        "failed": summary.get("failed", 0),
                        "total": summary.get("total", 0),
                        "time_seconds": timing.get("total_duration_seconds", 0.0),
                        "tokens": tokens,
                        "tool_calls": metrics.get("total_tool_calls", 0),
                        "errors": metrics.get("errors_encountered", 0),
                        "weighted_total": total,
                        "rubric": top_scores,
                    },
                    "expectations": grading.get("expectations", []),
                    "rubric_scores": rubric_scores,
                    "notes": extract_notes(grading),
                })

    return runs, weights, skill_name, runs_per_configuration


def extract_notes(grading: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    user_notes = grading.get("user_notes_summary", {})
    if isinstance(user_notes, dict):
        for key in ("uncertainties", "needs_review", "workarounds"):
            value = user_notes.get(key, [])
            if isinstance(value, list):
                notes.extend(str(item) for item in value)
    feedback = grading.get("eval_feedback", {})
    if isinstance(feedback, dict) and feedback.get("overall"):
        notes.append(str(feedback["overall"]))
    return notes


def config_order(configs: list[str]) -> list[str]:
    return sorted(configs, key=lambda name: (CONFIG_PRIORITY.get(name, 50), name))


def aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    configs = config_order(sorted({run["configuration"] for run in runs}))
    summary: dict[str, Any] = {}

    for config in configs:
        config_runs = [run for run in runs if run["configuration"] == config]
        summary[config] = {
            "pass_rate": stats([run["result"].get("pass_rate", 0.0) for run in config_runs]),
            "time_seconds": stats([run["result"].get("time_seconds", 0.0) for run in config_runs]),
            "tokens": stats([run["result"].get("tokens", 0.0) for run in config_runs]),
            "weighted_total": stats([
                run["result"].get("weighted_total")
                for run in config_runs
                if run["result"].get("weighted_total") is not None
            ]),
            "rubric": {},
        }

        rubric_keys = sorted({
            key
            for run in config_runs
            for key, value in run["result"].get("rubric", {}).items()
            if value is not None
        })
        for key in rubric_keys:
            summary[config]["rubric"][key] = stats([
                run["result"].get("rubric", {}).get(key)
                for run in config_runs
                if run["result"].get("rubric", {}).get(key) is not None
            ])

    if len(configs) >= 2:
        primary, baseline = configs[0], configs[1]
        summary["delta"] = calculate_delta(summary[primary], summary[baseline])
    elif configs:
        summary["delta"] = calculate_delta(summary[configs[0]], {})
    else:
        summary["delta"] = {}

    return summary


def calculate_delta(primary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    def mean_at(payload: dict[str, Any], key: str) -> float:
        value = payload.get(key, {})
        return float(value.get("mean", 0.0)) if isinstance(value, dict) else 0.0

    delta = {
        "pass_rate": f"{mean_at(primary, 'pass_rate') - mean_at(baseline, 'pass_rate'):+.2f}",
        "time_seconds": f"{mean_at(primary, 'time_seconds') - mean_at(baseline, 'time_seconds'):+.1f}",
        "tokens": f"{mean_at(primary, 'tokens') - mean_at(baseline, 'tokens'):+.0f}",
        "weighted_total": f"{mean_at(primary, 'weighted_total') - mean_at(baseline, 'weighted_total'):+.2f}",
        "rubric": {},
    }

    primary_rubric = primary.get("rubric", {}) if isinstance(primary.get("rubric"), dict) else {}
    baseline_rubric = baseline.get("rubric", {}) if isinstance(baseline.get("rubric"), dict) else {}
    for key in sorted(set(primary_rubric) | set(baseline_rubric)):
        p = primary_rubric.get(key, {}).get("mean", 0.0)
        b = baseline_rubric.get(key, {}).get("mean", 0.0)
        delta["rubric"][key] = f"{float(p) - float(b):+.2f}"
    return delta


def generate_benchmark(
    iteration_dir: Path,
    evals_path: Path | None,
    skill_name_arg: str,
    skill_path: str,
    executor_model: str,
    analyzer_model: str,
) -> dict[str, Any]:
    runs, weights, inferred_skill_name, runs_per_configuration = collect_runs(iteration_dir, evals_path)
    skill_name = skill_name_arg or inferred_skill_name
    evals_run = sorted({run["eval_id"] for run in runs})

    return {
        "metadata": {
            "skill_name": skill_name,
            "skill_path": skill_path,
            "executor_model": executor_model,
            "analyzer_model": analyzer_model,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": evals_run,
            "runs_per_configuration": runs_per_configuration,
            "rubric_weights": weights,
        },
        "runs": runs,
        "run_summary": aggregate_runs(runs),
        "notes": benchmark_notes(runs),
    }


def benchmark_notes(runs: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    if not runs:
        return ["No grading.json files were found; run evals and grading before aggregating."]

    missing_rubric = [run for run in runs if not run.get("rubric_scores")]
    if missing_rubric:
        notes.append(f"{len(missing_rubric)} runs have no rubric_scores; weighted_total may be 0 or missing.")

    innovation_runs = [
        run for run in runs
        if run["result"].get("rubric", {}).get("innovation") is not None
    ]
    if innovation_runs:
        notes.append("Innovation scores are most useful when compared against a baseline output.")

    return notes


def generate_markdown(benchmark: dict[str, Any]) -> str:
    metadata = benchmark["metadata"]
    summary = benchmark["run_summary"]
    configs = [key for key in config_order([k for k in summary if k != "delta"])]
    delta = summary.get("delta", {})

    lines = [
        f"# A-Stock Analyzer Benchmark: {metadata['skill_name']}",
        "",
        f"**Date**: {metadata['timestamp']}",
        f"**Skill path**: `{metadata['skill_path']}`",
        f"**Evals**: {', '.join(map(str, metadata['evals_run'])) or 'none'}",
        "",
        "## Summary",
        "",
    ]

    if not configs:
        lines.append("No graded runs found.")
        return "\n".join(lines)

    header = "| Metric | " + " | ".join(label(config) for config in configs)
    if len(configs) >= 2:
        header += " | Delta |"
    else:
        header += " |"
    lines.extend([header, "|" + "---|" * (len(configs) + (1 if len(configs) >= 2 else 0) + 1)])

    for metric in ("pass_rate", "weighted_total", "time_seconds", "tokens"):
        cells = [pretty_metric(metric)]
        for config in configs:
            cells.append(format_stat(summary[config].get(metric, {}), metric))
        if len(configs) >= 2:
            cells.append(str(delta.get(metric, "—")))
        lines.append("| " + " | ".join(cells) + " |")

    rubric_keys = sorted({
        key
        for config in configs
        for key in summary.get(config, {}).get("rubric", {})
    })
    if rubric_keys:
        lines.extend(["", "## Rubric", ""])
        header = "| Dimension | " + " | ".join(label(config) for config in configs)
        if len(configs) >= 2:
            header += " | Delta |"
        else:
            header += " |"
        lines.extend([header, "|" + "---|" * (len(configs) + (1 if len(configs) >= 2 else 0) + 1)])
        for metric in rubric_keys:
            cells = [pretty_metric(metric)]
            for config in configs:
                cells.append(format_stat(summary[config].get("rubric", {}).get(metric, {}), metric))
            if len(configs) >= 2:
                cells.append(str(delta.get("rubric", {}).get(metric, "—")))
            lines.append("| " + " | ".join(cells) + " |")

    if benchmark.get("notes"):
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in benchmark["notes"])

    return "\n".join(lines) + "\n"


def label(config: str) -> str:
    return config.replace("_", " ").title()


def pretty_metric(metric: str) -> str:
    return metric.replace("_", " ").title()


def format_stat(payload: dict[str, Any], metric: str) -> str:
    mean = payload.get("mean", 0.0)
    stddev = payload.get("stddev", 0.0)
    if metric == "pass_rate":
        return f"{float(mean) * 100:.1f}% ± {float(stddev) * 100:.1f}%"
    if metric == "tokens":
        return f"{float(mean):.0f} ± {float(stddev):.0f}"
    if metric == "time_seconds":
        return f"{float(mean):.1f}s ± {float(stddev):.1f}s"
    return f"{float(mean):.2f} ± {float(stddev):.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate rubric-enhanced benchmark results.")
    parser.add_argument("iteration_dir", type=Path, help="Workspace iteration directory, e.g. a-stock-analyzer-workspace/iteration-1")
    parser.add_argument("--evals", type=Path, help="Path to evals.json")
    parser.add_argument("--skill-name", default="", help="Override skill name")
    parser.add_argument("--skill-path", default="", help="Skill path for benchmark metadata")
    parser.add_argument("--executor-model", default="<model-name>", help="Executor model metadata")
    parser.add_argument("--analyzer-model", default="<model-name>", help="Analyzer/grader model metadata")
    parser.add_argument("--output", type=Path, help="Output benchmark JSON path")
    args = parser.parse_args()

    evals_path = args.evals
    if evals_path is None:
        candidate = args.iteration_dir.parent.parent / "a-stock-analyzer" / "evals" / "evals.json"
        evals_path = candidate if candidate.exists() else None

    benchmark = generate_benchmark(
        iteration_dir=args.iteration_dir,
        evals_path=evals_path,
        skill_name_arg=args.skill_name,
        skill_path=args.skill_path,
        executor_model=args.executor_model,
        analyzer_model=args.analyzer_model,
    )

    output_json = args.output or (args.iteration_dir / "benchmark.json")
    output_md = output_json.with_suffix(".md")
    write_json(output_json, benchmark)
    output_md.write_text(generate_markdown(benchmark), encoding="utf-8")

    print(f"Generated: {output_json}")
    print(f"Generated: {output_md}")


if __name__ == "__main__":
    main()
