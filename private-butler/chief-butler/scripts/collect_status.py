#!/usr/bin/env python3
"""Collect child skill summaries into Chief Butler dashboard state."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def profile_root_from_chief(base_dir):
    return base_dir.parents[1]


def run_export(script_path, base_dir):
    subprocess.run([str(script_path), "--base-dir", str(base_dir)], check=True)


def load_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect(base_dir, *, refresh=True):
    profile_root = profile_root_from_chief(base_dir)
    skills_root = profile_root / "skills"
    child_specs = [
        {
            "name": "health-butler",
            "base_dir": skills_root / "health-butler",
            "export_script": skills_root / "health-butler" / "scripts" / "export_summary.py",
            "summary": skills_root / "health-butler" / "data" / "summary.json",
        },
        {
            "name": "simple-todo",
            "base_dir": skills_root / "simple-todo",
            "export_script": skills_root / "simple-todo" / "scripts" / "export_summary.py",
            "summary": skills_root / "simple-todo" / "data" / "summary.json",
        },
    ]

    skills = []
    alerts = []
    for spec in child_specs:
        if refresh and spec["export_script"].exists():
            run_export(spec["export_script"], spec["base_dir"])
        summary = load_json(spec["summary"])
        if summary:
            skills.append(summary)
            for alert in summary.get("alerts", []):
                alerts.append({**alert, "skill": summary.get("skill")})
        else:
            skills.append({
                "skill": spec["name"],
                "status": "missing_summary",
                "updated_at": None,
                "cards": [],
                "alerts": [{"level": "warn", "text": "summary missing"}],
            })

    state = {
        "schema": "chief-butler-dashboard-state",
        "version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "skills": skills,
        "alerts": alerts,
        "dashboard": {
            "path": str(base_dir / "dashboard" / "index.html"),
            "note": "Current HTML layout is preserved; chief state is prepared for later UI migration.",
        },
    }
    output = base_dir / "data" / "dashboard_state.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state, output


def main():
    parser = argparse.ArgumentParser(description="Collect Chief Butler child skill summaries")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    state, output = collect(base_dir, refresh=not args.no_refresh)
    print(f"Collected {len(state['skills'])} skill summaries into {output}")


if __name__ == "__main__":
    main()
