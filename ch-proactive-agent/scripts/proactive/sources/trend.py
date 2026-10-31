"""趋势状态：调用金库原子工具 trend_state.py，并附上相关 R-XXX 的可执行部分。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ..context import CTX, short

# 规则正文只注入能决定动作的部分；“演进历史”等留在金库，不进评估 prompt。
RULE_SECTIONS = ("规则陈述", "适用环境", "执行细节", "例外")
RULE_DIGEST_LIMIT = 1200


def rule_digest(path: Path, limit: int = RULE_DIGEST_LIMIT) -> str:
    text = path.read_text(encoding="utf-8-sig")
    status = ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            front, body = text[3:end], text[end + 4:]
            match = re.search(r"(?m)^status:\s*(.+)$", front)
            status = match.group(1).strip() if match else ""
    title_match = re.search(r"(?m)^# (.+)$", body)
    parts = [title_match.group(1).strip() if title_match else path.stem]
    if status:
        parts.append(f"状态：{status}")
    for block in re.split(r"(?m)^(?=## )", body):
        heading = block.splitlines()[0][3:].strip() if block.startswith("## ") else ""
        if heading and heading.startswith(RULE_SECTIONS):
            parts.append(block.strip())
    return short("\n\n".join(parts), limit)


def rule_files() -> dict[str, Path]:
    result = {}
    for path in sorted((CTX.root / CTX.rules_dir).glob("R-*.md")):
        match = re.match(r"(R-\d{3})", path.name)
        if match:
            result[match.group(1)] = path
    return result


def trading_candidate(now: datetime, policy: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    from . import candidate

    proc = subprocess.run(
        [sys.executable, str(CTX.trend_tool), "--date", now.date().isoformat(), "--json"],
        cwd=CTX.root, text=True, capture_output=True, timeout=60, check=False,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if proc.returncode:
        return None, [f"trend_state 读取失败：{short(proc.stderr, 400)}"]
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, [f"trend_state 返回非法 JSON：{exc}"]
    ref_date = datetime.strptime(data["ref_date"], "%Y-%m-%d").date()
    age = (now.date() - ref_date).days
    if age < 0 or age > int(policy["max_trend_age_days"]):
        return None, [f"trend_state 数据过期：ref_date={ref_date}"]
    files = rule_files()
    rule_texts: dict[str, str] = {}
    for row in data.get("arming") or []:
        for rule_id in re.findall(r"R-\d{3}", str(row.get("rule") or "")):
            if rule_id in files and rule_id not in rule_texts:
                rule_texts[rule_id] = rule_digest(files[rule_id])
    previous = (data.get("history") or [])[-2:-1]
    evidence = {
        "plan_date": data["plan_date"], "ref_date": data["ref_date"], "state": data["state"],
        "phase": data.get("phase"), "position": data.get("position"), "counters": data.get("counters"),
        "ref_day": data.get("ref_day"), "phase_score": data.get("phase_score"),
        "state_note": data.get("state_note"), "exit_progress": data.get("exit_progress"),
        "arming": data.get("arming"), "previous_state": previous[0] if previous else None,
        "rule_texts": rule_texts,
        "note": "既定 trend_state 和 R-XXX 可执行部分；集中度轴、领导股、盘中承接和实际持仓另行核验。",
    }
    # 指纹不含规则摘要（见 default_fingerprint）：规则正文变化由 framework_change 负责。
    return candidate("trend_state", ref_date.isoformat(), ref_date.isoformat(), evidence), []
