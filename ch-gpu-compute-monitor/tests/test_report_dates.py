"""报告日与数据观测日必须保持为两套独立日期。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_report_html as R  # noqa: E402


def test_report_date_comes_from_frontmatter_not_evidence_asof():
    meta = yaml.safe_load("date: 2026-08-30\ndata_asof: 2026-08-29\n")
    report_date = R.resolve_report_date(meta)
    html = R.build_html(
        {"asof": "2026-08-29", "window_days": 90, "source_health": [], "models": {}},
        {},
        report_date,
    )

    assert report_date == "2026-08-30"
    assert "GPU 算力价格与供给监控 · 2026-08-30" in html
    assert "报告日 2026-08-30 · 数据截止 2026-08-29" in html


def test_report_date_override_is_validated():
    assert R.resolve_report_date({}, "2026-08-30") == "2026-08-30"
