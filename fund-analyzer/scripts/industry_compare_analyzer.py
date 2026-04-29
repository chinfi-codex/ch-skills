#!/usr/bin/env python3
"""
Industry comparison analyzer for thematic public funds.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from fund_data_fetcher import FundDataFetcher
from industry_compare_metrics import (
    annual_return,
    downside_protection,
    drawdown_repair_days,
    fmt_aum_total,
    fmt_days,
    fmt_focus,
    fmt_num,
    fmt_pct,
    fmt_percentile,
    format_date,
    max_drawdown,
)
from industry_compare_reporting import format_report

TRADING_DAYS = 252
RISK_FREE_RATE = 0.015
MIN_FUND_COUNT = 3
MIN_FOUND_DAYS = int(365 * 1.5)
MIN_AUM = 2.0
MAX_AUM = 100.0
INDUSTRY_ALIASES: Dict[str, Dict[str, Any]] = {
    "医药生物": {
        "code": "801150.SI",
        "name": "医药生物",
        "aliases": ["医药", "医药生物", "创新药", "医疗", "医疗健康", "生物医药"],
    },
    "电子": {
        "code": "801080.SI",
        "name": "电子",
        "aliases": ["电子", "半导体", "芯片", "消费电子"],
    },
    "计算机": {
        "code": "801750.SI",
        "name": "计算机",
        "aliases": ["计算机", "软件", "工业软件", "信创", "云计算", "人工智能", "ai"],
    },
    "新能源": {
        "code": "801730.SI",
        "name": "电力设备",
        "aliases": ["新能源", "光伏", "锂电", "储能", "电力设备", "新能车", "新能源汽车"],
    },
    "消费": {
        "code": "801120.SI",
        "name": "食品饮料",
        "aliases": ["消费", "食品饮料", "白酒", "乳制品", "消费品"],
    },
    "军工": {
        "code": "801740.SI",
        "name": "国防军工",
        "aliases": ["军工", "国防军工", "航空航天"],
    },
}

PROHIBITED_TERMS = [
    "买入",
    "卖出",
    "持有",
    "加仓",
    "减仓",
    "高位",
    "低位",
    "择时",
]


@dataclass
class IndustryContext:
    input_name: str
    canonical_name: str
    index_code: str


class IndustryCompareAnalyzer:
    def __init__(self, fetcher: FundDataFetcher):
        self.fetcher = fetcher
        self._index_members_cache: Dict[str, set[str]] = {}
        self._fund_info_cache: Dict[str, Dict[str, Any]] = {}
        self._aum_cache: Dict[str, Optional[float]] = {}
        self._manager_cache: Dict[str, Optional[pd.DataFrame]] = {}
        self._nav_cache: Dict[str, Optional[pd.DataFrame]] = {}

    def compare(self, industry_term: str) -> str:
        context = self._resolve_industry(industry_term)
        if context is None:
            return "未识别行业词，请使用支持的行业名称或别名。"

        candidates = self._screen_funds(context)
        if len(candidates) < MIN_FUND_COUNT:
            return "该行业可选基金不足"

        selected = candidates[:MIN_FUND_COUNT]
        metrics = self._build_comparison_metrics(context, selected)
        if metrics is None:
            return "该行业可选基金不足"
        return format_report(context, metrics, PROHIBITED_TERMS)

    def _resolve_industry(self, industry_term: str) -> Optional[IndustryContext]:
        query = industry_term.strip().lower()
        for payload in INDUSTRY_ALIASES.values():
            aliases = [alias.lower() for alias in payload["aliases"]]
            if query in aliases:
                return IndustryContext(
                    input_name=industry_term.strip(),
                    canonical_name=payload["name"],
                    index_code=payload["code"],
                )
        return None

    def _screen_funds(self, context: IndustryContext) -> List[Dict[str, Any]]:
        all_funds = self.fetcher.get_all_funds()
        if all_funds.empty:
            return []

        funds = all_funds.copy()
        funds = funds[funds.apply(lambda row: self._is_allowed_fund_type(row.to_dict()), axis=1)]
        funds = funds[funds["found_date"].notna()]
        funds["found_date"] = pd.to_datetime(
            funds["found_date"], format="%Y%m%d", errors="coerce"
        )
        cutoff = datetime.now() - timedelta(days=MIN_FOUND_DAYS)
        funds = funds[funds["found_date"] <= cutoff]

        text_filtered = funds[
            funds.apply(
                lambda row: self._text_matches_keyword(context.input_name, row.to_dict()),
                axis=1,
            )
        ]
        if len(text_filtered) < MIN_FUND_COUNT:
            text_filtered = funds[
                funds.apply(
                    lambda row: self._text_matches_industry(context, row.to_dict()),
                    axis=1,
                )
            ]
        text_filtered = self._dedupe_share_classes(text_filtered)
        text_filtered = text_filtered.sort_values(["found_date"], ascending=[True])
        qualified: List[Dict[str, Any]] = []
        for _, fund in text_filtered.iterrows():
            candidate = self._build_candidate(context, fund.to_dict())
            if candidate is not None:
                qualified.append(candidate)

        qualified.sort(key=lambda item: item["sharpe"], reverse=True)
        return qualified

    def _build_candidate(
        self, context: IndustryContext, fund_info: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        ts_code = fund_info.get("ts_code", "")
        if not ts_code:
            return None

        aum = self._resolve_fund_aum(ts_code, fund_info)
        if aum is None:
            return None
        if aum < MIN_AUM or aum > MAX_AUM:
            return None

        manager_row = self._get_primary_manager(ts_code)
        if manager_row is None:
            return None

        tenure_years = self._manager_tenure_years(manager_row)
        if tenure_years < 1:
            return None

        nav_df = self._get_adjusted_nav(ts_code)
        if nav_df is None or len(nav_df) < TRADING_DAYS:
            return None

        returns = nav_df["adjusted_nav"].pct_change().dropna()
        if returns.empty:
            return None

        annual_return_value = annual_return(nav_df["adjusted_nav"], TRADING_DAYS)
        annual_vol = returns.std() * math.sqrt(TRADING_DAYS)
        sharpe = (
            (annual_return_value - RISK_FREE_RATE) / annual_vol if annual_vol > 0 else -999
        )

        return {
            "ts_code": ts_code,
            "name": fund_info.get("name", ""),
            "aum": aum,
            "fund_info": fund_info,
            "manager": manager_row,
            "tenure_years": tenure_years,
            "nav_df": nav_df,
            "sharpe": sharpe,
        }

    def _matches_industry(
        self, context: IndustryContext, fund_info: Dict[str, Any], ts_code: str
    ) -> bool:
        return self._text_matches_industry(context, fund_info)

    def _text_matches_industry(
        self, context: IndustryContext, fund_info: Dict[str, Any]
    ) -> bool:
        name = str(fund_info.get("name", "") or "")
        benchmark = str(fund_info.get("benchmark", "") or "")
        text = f"{name} {benchmark}".lower()
        return any(alias.lower() in text for alias in self._industry_aliases(context))

    def _text_matches_keyword(self, keyword: str, fund_info: Dict[str, Any]) -> bool:
        query = str(keyword or "").strip().lower()
        if not query:
            return False
        name = str(fund_info.get("name", "") or "")
        benchmark = str(fund_info.get("benchmark", "") or "")
        text = f"{name} {benchmark}".lower()
        return query in text

    def _build_comparison_metrics(
        self, context: IndustryContext, funds: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        industry_df = self.fetcher.get_sw_daily(context.index_code, count=TRADING_DAYS)
        market_df = self.fetcher.get_market_index(count=TRADING_DAYS)
        if industry_df is None or market_df is None or industry_df.empty or market_df.empty:
            return None

        aligned_frames: Dict[str, pd.DataFrame] = {}
        base = pd.DataFrame(index=industry_df.index)
        base["industry_close"] = pd.to_numeric(industry_df["close"], errors="coerce")
        base["industry_ret"] = base["industry_close"].pct_change()

        market_series = pd.to_numeric(market_df["close"], errors="coerce")
        base = base.join(market_series.rename("market_close"), how="inner")
        base["market_ret"] = base["market_close"].pct_change()
        base = base.dropna()
        if base.empty:
            return None

        enriched_funds: List[Dict[str, Any]] = []
        for fund in funds:
            nav_df = fund["nav_df"][["adjusted_nav"]].copy()
            nav_df["fund_ret"] = nav_df["adjusted_nav"].pct_change()
            aligned = base.join(nav_df, how="inner").dropna()
            if len(aligned) < 120:
                continue
            aligned_frames[fund["ts_code"]] = aligned

            excess_vs_industry = aligned["fund_ret"] - aligned["industry_ret"]
            excess_vs_market = aligned["fund_ret"] - aligned["market_ret"]
            annual_return_value = annual_return(aligned["adjusted_nav"], TRADING_DAYS)
            annual_vol = aligned["fund_ret"].std() * math.sqrt(TRADING_DAYS)
            sharpe = (
                (annual_return_value - RISK_FREE_RATE) / annual_vol if annual_vol > 0 else np.nan
            )
            max_drawdown_value, peak_idx, trough_idx = max_drawdown(aligned["adjusted_nav"])
            calmar = annual_return_value / max_drawdown_value if max_drawdown_value > 0 else np.nan
            repair_days = drawdown_repair_days(aligned["adjusted_nav"], peak_idx, trough_idx)
            downside_capture = downside_protection(aligned)
            manager_metrics = self._manager_metrics(
                context, fund["manager"], fund["fund_info"], fund["ts_code"]
            )

            enriched_funds.append(
                {
                    **fund,
                    "aligned": aligned,
                    "industry_alpha": excess_vs_industry.mean() * TRADING_DAYS,
                    "market_excess": ((1 + excess_vs_market).prod() - 1),
                    "excess_stability": excess_vs_industry.std() * math.sqrt(TRADING_DAYS),
                    "industry_win_rate": (excess_vs_industry > 0).mean(),
                    "annual_return": annual_return_value,
                    "sharpe_calc": sharpe,
                    "calmar": calmar,
                    "max_drawdown": max_drawdown_value,
                    "repair_days": repair_days,
                    "downside_protection": downside_capture,
                    "manager_metrics": manager_metrics,
                }
            )

        if len(enriched_funds) < MIN_FUND_COUNT:
            return None

        enriched_funds = enriched_funds[:MIN_FUND_COUNT]
        rankings = self._rank_funds(enriched_funds)
        report_date = min(
            fund["aligned"].index.max().strftime("%Y-%m-%d") for fund in enriched_funds
        )
        industry_drawdown = max_drawdown(base["industry_close"])[0]
        industry_repair = drawdown_repair_days(
            base["industry_close"],
            max_drawdown(base["industry_close"])[1],
            max_drawdown(base["industry_close"])[2],
        )
        return {
            "date": report_date,
            "industry_name": context.canonical_name,
            "funds": enriched_funds,
            "industry_drawdown": industry_drawdown,
            "industry_repair": industry_repair,
            "rankings": rankings,
        }

    def _manager_metrics(
        self,
        context: IndustryContext,
        manager_row: pd.Series,
        fund_info: Dict[str, Any],
        ts_code: str,
    ) -> Dict[str, Any]:
        manager_name = str(manager_row.get("name", "") or "").strip()
        records = self.fetcher.get_manager_related_records(manager_name)
        tenure_years = self._manager_tenure_years(manager_row)

        if records is None or records.empty:
            return {
                "tenure_years": tenure_years,
                "industry_focus_ratio": None,
                "history_percentile": None,
                "aum_total": None,
                "resume": str(manager_row.get("resume", "") or "").strip(),
            }

        unique_codes = sorted(set(records["ts_code"].dropna().astype(str).tolist()))
        industry_hits = 0
        current_aum_total = 0.0
        peer_returns: List[float] = []
        aum_sample_count = 0

        for related_ts_code in unique_codes:
            related_info = self._get_fund_info(related_ts_code)
            if not related_info:
                continue

            if self._matches_industry(context, related_info, related_ts_code):
                industry_hits += 1

            if aum_sample_count < 10:
                aum = self._resolve_fund_aum(related_ts_code, related_info)
                if aum is not None:
                    current_aum_total += aum
                    aum_sample_count += 1

            if related_info.get("invest_type") == fund_info.get("invest_type"):
                related_nav = self._get_adjusted_nav(related_ts_code)
                if related_nav is not None and len(related_nav) >= 120:
                    peer_returns.append(annual_return(related_nav["adjusted_nav"], TRADING_DAYS))

        industry_focus_ratio = (
            industry_hits / len(unique_codes) if unique_codes else None
        )
        history_percentile = None
        if peer_returns:
            target_return = annual_return(self._get_adjusted_nav(ts_code)["adjusted_nav"], TRADING_DAYS)
            history_percentile = (
                sum(ret <= target_return for ret in peer_returns) / len(peer_returns)
            )

        return {
            "tenure_years": tenure_years,
            "industry_focus_ratio": industry_focus_ratio,
            "history_percentile": history_percentile,
            "aum_total": current_aum_total if current_aum_total > 0 else None,
            "resume": str(manager_row.get("resume", "") or "").strip(),
        }

    def _rank_funds(self, funds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        scored: List[Dict[str, Any]] = []
        for fund in funds:
            positives = [
                ("行业超额", fund["industry_alpha"]),
                ("风险调整收益", fund["sharpe_calc"]),
                ("回撤控制", -fund["max_drawdown"]),
                ("行业胜率", fund["industry_win_rate"]),
            ]
            positives.sort(key=lambda item: (item[1] if pd.notna(item[1]) else -999), reverse=True)

            negatives = [
                ("回撤偏大", fund["max_drawdown"]),
                (
                    "超额稳定性一般",
                    fund["excess_stability"] if pd.notna(fund["excess_stability"]) else 999,
                ),
                (
                    "经理行业专注度一般",
                    1 - fund["manager_metrics"]["industry_focus_ratio"]
                    if fund["manager_metrics"]["industry_focus_ratio"] is not None
                    else 999,
                ),
            ]
            negatives.sort(key=lambda item: item[1], reverse=True)

            score = 0.0
            for metric in ["industry_alpha", "sharpe_calc", "calmar", "industry_win_rate"]:
                value = fund.get(metric)
                if pd.notna(value):
                    score += float(value)
            score -= fund["max_drawdown"] * 2 if pd.notna(fund["max_drawdown"]) else 0

            scored.append(
                {
                    "name": fund["name"],
                    "score": score,
                    "strength": positives[0][0],
                    "weakness": negatives[0][0],
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored

    def _best_comment(self, funds: List[Dict[str, Any]], key: str, positive: bool) -> str:
        values = []
        for fund in funds:
            value = fund.get(key)
            if pd.notna(value):
                values.append((fund["name"], value))
        if not values:
            return "样本不足"
        values.sort(key=lambda item: item[1], reverse=positive)
        return f"{values[0][0]}最优"

    def _best_comment_by_nested(
        self, funds: List[Dict[str, Any]], outer: str, inner: str, positive: bool
    ) -> str:
        values = []
        for fund in funds:
            value = fund.get(outer, {}).get(inner)
            if value is not None and pd.notna(value):
                values.append((fund["name"], value))
        if not values:
            return "样本不足"
        values.sort(key=lambda item: item[1], reverse=positive)
        return f"{values[0][0]}更优"

    def _sorted_comment(self, funds: List[Dict[str, Any]], key: str) -> str:
        values = [(fund["name"], fund.get(key)) for fund in funds if pd.notna(fund.get(key))]
        if len(values) != len(funds):
            return "样本不足"
        values.sort(key=lambda item: item[1], reverse=True)
        return ">".join(item[0] for item in values)

    def _industry_aliases(self, context: IndustryContext) -> List[str]:
        for payload in INDUSTRY_ALIASES.values():
            if payload["code"] == context.index_code:
                return payload["aliases"]
        return [context.canonical_name]

    def _dedupe_share_classes(self, funds: pd.DataFrame) -> pd.DataFrame:
        """Keep one representative share class per fund product."""
        if funds.empty:
            return funds

        df = funds.copy()
        df["base_name"] = df["name"].astype(str).apply(self._normalize_fund_name)
        df["share_rank"] = df["name"].astype(str).apply(self._share_class_rank)
        df = df.sort_values(["base_name", "share_rank", "found_date"], ascending=[True, True, True])
        df = df.drop_duplicates(subset=["base_name"], keep="first")
        return df.drop(columns=["base_name", "share_rank"])

    def _normalize_fund_name(self, name: str) -> str:
        cleaned = re.sub(r"[A-Z]$", "", name.strip())
        cleaned = re.sub(r"[ABCDEFHIQRS]类$", "", cleaned)
        return cleaned

    def _share_class_rank(self, name: str) -> int:
        suffix = name.strip()[-1:] if name else ""
        order = {"A": 0, "": 1, "C": 2, "E": 3}
        return order.get(suffix, 4)

    def _is_allowed_fund_type(self, fund_info: Dict[str, Any]) -> bool:
        fund_type = str(fund_info.get("fund_type", "") or "").strip()
        invest_type = str(fund_info.get("invest_type", "") or "").strip()

        if "指数" in invest_type:
            return False
        if fund_type == "股票型":
            return True
        if invest_type == "灵活配置型":
            return True
        if fund_type == "混合型":
            return True
        if fund_type == "偏股混合型" or invest_type == "偏股混合型":
            return True
        return False

    def _get_index_members(self, index_code: str) -> set[str]:
        if index_code in self._index_members_cache:
            return self._index_members_cache[index_code]

        members_df = self.fetcher.get_index_member(index_code)
        if members_df is None or members_df.empty:
            self._index_members_cache[index_code] = set()
            return set()

        member_set = set(members_df["con_code"].dropna().astype(str).tolist())
        self._index_members_cache[index_code] = member_set
        return member_set

    def _get_fund_info(self, ts_code: str) -> Dict[str, Any]:
        if ts_code not in self._fund_info_cache:
            self._fund_info_cache[ts_code] = self.fetcher.get_fund_info(ts_code) or {}
        return self._fund_info_cache[ts_code]

    def _get_aum(self, ts_code: str) -> Optional[float]:
        if ts_code not in self._aum_cache:
            self._aum_cache[ts_code] = self.fetcher.get_fund_aum(ts_code)
        return self._aum_cache[ts_code]

    def _resolve_fund_aum(self, ts_code: str, fund_info: Dict[str, Any]) -> Optional[float]:
        issue_amount = fund_info.get("issue_amount")
        if issue_amount is not None and not pd.isna(issue_amount):
            try:
                return float(issue_amount)
            except (TypeError, ValueError):
                pass

        aum = self._get_aum(ts_code)
        if aum is not None and pd.notna(aum):
            return float(aum)
        return None

    def _get_adjusted_nav(self, ts_code: str) -> Optional[pd.DataFrame]:
        if ts_code not in self._nav_cache:
            self._nav_cache[ts_code] = self.fetcher.get_fund_nav_adjusted(
                ts_code, count=TRADING_DAYS
            )
        return self._nav_cache[ts_code]

    def _get_primary_manager(self, ts_code: str) -> Optional[pd.Series]:
        if ts_code not in self._manager_cache:
            self._manager_cache[ts_code] = self.fetcher.get_current_fund_managers(ts_code)
        manager_df = self._manager_cache[ts_code]
        if manager_df is None or manager_df.empty:
            return None
        return manager_df.iloc[0]

    def _manager_tenure_years(self, manager_row: pd.Series) -> float:
        begin_date = manager_row.get("begin_date")
        if pd.isna(begin_date):
            return 0.0
        begin_dt = pd.to_datetime(begin_date)
        return max((pd.Timestamp(datetime.now()) - begin_dt).days / 365.25, 0.0)
