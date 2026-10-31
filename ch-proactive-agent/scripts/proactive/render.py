"""判断与排版分离：模型给 brief 内容字段，标签、日期与版式由这里确定性生成。

术语表把内部编码（R-XXX、产业链节点、跟踪变量）翻成人话；
发送前清理节点代号与免责套话，残留的内部标识只记 warning，不拦截提醒。
"""

from __future__ import annotations

import re
from typing import Any

from proactive_config import read_json

from .context import CTX
from .sources import cached

LABELS = {"trend_state": "交易", "scheduled_review": "交易", "catalyst": "基本面",
          "framework_review": "框架", "framework_change": "框架"}
BRIEF_REQUIRED = ("headline", "what_changed", "so_what", "falsifier", "source")
HEADLINE_LIMIT = 40
FIELD_LIMIT = 260
DISCLAIMER = re.compile(r"(不构成[^。；\n]{0,12}(指令|建议)|仅(作|为)?提醒复核|无实际持仓(数据|真相源)?)")
# 中文在 Unicode 正则里算 \w，紧贴中文的编码用 \b 匹配不到，所以统一用 ASCII 边界。
_L = r"(?<![A-Za-z0-9_-])"
_R = r"(?![A-Za-z0-9_-])"
RESIDUAL_PATTERNS = {
    "event_id": re.compile(_L + r"CE-\d{8}"),
    "candidate_id": re.compile(_L + r"PG-[0-9a-f]{16}" + _R),
    "file_name": re.compile(r"[A-Za-z0-9_-]+\.jsonl?" + _R),
    "node_code": re.compile(_L + r"[A-Z]{1,2}\d-[a-z][a-z0-9-]*" + _R),
}


class BriefError(ValueError):
    pass


def build_glossary() -> dict[str, str]:
    from .sources.trend import rule_files

    terms: dict[str, str] = {}
    for rule_id, path in rule_files().items():
        terms[rule_id] = path.stem[len(rule_id) + 1:] or rule_id
    try:
        board = read_json(CTX.board)
    except (OSError, ValueError):
        board = {}
    for node_id, node in (board.get("nodes") or {}).items():
        if isinstance(node, dict) and node.get("name"):
            terms[node_id] = str(node["name"])
    for row in board.get("tracking_vars") or []:
        if isinstance(row, dict) and row.get("id") and row.get("name"):
            terms[str(row["id"])] = str(row["name"])
    return terms


def glossary() -> dict[str, str]:
    return cached(("glossary",), build_glossary)


def glossary_for(text: str) -> dict[str, str]:
    """只注入候选里真正出现过的术语，避免评估包膨胀。"""
    return {code: name for code, name in glossary().items() if code in text}


def scrub(text: str, terms: dict[str, str] | None = None) -> tuple[str, list[str]]:
    terms = glossary() if terms is None else terms
    notes: list[str] = []
    for code, name in sorted(terms.items(), key=lambda item: -len(item[0])):
        if code.startswith("R-"):
            continue  # 规则编号允许出现，由 Prompt 要求“编号 + 名称”
        pattern = re.compile(_L + re.escape(code) + _R)
        if pattern.search(text):
            text = pattern.sub(name, text)
            notes.append(f"replaced:{code}")
    sentences = re.split(r"(?<=[。；])", text)
    kept = [s for s in sentences if not (DISCLAIMER.search(s) and len(s.strip()) <= 40)]
    if len(kept) != len(sentences):
        notes.append("disclaimer_removed")
        text = "".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    for name, pattern in RESIDUAL_PATTERNS.items():
        if pattern.search(text):
            notes.append(f"residual:{name}")
    return text, notes


def _clip(value: Any, limit: int = FIELD_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _mmdd(value: Any) -> str:
    text = str(value or "")
    return text[5:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else text


def render_brief(kind: str, action: str, priority: str | None, brief: Any, observed_at: Any) -> str:
    if not isinstance(brief, dict):
        raise BriefError("brief 必须是对象")
    missing = [key for key in BRIEF_REQUIRED if not str(brief.get(key) or "").strip()]
    if action == "needs_user" and not str(brief.get("ask") or "").strip():
        missing.append("ask")
    if missing:
        raise BriefError("brief 缺少字段：" + "、".join(missing))
    label = "需你决定" if action == "needs_user" else LABELS.get(kind, "提醒")
    lines = [f"【{priority or 'P2'} {label}】{_clip(brief['headline'], HEADLINE_LIMIT)}",
             f"发生了什么：{_clip(brief['what_changed'])}"]
    if str(brief.get("delta") or "").strip():
        lines.append(f"变化在哪：{_clip(brief['delta'])}")
    lines.append(f"对你：{_clip(brief['so_what'])}")
    if str(brief.get("missing") or "").strip():
        lines.append(f"还缺：{_clip(brief['missing'], 160)}")
    lines.append(f"失效：{_clip(brief['falsifier'])}")
    if action == "needs_user":
        lines.append(f"请回复：{_clip(brief['ask'], 160)}")
    date_label = "事件日" if kind in {"catalyst", "trend_state", "scheduled_review"} else "日期"
    lines.append(f"来源：{_clip(brief['source'], 80)} · {date_label} {_mmdd(observed_at)}")
    return "\n".join(lines)
