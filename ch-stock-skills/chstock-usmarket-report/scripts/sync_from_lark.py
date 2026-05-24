#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read the Lark spreadsheet referenced in stock_pool.yaml and rewrite the yaml.

The spreadsheet has three sub-sheets:
  - watchlist:  类型 | 分组 | 代码 | 备注      (rows are flat: type=index|stock)
  - groups:     分组 | emoji | 描述           (group metadata)
  - thresholds: key  | value                   (rise/drop thresholds)

Network access is delegated to `lark-cli sheets +read`. The script does not
make domain judgments — it only transcribes sheet rows to yaml.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "assets" / "stock_pool.yaml"


def _lark_bin() -> str:
    path = shutil.which("lark-cli")
    if not path:
        raise RuntimeError("lark-cli not found in PATH; install it or run with --no-sync.")
    return path


def fetch_sheet_id_map(spreadsheet_token: str) -> Dict[str, str]:
    """Return {sheet_title: sheet_id} for all sub-sheets in the spreadsheet."""
    cmd = [
        _lark_bin(),
        "sheets",
        "+info",
        "--spreadsheet-token",
        spreadsheet_token,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli +info failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"lark-cli +info not-ok: {payload}")
    sheets = (
        payload.get("data", {})
        .get("sheets", {})
        .get("sheets", [])
    )
    return {sheet.get("title", ""): sheet.get("sheet_id", "") for sheet in sheets if sheet.get("sheet_id")}


def run_lark_read(spreadsheet_token: str, sheet_id: str) -> List[List[Any]]:
    """Read a full sheet by id via `lark-cli sheets +read --range <sheetId>!A:Z`."""
    cmd = [
        _lark_bin(),
        "sheets",
        "+read",
        "--spreadsheet-token",
        spreadsheet_token,
        "--range",
        f"{sheet_id}!A:Z",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli read failed for {sheet_id}: {result.stderr.strip()}")

    payload = json.loads(result.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"lark-cli returned not-ok for {sheet_id}: {payload}")

    values = (
        payload.get("data", {})
        .get("valueRange", {})
        .get("values")
        or payload.get("data", {}).get("values")
        or []
    )
    return values


def resolve_sheet_id(title_map: Dict[str, str], configured_title: str) -> str:
    sheet_id = title_map.get(configured_title)
    if not sheet_id:
        raise RuntimeError(
            f"飞书表格中找不到名为 '{configured_title}' 的子表；当前子表：{list(title_map.keys())}"
        )
    return sheet_id


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        # Rich-text cells sometimes come as list-of-segments
        return "".join(normalize_cell(part) for part in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("v") or "")
    return str(value).strip()


def rows_to_dicts(values: List[List[Any]]) -> List[Dict[str, str]]:
    if not values:
        return []
    headers = [normalize_cell(cell) for cell in values[0]]
    rows: List[Dict[str, str]] = []
    for raw in values[1:]:
        if not any(normalize_cell(cell) for cell in raw):
            continue
        row = {headers[i]: normalize_cell(raw[i]) if i < len(raw) else "" for i in range(len(headers))}
        rows.append(row)
    return rows


def parse_watchlist(rows: List[Dict[str, str]]) -> tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    indices: List[Dict[str, Any]] = []
    group_to_stocks: Dict[str, List[str]] = {}
    for row in rows:
        kind = (row.get("类型") or "").lower()
        ticker = row.get("代码") or ""
        if not ticker:
            continue
        if kind == "index":
            indices.append({"ticker": ticker, "name": row.get("备注") or ticker})
        elif kind == "stock":
            group = row.get("分组") or ""
            if not group:
                continue
            group_to_stocks.setdefault(group, []).append(ticker)
    return indices, group_to_stocks


def parse_groups(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [
        {
            "name": row.get("分组") or "",
            "emoji": row.get("emoji") or "",
            "desc": row.get("描述") or "",
        }
        for row in rows
        if (row.get("分组") or "")
    ]


def parse_thresholds(rows: List[Dict[str, str]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in rows:
        key = (row.get("key") or "").strip()
        raw = row.get("value")
        if not key or raw in (None, ""):
            continue
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def assemble_config(
    indices: List[Dict[str, Any]],
    group_meta: List[Dict[str, str]],
    group_to_stocks: Dict[str, List[str]],
    thresholds: Dict[str, float],
    lark_sheet: Dict[str, Any],
    existing_output: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    groups_out: List[Dict[str, Any]] = []
    for meta in group_meta:
        name = meta["name"]
        groups_out.append(
            {
                "name": name,
                "desc": meta["desc"],
                "emoji": meta["emoji"],
                "stocks": group_to_stocks.get(name, []),
            }
        )
    # Surface any group present in watchlist but missing from meta sheet
    for name, stocks in group_to_stocks.items():
        if not any(g["name"] == name for g in groups_out):
            groups_out.append({"name": name, "desc": "", "emoji": "", "stocks": stocks})

    config: Dict[str, Any] = {
        "lark_sheet": lark_sheet,
        "indices": indices,
        "groups": groups_out,
        "thresholds": thresholds or {"drop": -7.0, "rise": 7.0},
    }
    if existing_output:
        config["output"] = existing_output
    return config


def write_yaml(path: Path, config: Dict[str, Any]) -> None:
    header = (
        "# 美股日报观察池配置\n"
        "#\n"
        "# ⚠️ 本文件由 scripts/sync_from_lark.py 从飞书表格同步生成，请勿手工编辑。\n"
        "# 编辑入口（飞书表格 URL）：\n"
        f"#   {config['lark_sheet'].get('url', '')}\n"
        "# 跳过 sync：python scripts/generate_report.py --no-sync\n"
        "\n"
    )
    body = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    path.write_text(header + body, encoding="utf-8")


def sync(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as fh:
        current = yaml.safe_load(fh) or {}

    lark_sheet = current.get("lark_sheet") or {}
    token = lark_sheet.get("spreadsheet_token")
    if not token:
        raise RuntimeError(
            "lark_sheet.spreadsheet_token 未配置；先在 yaml 写入 token，或运行 --no-sync。"
        )

    sheet_titles = lark_sheet.get("sheets") or {}
    watchlist_title = sheet_titles.get("watchlist", "watchlist")
    groups_title = sheet_titles.get("groups", "groups")
    thresholds_title = sheet_titles.get("thresholds", "thresholds")

    title_map = fetch_sheet_id_map(token)
    watchlist_rows = rows_to_dicts(run_lark_read(token, resolve_sheet_id(title_map, watchlist_title)))
    groups_rows = rows_to_dicts(run_lark_read(token, resolve_sheet_id(title_map, groups_title)))
    thresholds_rows = rows_to_dicts(run_lark_read(token, resolve_sheet_id(title_map, thresholds_title)))

    indices, group_to_stocks = parse_watchlist(watchlist_rows)
    group_meta = parse_groups(groups_rows)
    thresholds = parse_thresholds(thresholds_rows)

    new_config = assemble_config(
        indices,
        group_meta,
        group_to_stocks,
        thresholds,
        lark_sheet,
        existing_output=current.get("output"),
    )
    write_yaml(config_path, new_config)
    return new_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync stock_pool.yaml from the Lark spreadsheet.")
    parser.add_argument(
        "--config",
        "-c",
        default=str(DEFAULT_CONFIG_PATH),
        help="目标 yaml 路径，默认 assets/stock_pool.yaml",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印解析结果，不写文件")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if args.dry_run:
        # Run through parse, but don't write
        with config_path.open("r", encoding="utf-8") as fh:
            current = yaml.safe_load(fh) or {}
        lark_sheet = current.get("lark_sheet") or {}
        token = lark_sheet.get("spreadsheet_token")
        if not token:
            print("lark_sheet.spreadsheet_token 未配置", file=sys.stderr)
            sys.exit(1)
        sheet_titles = lark_sheet.get("sheets") or {}
        title_map = fetch_sheet_id_map(token)
        watchlist_rows = rows_to_dicts(
            run_lark_read(token, resolve_sheet_id(title_map, sheet_titles.get("watchlist", "watchlist")))
        )
        groups_rows = rows_to_dicts(
            run_lark_read(token, resolve_sheet_id(title_map, sheet_titles.get("groups", "groups")))
        )
        thresholds_rows = rows_to_dicts(
            run_lark_read(token, resolve_sheet_id(title_map, sheet_titles.get("thresholds", "thresholds")))
        )
        indices, group_to_stocks = parse_watchlist(watchlist_rows)
        preview = {
            "indices": indices,
            "group_to_stocks": group_to_stocks,
            "group_meta": parse_groups(groups_rows),
            "thresholds": parse_thresholds(thresholds_rows),
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return

    new_config = sync(config_path)
    summary = {
        "status": "synced",
        "config": str(config_path),
        "indices": [i["ticker"] for i in new_config.get("indices", [])],
        "groups": [
            {"name": g["name"], "stocks": g["stocks"]} for g in new_config.get("groups", [])
        ],
        "thresholds": new_config.get("thresholds", {}),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
