from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html_report.builder import HtmlReportBuilder  # noqa: E402
from html_report.figures import StaticFigure, insert_figures  # noqa: E402


BODY = (
    "<h2>报告标题</h2>"
    "<h3>一句话结论</h3><p>结论正文</p>"
    "<h3>纵轴</h3><p>矢量</p>"
    "<h3>待跟踪 &amp; 本期变更</h3><table></table>"
    "<h4>🔄 本期变更(框架 × 跟踪合并)</h4><ul><li>顺延</li></ul>"
)
FIG = "<svg></svg>"


class FigurePlacementTests(unittest.TestCase):
    def test_section_end_lands_before_next_same_level_heading(self) -> None:
        out = insert_figures(BODY, [StaticFigure(html=FIG, anchor=("一句话结论",))])
        self.assertLess(out.index("结论正文"), out.index("report-figure"))
        self.assertLess(out.index("report-figure"), out.index("<h3>纵轴</h3>"))

    def test_before_placement(self) -> None:
        out = insert_figures(BODY, [StaticFigure(html=FIG, anchor=("纵轴",), placement="before")])
        self.assertLess(out.index("report-figure"), out.index("<h3>纵轴</h3>"))

    def test_after_placement(self) -> None:
        out = insert_figures(BODY, [StaticFigure(html=FIG, anchor=("纵轴",), placement="after")])
        self.assertLess(out.index("<h3>纵轴</h3>"), out.index("report-figure"))
        self.assertLess(out.index("report-figure"), out.index("矢量"))

    def test_match_last_targets_the_inner_heading(self) -> None:
        """「待跟踪 & 本期变更」 also contains 「本期变更」; match=last picks the subsection."""
        first = insert_figures(
            BODY, [StaticFigure(html=FIG, anchor=("本期变更",), placement="before")]
        )
        self.assertLess(first.index("report-figure"), first.index("待跟踪"))
        last = insert_figures(
            BODY,
            [StaticFigure(html=FIG, anchor=("本期变更",), placement="before", match="last")],
        )
        self.assertLess(last.index("待跟踪"), last.index("report-figure"))
        self.assertLess(last.index("report-figure"), last.index("🔄"))

    def test_missing_anchor_appends_rather_than_drops(self) -> None:
        out = insert_figures(BODY, [StaticFigure(html=FIG, anchor=("没有这个标题",))])
        self.assertIn("report-figure", out)
        self.assertGreater(out.index("report-figure"), out.index("🔄"))

    def test_anchor_alternatives_fall_through_in_order(self) -> None:
        out = insert_figures(
            BODY, [StaticFigure(html=FIG, anchor=("不存在", "纵轴"), placement="before")]
        )
        self.assertLess(out.index("report-figure"), out.index("<h3>纵轴</h3>"))

    def test_rejects_unknown_placement_and_match(self) -> None:
        with self.assertRaises(ValueError):
            StaticFigure(html=FIG, placement="sideways")
        with self.assertRaises(ValueError):
            StaticFigure(html=FIG, match="middle")


class BuilderIntegrationTests(unittest.TestCase):
    def test_figure_markup_and_css_render_without_scripts(self) -> None:
        builder = HtmlReportBuilder(title="t", theme="claude")
        builder.add_figure(
            StaticFigure(html=FIG, anchor=("一句话结论",), title="双轴定位图", caption="说明")
        )
        out = builder.render("# t\n\n## 一句话结论\n\n结论正文\n\n## 纵轴\n\n矢量\n")
        self.assertIn("report-figure", out)
        self.assertIn("双轴定位图", out)
        self.assertIn(".rf-card", out)
        body = out[out.index('id="report-body"'):out.index("</section>")]
        self.assertNotIn("<script", body)  # the figure must not need JS to appear

    def test_no_figures_adds_no_css(self) -> None:
        builder = HtmlReportBuilder(title="t", theme="claude")
        out = builder.render("# t\n\n## 一句话结论\n\n结论正文\n")
        self.assertNotIn(".rf-card", out)


if __name__ == "__main__":
    unittest.main()
