"""测试公共夹具：临时金库 + 临时运行态，不连接 PG、不调用模型或消息通道。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import proactive_goal as pg  # noqa: E402
from proactive import evaluate, pulse, sources, store, validate  # noqa: E402
from proactive.context import CTX, NOTIFY_ACTIONS, parse_time  # noqa: E402
from proactive.sources import catalyst, framework, trend  # noqa: E402

NOW = "2026-10-27T10:00:00+08:00"
JOB = "a" * 12
DELIVERY_JOB = "b" * 12


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        CTX.configure(vault_root=root / "vault", runtime_home=root / "runtime")
        vault = CTX.root
        for path in (CTX.events, CTX.board, CTX.runtime):
            (path if path == CTX.runtime else path.parent).mkdir(parents=True, exist_ok=True)
        raw = vault / "RAW" / "example.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("原始证据正文", encoding="utf-8")
        for rel in CTX.framework_files + [CTX.board_framework]:
            path = vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("既定框架\n", encoding="utf-8")
        (vault / CTX.rules_dir).mkdir(parents=True, exist_ok=True)
        self.board = {"version": "2026-10-01", "stance": {"reading": "旧基线"},
                      "nodes": {"S2-hbm": {"name": "HBM/存储"}}, "tracking_vars": [{"id": "TV-hbm", "name": "HBM"}]}
        CTX.board.write_text(json.dumps(self.board, ensure_ascii=False), encoding="utf-8")
        CTX.board_history.write_text("", encoding="utf-8")
        self.events: list[dict] = []
        self.trend = sources.candidate("trend_state", "2026-10-26", "2026-10-26", {
            "ref_date": "2026-10-26", "state": "退潮", "arming": [],
        })
        self.trend_calls = 0

        def fake_trend(now, policy):
            self.trend_calls += 1
            return self.trend, []

        self.patches = [
            patch.object(trend, "trading_candidate", side_effect=fake_trend),
            patch.object(catalyst, "catalyst_candidates", side_effect=lambda now: list(self.events)),
            patch.dict(os.environ, {"HERMES_HOME": str(root / "hermes")}, clear=False),
            patch.object(pulse, "_pg_watermark", return_value="pg-stable"),
            patch.object(pulse, "trigger_evaluation", return_value=(True, "")),
        ]
        for item in self.patches:
            item.start()
        self.output_dir = root / "hermes" / "cron" / "output" / JOB
        self.output_dir.mkdir(parents=True)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        CTX.configure()
        self.tmp.cleanup()

    def call(self, command, now=NOW, **kwargs):
        out = io.StringIO()
        defaults = {"dry_run": False, "delivery_job_id": None}
        args = argparse.Namespace(now=now, **{**defaults, **kwargs})
        with contextlib.redirect_stdout(out):
            code = getattr(pg, f"cmd_{command}")(args)
        text = out.getvalue()
        if command in {"dispatch", "pulse"}:
            return code, text.strip() or None, None
        payload = json.JSONDecoder().raw_decode(text)[0] if text.strip() else None
        gate = json.loads(text.strip().splitlines()[-1]) if command in {"scan", "gate"} and text.strip() else None
        return code, payload, gate

    def init(self, now=NOW):
        return self.call("init", now)

    def scan(self, now=NOW):
        return self.call("scan", now)

    def pulse(self, now=NOW, channel="local"):
        return self.call("pulse", now, evaluation_job_id=JOB, delivery_job_id=DELIVERY_JOB, channel=channel)

    def gate(self, now=NOW):
        return self.call("gate", now)

    def add_event(self, native="CE-20261027-test", title="基本面边际变化", version=1, observed_at="2026-10-27"):
        item = sources.candidate("catalyst", native, observed_at, {
            "event_id": native, "title": title, "version": version, "rating": "A",
            "source_refs": [{"raw_path": "RAW/example.md"}], "negative_chain": ["保留反证"],
        })
        self.events = [row for row in self.events if row["native_id"] != native]
        self.events.append(item)
        return item

    def decide(self, item, action="silent", now=NOW, message="合格的真实提醒", **kwargs):
        return self.decide_rows([self.row(item, action, message, **kwargs)], now)

    def row(self, item, action="silent", message="合格的真实提醒", **kwargs):
        row = {"candidate_id": item["candidate_id"], "evidence_hash": item["evidence_hash"],
               "action": action, "reason": "测试决策", **kwargs}
        if action in NOTIFY_ACTIONS and "brief" not in kwargs:
            row["message"] = message
        return row

    def decide_rows(self, rows, now=NOW):
        state = store.load_state()
        fresh, _extra, _errors = sources.collect(state, parse_time(now))
        return validate.record_batch(state, rows, parse_time(now), "test-run", evaluate.latest_native(fresh, state))

    def dispatch(self, now=NOW, delivery_job_id=None):
        return self.call("dispatch", now, evaluation_job_id=JOB, channel="local", delivery_job_id=delivery_job_id)

    def write_run(self, body, name="2026-10-27_10-00-00.md"):
        raw = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
        text = "# Cron Job: test\n\n## Prompt\n\n…\n\n**Response Characters:** " + str(len(raw)) + "\n## Response\n\n" + raw + "\n"
        path = self.output_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def state(self):
        return store.load_state()

    def save(self, state):
        store.save_state(state)
