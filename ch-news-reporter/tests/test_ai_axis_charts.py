from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT.parent / "shared"))

import ai_axis_charts as ax  # noqa: E402


def _vector(vid: str, stage: str, **extra) -> dict:
    return {"id": vid, "vector": vid, "stage": stage, "open_pain": "", **extra}


def _form(fid: str, pen: str, **extra) -> dict:
    return {"id": fid, "form": fid, "penetration_stage": pen, **extra}


def _watchboard(vectors, forms, **extra) -> dict:
    return {"as_of": "2026-08-01", "frame": {"agent_eng_vectors": vectors, "product_forms": forms}, **extra}


class StageParsingTests(unittest.TestCase):
    def test_qualified_labels_still_resolve(self) -> None:
        """The model writes 「主流化前段」/「收敛中(事实标准候选)」, not bare enum values."""
        self.assertEqual(ax._ordinal("主流化前段", ax.PEN_ORDINALS), 3)
        self.assertEqual(ax._ordinal("收敛中(事实标准候选)", ax.STAGE_ORDINALS), 2)
        self.assertEqual(ax._ordinal("实验", ax.STAGE_ORDINALS), 1)
        self.assertIsNone(ax._ordinal("莫名其妙", ax.STAGE_ORDINALS))
        self.assertIsNone(ax._ordinal(None, ax.STAGE_ORDINALS))

    def test_missing_completion_sits_mid_band(self) -> None:
        self.assertEqual(ax._score(2, None, 3), 2.5)
        self.assertLess(ax._score(2, 0, 3), 2.5)
        self.assertGreater(ax._score(2, 3, 3), 2.5)

    def test_truthy_count_ignores_non_true_values(self) -> None:
        self.assertEqual(ax._truthy_count({"a": True, "b": False, "c": None}, 3), 1)
        self.assertEqual(ax._truthy_count([True, True], 5), 2)
        self.assertIsNone(ax._truthy_count(None, 3))


class ObjectParsingTests(unittest.TestCase):
    def test_axis_objects_and_grading_audit_merge_and_dedupe(self) -> None:
        wb = _watchboard(
            [_vector("v-a", "收敛中")], [_form("f-a", "早期采用")],
            axis_objects=[{"name": "DeepSeek V4-Flash", "grade": "s_confirmed",
                           "vector_id": "v-a", "form_id": "f-a"},
                          {"name": "某 A 级动作", "grade": "a", "vector_id": "v-a", "form_id": "f-a"}],
            grading_audit=[{"entity": "DeepSeek V4-Flash", "final_grade": "s_confirmed",
                            "axis_hit": {"vector_id": "v-a", "form_id": "f-a"}},
                           {"entity": "MCP 无状态规范", "final_grade": "s_candidate",
                            "axis_hit": {"vector_id": "v-a", "form_id": "f-a"}}],
        )
        objects = ax.parse_objects(wb)
        self.assertEqual([o["name"] for o in objects],
                         ["DeepSeek V4-Flash", "MCP 无状态规范", "某 A 级动作"])
        self.assertEqual(objects[0]["grade"], "S+")
        self.assertEqual(objects[1]["grade"], "S?")

    def test_audit_rows_without_axis_hit_are_skipped(self) -> None:
        wb = _watchboard([_vector("v-a", "收敛中")], [],
                         grading_audit=[{"entity": "无落点", "final_grade": "a"}])
        self.assertEqual(ax.parse_objects(wb), [])


class HistoryTests(unittest.TestCase):
    def _history(self):
        day = lambda vs, fs: {"frame": {"agent_eng_vectors": vs, "product_forms": fs}}
        return [
            ("2026-07-30", day([_vector("v-a", "实验")], [_form("f-a", "早期采用")])),
            ("2026-07-31", day([_vector("v-a", "收敛中")], [])),
            ("2026-08-01", day([_vector("v-a", "收敛中")], [])),
        ]

    def test_series_marks_promotion_and_silent_drop(self) -> None:
        series = ax.stage_series(self._history())
        self.assertEqual(series["vectors"]["v-a"], [1, 2, 2])
        # the form vanished after day 1 without settling — that must stay visible
        self.assertEqual(series["forms"]["f-a"], [2, "gone", "gone"])

    def test_days_at_stage_flags_window_truncation(self) -> None:
        self.assertEqual(ax.days_at_stage([1, 2, 2]), (2, False))
        self.assertEqual(ax.days_at_stage([2, 2, 2]), (3, True))
        self.assertIsNone(ax.days_at_stage(["gone"]))

    def test_promotion_becomes_a_move_and_a_caption(self) -> None:
        history = self._history()
        figures = ax.build_figures(_watchboard([_vector("v-a", "收敛中")], []), history)
        self.assertEqual(len(figures), 2)
        self.assertIn("档位位移 1 次", figures[0].caption)
        self.assertIn("07-31", figures[0].caption)
        self.assertIn("没结算就从 frame 里消失", figures[1].caption)


class DegradationTests(unittest.TestCase):
    def test_no_history_still_draws_the_map_only(self) -> None:
        figures = ax.build_figures(_watchboard([_vector("v-a", "收敛中")], [_form("f-a", "萌芽")]))
        self.assertEqual(len(figures), 1)
        self.assertIn("<svg", figures[0].html)
        self.assertIn("无历史可比", figures[0].caption)

    def test_empty_or_unusable_frame_draws_nothing(self) -> None:
        self.assertEqual(ax.build_figures({}), [])
        self.assertEqual(ax.build_figures(_watchboard([_vector("v-a", "没有这个档位")], [])), [])

    def test_unlinked_vectors_stay_inside_the_lane(self) -> None:
        """Regression: clamping unlinked points to the plot area used to push
        them out of the 未挂钩 lane and collapse them onto each other."""
        vectors = [_vector(f"v-{i}", "收敛中") for i in range(7)]
        figures = ax.build_figures(_watchboard(vectors, [_form("f-a", "早期采用")]))
        lane_right = ax.L + ax._lane_width(len(vectors))[0]
        xs = [float(m) for m in re.findall(r'<circle cx="([\d.]+)"', figures[0].html)]
        self.assertEqual(len(xs), len(vectors))
        self.assertTrue(all(ax.L <= x <= lane_right for x in xs), xs)

    def test_links_are_read_from_either_side(self) -> None:
        """The coupling is one relationship; the model may record it on the form."""
        from_vector = _watchboard([_vector("v-a", "收敛中", links=["f-a"])], [_form("f-a", "主流化")])
        from_form = _watchboard([_vector("v-a", "收敛中")], [_form("f-a", "主流化", links=["v-a"])])
        xs = []
        for wb in (from_vector, from_form):
            html = ax.build_figures(wb)[0].html
            xs.append(float(re.search(r'<circle cx="([\d.]+)"', html).group(1)))
        self.assertEqual(xs[0], xs[1])
        self.assertGreater(xs[0], ax.L + 100)  # inside the plot, not in the 未挂钩 lane

    def test_same_cell_points_do_not_stack(self) -> None:
        vectors = [_vector(f"v-{i}", "收敛中", links=["f-a"]) for i in range(4)]
        figures = ax.build_figures(_watchboard(vectors, [_form("f-a", "早期采用")]))
        pts = [(float(x), float(y)) for x, y in
               re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', figures[0].html)]
        self.assertEqual(len(pts), 4)
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                gap = ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2) ** 0.5
                self.assertGreaterEqual(round(gap, 1), 20.0, f"{pts[i]} vs {pts[j]}")

    def test_figures_carry_no_script_tags(self) -> None:
        figures = ax.build_figures(_watchboard([_vector("v-a", "收敛中")], [_form("f-a", "萌芽")]))
        self.assertNotIn("<script", figures[0].html)


if __name__ == "__main__":
    unittest.main()
