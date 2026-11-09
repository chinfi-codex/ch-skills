"""v0.5 工程项测试：主题冷却、指纹继承、回执对账、排序、健康告警、压缩、单次采集、
规则摘要、事件唤醒（pulse / gate / signal）与 brief 渲染。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from helpers import DELIVERY_JOB, JOB, NOW, RuntimeCase, catalyst, parse_time, pulse, sources, store, trend
from proactive import evaluate, outbox, render, runs
from proactive.context import CTX, digest, iso


def overdue_board(case, version="2026-09-01"):
    case.board["version"] = version
    CTX.board.write_text(json.dumps(case.board, ensure_ascii=False), encoding="utf-8")


def brief(**extra):
    return {"headline": "框架该体检了", "what_changed": "版本停在 09-01。", "so_what": "安排体检。",
            "falsifier": "已完成体检。", "source": "供需平衡框架体检约定", **extra}


class TopicDedupTest(RuntimeCase):
    def test_board_candidate_is_stable_across_days(self):
        overdue_board(self)
        state = store.default_state(parse_time(NOW))
        first = sources.collect(state, parse_time(NOW))[0]
        later = sources.collect(state, parse_time("2026-10-29T10:00:00+08:00"))[0]
        pick = lambda rows: next(r for r in rows if r["kind"] == "framework_review")
        self.assertEqual(pick(first)["candidate_id"], pick(later)["candidate_id"])
        self.assertNotEqual(pick(first)["evidence"]["overdue_days"], pick(later)["evidence"]["overdue_days"])

    def test_same_topic_is_suppressed_within_cooldown(self):
        self.init()
        overdue_board(self)
        _, payload, _ = self.scan()
        item = next(r for r in payload["candidates"] if r["kind"] == "framework_review")
        self.decide_rows([self.row(item, "needs_user", brief=brief(ask="1 安排；2 推迟"), priority="P3")])
        _, message, _ = self.dispatch()
        self.assertTrue(message.startswith("【P3 需你决定】"))
        # 体检后又到期的新版本：同一主题，7 天冷却内不再提醒
        overdue_board(self, "2026-09-05")
        _, payload, _ = self.scan("2026-10-29T10:00:00+08:00")
        again = next(r for r in payload["candidates"] if r["kind"] == "framework_review")
        self.assertNotEqual(again["candidate_id"], item["candidate_id"])
        outcome = self.decide_rows([self.row(again, "needs_user", brief=brief(ask="1；2"), priority="P3")],
                                   "2026-10-29T10:00:00+08:00")[0]
        self.assertEqual(outcome["outcome"], "suppressed_cooldown")

    def test_recent_notifications_reach_the_evaluator(self):
        self.init()
        item = self.add_event()
        self.scan()
        self.decide(item, "notify", message="【P2 基本面】旧提醒\n正文")
        self.dispatch()
        payload = evaluate.wake_payload([], [], parse_time(NOW), self.state())
        self.assertEqual(payload["recent_notifications"][0]["headline"], "【P2 基本面】旧提醒")
        self.assertEqual(payload["recent_notifications"][0]["topic_key"], "catalyst:CE-20261027-test")


class FingerprintInheritanceTest(RuntimeCase):
    def test_v2_trend_with_same_semantics_is_not_reopened(self):
        self.init()
        self.trend = sources.candidate("trend_state", "2026-10-26", "2026-10-26", {
            "ref_date": "2026-10-26", "state": "退潮", "arming": [], "rule_texts": {"R-016": "新摘要"}})
        state = self.state()
        old_evidence = dict(self.trend["evidence"], rule_texts={"R-016": "旧的长原文" * 50})
        old_id = "PG-" + digest({"kind": "trend_state", "native_id": "2026-10-26", "evidence": old_evidence})[:16]
        (CTX.packets).mkdir(parents=True, exist_ok=True)
        (CTX.packets / f"{old_id}.json").write_text(json.dumps({"candidate_id": old_id, "evidence": old_evidence}), encoding="utf-8")
        state["candidates"] = {old_id: {"evidence_hash": "0" * 64, "kind": "trend_state", "native_id": "2026-10-26",
                                        "attempts": 1, "last_action": "silent", "fingerprint_version": 2,
                                        "packet_path": f"packets/{old_id}.json"}}
        self.save(state)
        _, payload, gate = self.scan()
        self.assertFalse(gate["wakeAgent"])
        inherited = self.state()["candidates"][self.trend["candidate_id"]]
        self.assertEqual((inherited["last_action"], inherited["inherited_from"]), ("silent", old_id))


    def test_static_note_and_rule_digest_do_not_version_trend(self):
        base = {"ref_date": "2026-10-26", "state": "退潮", "arming": []}
        a = sources.candidate("trend_state", "2026-10-26", "2026-10-26", {**base, "note": "旧说明", "rule_texts": {"R-1": "x"}})
        b = sources.candidate("trend_state", "2026-10-26", "2026-10-26", {**base, "note": "新说明", "rule_texts": {"R-1": "y"}})
        c = sources.candidate("trend_state", "2026-10-26", "2026-10-26", {**base, "state": "修复"})
        d = sources.candidate("trend_state", "2026-10-26", "2026-10-26", {**base, "note": "旧说明", "plan_date": "2026-10-28"})
        self.assertEqual(a["candidate_id"], b["candidate_id"])
        self.assertEqual(a["candidate_id"], d["candidate_id"])
        self.assertNotEqual(a["candidate_id"], c["candidate_id"])


class ValidationRepairTest(RuntimeCase):
    def test_review_after_on_silent_is_ignored(self):
        self.init()
        item = self.add_event()
        self.scan()
        outcome = self.decide(item, "silent", review_after="2026-10-28T10:00:00+08:00")[0]
        self.assertEqual(outcome["outcome"], "silent")
        self.assertIn("review_after_ignored", outcome["repairs"])

    def test_defer_beyond_validity_is_clamped(self):
        self.init()
        item = self.add_event()
        item["valid_until"] = "2026-10-27T20:00:00+08:00"
        _, payload, _ = self.scan()
        outcome = self.decide(payload["candidates"][0], "defer", review_after="2026-10-29T10:00:00+08:00")[0]
        self.assertIn("defer_clamped_to_valid_until", outcome["repairs"])
        self.assertEqual(self.state()["candidates"][item["candidate_id"]]["review_after"], "2026-10-27T20:00:00+08:00")

    def test_unparseable_response_charges_the_inflight_batch(self):
        self.init()
        item = self.add_event()
        self.scan()
        state = self.state()
        state["signals"]["inflight"] = {"batch_id": "x", "created_at": NOW, "candidate_ids": [item["candidate_id"]], "status": "taken"}
        self.save(state)
        self.write_run("不是 JSON")
        self.dispatch()
        state = self.state()
        self.assertEqual(state["candidates"][item["candidate_id"]]["attempts"], 1)
        self.assertIsNone(state["signals"]["inflight"])


class ReceiptTest(RuntimeCase):
    def _db(self, rows):
        path = CTX.root.parent / "hermes" / "cron" / "executions.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE executions (job_id TEXT, status TEXT, delivery_outcome TEXT, error TEXT, started_at TEXT, finished_at TEXT)")
        conn.executemany("INSERT INTO executions VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def _attempt(self):
        self.init()
        item = self.add_event()
        self.scan()
        self.decide(item, "notify")
        self.dispatch()

    def test_delivered_receipt_is_recorded(self):
        self._attempt()
        self._db([(DELIVERY_JOB, "completed", "delivered", None, "2026-10-27T09:59:59.500000+08:00", "2026-10-27T10:00:01+08:00")])
        self.dispatch("2026-10-27T10:02:00+08:00", delivery_job_id=DELIVERY_JOB)
        intent = next(iter(self.state()["outbox"].values()))
        self.assertEqual((intent["status"], intent["receipt"]), ("delivered", "delivered"))

    def test_failed_and_missing_receipts(self):
        self._attempt()
        self._db([(DELIVERY_JOB, "failed", None, "gateway down", "2026-10-27T09:59:59+08:00", "2026-10-27T10:00:02+08:00")])
        self.dispatch("2026-10-27T10:02:00+08:00", delivery_job_id=DELIVERY_JOB)
        self.assertEqual(next(iter(self.state()["outbox"].values()))["status"], "failed")

    def test_no_record_becomes_unverified_after_grace(self):
        self._attempt()
        self._db([])
        self.dispatch("2026-10-27T10:30:00+08:00", delivery_job_id=DELIVERY_JOB)
        self.assertEqual(next(iter(self.state()["outbox"].values()))["status"], "attempted")
        self.dispatch("2026-10-27T12:30:00+08:00", delivery_job_id=DELIVERY_JOB)
        self.assertEqual(next(iter(self.state()["outbox"].values()))["status"], "unverified")


class OrderingAndCacheTest(RuntimeCase):
    def test_trend_comes_first_even_with_many_catalysts(self):
        self.init()
        for i in range(8):
            self.add_event(f"CE-{i}", observed_at=f"2026-10-2{i % 8}")
        self.trend = sources.candidate("trend_state", "2026-10-27", "2026-10-27", {"ref_date": "2026-10-27", "state": "修复"})
        _, payload, _ = self.scan()
        self.assertEqual(payload["candidates"][0]["kind"], "trend_state")
        self.assertEqual(len(payload["candidates"]), 6)

    def test_one_tick_collects_trend_once(self):
        self.init()
        item = self.add_event()
        self.scan()
        self.trend_calls = 0
        self.write_run({"decisions": [self.row(item, "notify")]}, name="r1.md")
        self.dispatch()
        self.assertEqual(self.trend_calls, 1)


class HealthAndCompactionTest(RuntimeCase):
    def test_unparseable_run_format_raises_local_health_alert(self):
        self.init()
        (self.output_dir / "odd.md").write_text("# Cron Job\n\n## Prompt\n\n…\n\n## Output\n\n{}\n", encoding="utf-8")
        self.dispatch()
        health = self.state()["health"]
        self.assertEqual(health[-1]["code"], "eval_run_unparseable")

    def test_terminal_candidates_move_to_index_and_stay_quiet(self):
        self.add_event("CE-old")
        self.init()
        baseline = len(self.state()["candidates"])          # 事件、趋势与 08:35 窗口
        self.call("compact", "2026-10-31T10:00:00+08:00")
        state = self.state()
        self.assertEqual(state["candidates"], {})
        self.assertEqual(len(state["_index"]), baseline)
        _, payload, gate = self.scan("2026-10-31T10:00:00+08:00")
        self.assertFalse(gate["wakeAgent"])
        self.assertTrue(CTX.index_path.is_file())

    def test_compaction_keeps_intents_until_settled(self):
        self.init()
        item = self.add_event()
        self.scan()
        self.decide(item, "notify")
        self.dispatch()
        stats = store.compact(self.state(), parse_time("2026-11-30T10:00:00+08:00"))
        self.assertEqual(stats["intents_archived"], 0)


class RuleDigestTest(RuntimeCase):
    def test_digest_keeps_actionable_sections_only(self):
        path = CTX.root / CTX.rules_dir / "R-016-缩量退潮降仓.md"
        path.write_text("---\nstatus: 修订中\n---\n# R-016 缩量退潮降仓\n\n## 规则陈述\n破位即降仓\n\n## 演进历史\n很长的历史" * 1, encoding="utf-8")
        text = trend.rule_digest(path)
        self.assertIn("破位即降仓", text)
        self.assertIn("状态：修订中", text)
        self.assertNotIn("很长的历史", text)
        self.assertEqual(render.build_glossary()["R-016"], "缩量退潮降仓")


class PulseTest(RuntimeCase):
    def test_file_change_waits_for_debounce_then_stages_one_batch(self):
        self.init()
        CTX.events.write_text("{}\n", encoding="utf-8")
        self.pulse("2026-10-27T10:00:00+08:00")                     # 建立水位（同时心跳扫描）
        self.add_event()
        CTX.events.write_text('{"event_id": "x"}\n', encoding="utf-8")
        self.pulse("2026-10-27T10:02:00+08:00")
        self.assertIn("catalyst", self.state()["signals"]["pending"])
        self.assertIsNone(self.state()["signals"]["inflight"])
        self.pulse("2026-10-27T10:06:00+08:00")                     # 安静 4 分钟 > 180 秒
        state = self.state()
        self.assertEqual(state["signals"]["inflight"]["status"], "ready")
        pulse.trigger_evaluation.assert_called_with(JOB)
        _, payload, gate = self.gate("2026-10-27T10:06:30+08:00")
        self.assertTrue(gate["wakeAgent"])
        self.assertEqual(payload["candidates"][0]["native_id"], "CE-20261027-test")
        _, _, gate = self.gate("2026-10-27T10:07:00+08:00")
        self.assertFalse(gate["wakeAgent"])

    def test_batch_in_flight_blocks_new_scans_until_it_returns(self):
        self.init()
        self.add_event()
        self.pulse("2026-10-27T10:31:00+08:00")                     # 心跳到期触发首次扫描
        inflight = self.state()["signals"]["inflight"]
        self.assertIsNotNone(inflight)
        self.pulse("2026-10-27T10:45:00+08:00")                     # 在途：不重复备批
        self.assertEqual(self.state()["signals"]["inflight"]["batch_id"], inflight["batch_id"])
        self.pulse("2026-10-27T11:05:00+08:00")                     # 超过 30 分钟租约
        state = self.state()
        self.assertIn("evaluation_timeout", [row["code"] for row in state["health"]])
        cid = inflight["candidate_ids"][0]
        self.assertEqual(state["candidates"][cid]["attempts"], 1)

    def test_night_data_is_evaluated_on_arrival_but_delivered_after_quiet_hours(self):
        self.init("2026-10-27T22:00:00+08:00")
        CTX.events.write_text("{}\n", encoding="utf-8")
        self.pulse("2026-10-28T00:00:00+08:00")                     # 建立水位
        item = self.add_event(observed_at="2026-10-28")
        CTX.events.write_text('{"event_id": "night"}\n', encoding="utf-8")   # 00:10 台账写入
        self.pulse("2026-10-28T00:12:00+08:00")
        self.pulse("2026-10-28T00:16:00+08:00")                     # 防抖结束 → 当场备批并触发评估
        self.assertEqual(self.state()["signals"]["inflight"]["status"], "ready")
        pulse.trigger_evaluation.assert_called_with(JOB)
        _, payload, gate = self.gate("2026-10-28T00:17:00+08:00")
        self.assertTrue(gate["wakeAgent"])
        self.write_run({"decisions": [self.row(payload["candidates"][0], "notify", "夜间到达的提醒")]}, name="night.md")
        _, message, _ = self.pulse("2026-10-28T00:20:00+08:00")     # 评估结果夜里就入账
        self.assertIsNone(message)
        intent = next(iter(self.state()["outbox"].values()))
        self.assertEqual((intent["status"], intent["not_before"]), ("pending", "2026-10-28T06:00:00+08:00"))
        _, message, _ = self.pulse("2026-10-28T06:00:00+08:00")
        self.assertEqual(message, "夜间到达的提醒")

    def test_rate_limit_records_health_instead_of_scanning(self):
        self.init()
        state = self.state()
        state["signals"]["evaluations"] = [iso(parse_time(NOW) - timedelta(minutes=m)) for m in (1, 2, 3, 4)]
        self.save(state)
        self.add_event()
        self.pulse("2026-10-27T10:31:00+08:00")
        self.assertEqual(self.state()["health"][-1]["code"], "evaluation_rate_limited")
        pulse.trigger_evaluation.assert_not_called()

    def test_skill_signal_marks_its_sources(self):
        self.init()
        self.pulse("2026-10-27T10:00:00+08:00")
        self.call("signal", "2026-10-27T10:01:00+08:00", source="skill:a-stock-daily-market-sense", ref="r.md", reason=None)
        state = self.state()
        pulse.detect(state, parse_time("2026-10-27T10:01:30+08:00"))
        self.assertIn("trend_state", state["signals"]["pending"])

    def test_pulse_delivers_due_intent(self):
        self.init()
        item = self.add_event()
        self.scan()
        self.decide(item, "notify")
        _, message, _ = self.pulse("2026-10-27T10:02:00+08:00")
        self.assertEqual(message, "合格的真实提醒")


class RenderTest(RuntimeCase):
    def test_brief_renders_with_code_controlled_label_and_date(self):
        text = render.render_brief("catalyst", "notify", "P2", brief(delta="首次"), "2026-10-26")
        lines = text.splitlines()
        self.assertEqual(lines[0], "【P2 基本面】框架该体检了")
        self.assertEqual(lines[-1], "来源：供需平衡框架体检约定 · 事件日 10-26")

    def test_needs_user_requires_ask(self):
        with self.assertRaises(render.BriefError):
            render.render_brief("framework_review", "needs_user", "P3", brief(), "2026-10-26")

    def test_scrub_translates_codes_and_drops_disclaimers(self):
        text, notes = render.scrub("节点S2-hbm继续收紧。仅提醒复核，不构成买卖指令。见 CE-20261026。",
                                   {"S2-hbm": "HBM/存储"})
        self.assertIn("HBM/存储", text)
        self.assertNotIn("不构成", text)
        self.assertIn("residual:event_id", notes)

    def test_brief_decision_produces_rendered_intent(self):
        self.init()
        item = self.add_event()
        self.scan()
        self.decide_rows([self.row(item, "notify", brief=brief(headline="燃机订单价上修"), priority="P2")])
        message = next(iter(self.state()["outbox"].values()))["message"]
        self.assertTrue(message.startswith("【P2 基本面】燃机订单价上修"))
        self.assertTrue(message.endswith("事件日 10-27"))


class ArrivalTimingTest(RuntimeCase):
    def test_trend_tool_is_asked_for_tomorrows_plan(self):
        self.patches[0].stop()
        try:
            with patch.object(trend.subprocess, "run") as run:
                run.return_value.returncode = 1
                run.return_value.stderr = "x"
                trend.trading_candidate(parse_time("2026-11-09T17:05:00+08:00"), {"max_trend_age_days": 3})
            argv = run.call_args[0][0]
            self.assertEqual(argv[argv.index("--date") + 1], "2026-11-10")
        finally:
            self.patches[0].start()

    def test_v3_trend_differing_only_in_plan_date_is_inherited(self):
        self.init()
        evidence = {"ref_date": "2026-11-06", "state": "退潮", "arming": [], "plan_date": "2026-11-10"}
        self.trend = sources.candidate("trend_state", "2026-11-06", "2026-11-06", evidence)
        old_evidence = dict(evidence, plan_date="2026-11-09")
        old_id = "PG-" + digest({"kind": "trend_state", "native_id": "2026-11-06",
                                 "evidence": {k: v for k, v in old_evidence.items()}})[:16]
        CTX.packets.mkdir(parents=True, exist_ok=True)
        (CTX.packets / f"{old_id}.json").write_text(json.dumps({"candidate_id": old_id, "evidence": old_evidence}), encoding="utf-8")
        state = self.state()
        state["_index"][old_id] = {"native_id": "2026-11-06", "kind": "trend_state", "last_action": "notify",
                                   "packet_path": f"packets/{old_id}.json", "fingerprint_version": 3}
        self.save(state)
        _, payload, _gate = self.scan("2026-11-09T12:00:00+08:00")
        # 当天 08:35 固定窗口会另成候选；这里只关心趋势没有被重新呈现
        self.assertNotIn("trend_state", [row["kind"] for row in payload["candidates"]])
        self.assertEqual(self.state()["candidates"][self.trend["candidate_id"]]["last_action"], "notify")

    def test_trigger_does_not_wait_for_the_evaluation(self):
        self.patches[4].stop()
        try:
            with patch.object(pulse.subprocess, "Popen") as popen:
                popen.return_value.pid = 4242
                ok, detail = pulse.trigger_evaluation(JOB)
            self.assertTrue(ok)
            self.assertIn("4242", detail)
            kwargs = popen.call_args[1]
            self.assertTrue(kwargs["start_new_session"])
            popen.return_value.wait.assert_not_called()
            self.assertTrue((CTX.runtime / "logs" / "trigger.log").is_file())
        finally:
            self.patches[4].start()


if __name__ == "__main__":
    unittest.main()
