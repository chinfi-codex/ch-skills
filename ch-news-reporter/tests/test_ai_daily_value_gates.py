from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from prepare_report_data import (  # noqa: E402
    build_enrichment_candidates,
    is_content_truncated,
    select_report_items,
)
from save_report_state import validate  # noqa: E402


class SourceAwareSelectionTests(unittest.TestCase):
    def test_selection_preserves_sources_and_enforces_hard_share(self) -> None:
        sources = [
            "product_hunt",
            "hacker_news",
            "github_trending",
            "rss",
            "jin10",
        ]
        items = []
        for source in sources:
            count = 20 if source == "jin10" else 3
            for idx in range(count):
                items.append(
                    {
                        "id": f"{source}-{idx}",
                        "source_type": source,
                        "published_at": f"2026-07-17T{23 - min(idx, 23):02d}:00:00+08:00",
                        "metadata": {
                            "daily_rank": idx + 1,
                            "score": 100 - idx,
                            "stars": 1000 - idx,
                        },
                    }
                )
        profile = {
            "sources": sources,
            "source_selection": {
                "preferred_limits": {source: 1 for source in sources},
                "hard_max_share": 0.4,
            },
        }

        selected = select_report_items(items, profile, limit=10)

        self.assertEqual(len(selected), 10)
        counts = {
            source: sum(1 for item in selected if item["source_type"] == source)
            for source in sources
        }
        self.assertTrue(all(counts[source] >= 1 for source in sources))
        self.assertLessEqual(max(counts.values()), 4)


class EnrichmentCandidateTests(unittest.TestCase):
    def test_truncation_detector_handles_feed_ellipsis_only_at_the_end(self) -> None:
        self.assertTrue(is_content_truncated("正文尚未结束……  "))
        self.assertTrue(is_content_truncated("正文尚未结束...\n"))
        self.assertFalse(is_content_truncated("正文中间有…但结尾完整。"))
        self.assertFalse(is_content_truncated(""))

    def test_truncated_rss_item_is_marked_for_full_article_fetch(self) -> None:
        candidates = build_enrichment_candidates(
            [
                {
                    "id": "rss-1",
                    "source_type": "rss",
                    "title": "被截断的 RSS 摘要",
                    "url": "https://example.com/full-story",
                    "content": "只拿到了摘要…",
                }
            ]
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["target_type"], "article_url")
        self.assertTrue(candidates[0]["truncated"])
        self.assertIn("fetch full article", candidates[0]["reason"])


class GradingAuditValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = {
            "title": "AI 日报",
            "state_enabled": True,
            "value_grading": {
                "audit_required": True,
                "candidate_budget": 2,
                "deep_url_min": 3,
                "deep_url_max": 6,
            },
        }
        self.payload = {
            "as_of": "2026-07-17",
            "regime": "agent 主形态",
            "tracking_items": [],
            "next_nodes": [],
            "falsifiers": ["若模型并未真实开放，则当前判断下调"],
            "grading_audit": [],
        }

    @staticmethod
    def candidate() -> dict:
        return {
            "entity_id": "kimi-k3",
            "entity": "Kimi K3",
            "provisional_grade": "s_candidate",
            "final_grade": "s_candidate",
            "trigger_conditions": ["重点厂商旗舰模型，影响能力与开放性"],
            "candidate_exclusions": [],
            "evidence_sources": [
                {
                    "source_family": "rss",
                    "type": "official",
                    "url": "https://example.com/kimi-k3",
                }
            ],
            "confirmation_blockers": ["权重尚未真实开放"],
            "evidence_gaps": ["缺少独立复现"],
            "deep_enrichment": {
                "status": "partial",
                "target_urls": ["https://example.com/kimi-k3"],
                "note": "首发日尚无可复现权重",
            },
            "rationale": "保留 A+ 候选，等待可用性与独立复核",
        }

    def run_validation(self, payload: dict) -> list[str]:
        errors, _warnings = validate(payload, self.profile, prior=None)
        return errors

    def test_empty_audit_is_valid(self) -> None:
        self.assertEqual(self.run_validation(copy.deepcopy(self.payload)), [])

    def test_candidate_partial_enrichment_is_valid_with_explicit_blocker(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["grading_audit"] = [self.candidate()]
        self.assertEqual(self.run_validation(payload), [])

    def test_missing_audit_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload.pop("grading_audit")
        errors = self.run_validation(payload)
        self.assertTrue(any("missing 'grading_audit'" in error for error in errors))

    def test_confirmed_requires_complete_official_and_independent_evidence(self) -> None:
        payload = copy.deepcopy(self.payload)
        candidate = self.candidate()
        candidate["final_grade"] = "s_confirmed"
        payload["grading_audit"] = [candidate]

        errors = self.run_validation(payload)

        self.assertTrue(any("confirmation_blockers" in error for error in errors))
        self.assertTrue(any("not complete" in error for error in errors))
        self.assertTrue(any("official and independent" in error for error in errors))

    def test_confirmed_is_valid_after_all_confirmation_gates(self) -> None:
        payload = copy.deepcopy(self.payload)
        candidate = self.candidate()
        candidate.update(
            {
                "final_grade": "s_confirmed",
                "confirmation_blockers": [],
                "evidence_gaps": [],
                "evidence_sources": [
                    {
                        "source_family": "official_blog",
                        "type": "official",
                        "url": "https://example.com/official",
                    },
                    {
                        "source_family": "independent_eval",
                        "type": "independent",
                        "url": "https://example.com/eval",
                    },
                ],
                "deep_enrichment": {
                    "status": "complete",
                    "target_urls": [
                        "https://example.com/official",
                        "https://example.com/docs",
                        "https://example.com/eval",
                    ],
                    "note": "四项确认门槛均满足",
                },
                "rationale": "一手、可用性与独立复核齐备，升级为 S-confirmed",
            }
        )
        payload["grading_audit"] = [candidate]
        self.assertEqual(self.run_validation(payload), [])

    def test_candidate_budget_is_hard_limit(self) -> None:
        payload = copy.deepcopy(self.payload)
        audit = []
        for idx in range(3):
            candidate = self.candidate()
            candidate["entity_id"] = f"candidate-{idx}"
            candidate["entity"] = f"Candidate {idx}"
            audit.append(candidate)
        payload["grading_audit"] = audit

        errors = self.run_validation(payload)

        self.assertTrue(any("exceeding candidate budget 2" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
