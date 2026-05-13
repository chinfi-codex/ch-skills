#!/usr/bin/env python3
"""Refresh the Chief Butler dashboard without changing its layout."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def default_base_dir():
    return Path(__file__).resolve().parents[1]


def profile_root_from_chief(base_dir):
    return base_dir.parents[1]


def main():
    parser = argparse.ArgumentParser(description="Refresh Chief Butler dashboard")
    parser.add_argument("--base-dir", default=None)
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser() if args.base_dir else default_base_dir()
    profile_root = profile_root_from_chief(base_dir)
    health_dir = profile_root / "skills" / "health-butler"
    dashboard = base_dir / "dashboard" / "index.html"

    subprocess.run([str(health_dir / "scripts" / "calculate_daily_budget.py"), "--base-dir", str(health_dir), "--all", "--quiet"], check=True)
    subprocess.run([str(health_dir / "scripts" / "export_dashboard_data.py"), "--base-dir", str(health_dir)], check=True)
    subprocess.run(
        [
            str(health_dir / "scripts" / "refresh_dashboard_data.py"),
            "--base-dir",
            str(health_dir),
            "--dashboard",
            str(dashboard),
        ],
        check=True,
    )
    subprocess.run([str(base_dir / "scripts" / "collect_status.py"), "--base-dir", str(base_dir)], check=True)
    print(f"Refreshed Chief Butler dashboard at {dashboard}")


if __name__ == "__main__":
    main()
