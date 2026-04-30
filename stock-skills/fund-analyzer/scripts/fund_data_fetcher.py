#!/usr/bin/env python3
"""
基金数据获取模块
使用 Tushare Pro API 获取基金数据
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import numpy as np

try:
    import tushare as ts
except ImportError:
    print("错误：请先安装tushare: pip install tushare")
    sys.exit(1)


def get_tushare_token() -> str:
    """获取Tushare Token，优先级：环境变量 > 当前目录.env文件"""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token

    try:
        env_path = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    if k.strip() == "TUSHARE_TOKEN":
                        token = v.strip().strip('"').strip("'")
                        if token:
                            return token
    except Exception:
        pass

    return ""


def get_tushare_pro():
    """获取Tushare Pro API客户端"""
    token = get_tushare_token()
    if not token:
        raise RuntimeError("缺少TUSHARE_TOKEN: 请设置环境变量或在.env文件中配置")
    return ts.pro_api(token)


class FundDataFetcher:
    """基金数据获取器"""

    def __init__(self):
        self.pro = get_tushare_pro()
        self._fund_list_cache: Optional[pd.DataFrame] = None
        self._cache_time: Optional[datetime] = None
        self._request_cache: Dict[Tuple[str, Tuple[Tuple[str, Any], ...]], Tuple[float, Any]] = {}
        self._last_request_ts = 0.0
        self._min_interval = float(os.environ.get("TUSHARE_MIN_INTERVAL", "0.12"))
        self._cache_ttl_seconds = int(os.environ.get("TUSHARE_CACHE_TTL_SECONDS", "300"))
        self._rate_limit_sleep_seconds = int(os.environ.get("TUSHARE_RATE_LIMIT_SLEEP_SECONDS", "65"))
        self._max_retries = int(os.environ.get("TUSHARE_MAX_RETRIES", "2"))

    def _request(
        self,
        method_name: str,
        *,
        ttl_seconds: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        ttl = self._cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        normalized_items = tuple(sorted((key, self._normalize_cache_value(value)) for key, value in kwargs.items()))
        cache_key = (method_name, normalized_items)
        now = time.time()
        if cache_key in self._request_cache:
            cached_at, cached_value = self._request_cache[cache_key]
            if ttl > 0 and now - cached_at <= ttl:
                return cached_value.copy() if isinstance(cached_value, pd.DataFrame) else cached_value

        elapsed = now - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        method = getattr(self.pro, method_name)
        attempt = 0
        while True:
            try:
                value = method(**kwargs)
                self._last_request_ts = time.time()
                self._request_cache[cache_key] = (
                    self._last_request_ts,
                    value.copy() if isinstance(value, pd.DataFrame) else value,
                )
                return value
            except Exception as exc:
                if not self._is_rate_limit_error(exc) or attempt >= self._max_retries:
                    raise

                attempt += 1
                sleep_seconds = self._rate_limit_wait_seconds(exc)
                print(
                    f"Tushare 接口 {method_name} 命中限流，第 {attempt} 次重试前等待 {sleep_seconds} 秒..."
                )
                time.sleep(sleep_seconds)
                self._last_request_ts = time.time()

    def _normalize_cache_value(self, value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        message = str(exc)
        return "每分钟最多访问该接口" in message or "rate limit" in message.lower()

    def _rate_limit_wait_seconds(self, exc: Exception) -> int:
        message = str(exc)
        match = re.search(r"每分钟最多访问该接口(\d+)次", message)
        if match:
            # Sleep a full window plus a small buffer rather than trying to
            # infer remaining quota from client-side state.
            return self._rate_limit_sleep_seconds
        return self._rate_limit_sleep_seconds

    def get_all_funds(self) -> pd.DataFrame:
        """获取所有公募基金列表（带缓存，缓存1天）"""
        if (
            self._fund_list_cache is not None
            and self._cache_time is not None
            and (datetime.now() - self._cache_time).days < 1
        ):
            return self._fund_list_cache

        try:
            df = self._request("fund_basic", ttl_seconds=86400)
            if df is None or df.empty:
                return pd.DataFrame()

            self._fund_list_cache = df
            self._cache_time = datetime.now()
            return df
        except Exception as e:
            print(f"获取基金列表失败: {e}")
            return pd.DataFrame()

    def search_fund(self, query: str) -> List[Dict[str, Any]]:
        """搜索基金，返回所有匹配结果列表"""
        query = query.strip()
        all_funds = self.get_all_funds()

        if all_funds.empty:
            return []

        results = []

        if query.isdigit():
            if len(query) == 6:
                result = all_funds[all_funds["ts_code"] == query + ".OF"]
                if not result.empty:
                    results.extend(result.to_dict("records"))
                result = all_funds[all_funds["ts_code"] == query + ".SZ"]
                if not result.empty:
                    results.extend(result.to_dict("records"))
                result = all_funds[all_funds["ts_code"] == query + ".SH"]
                if not result.empty:
                    results.extend(result.to_dict("records"))
                result = all_funds[all_funds["ts_code"].str.startswith(query, na=False)]
                if not result.empty:
                    results.extend(result.to_dict("records"))
        else:
            result = all_funds[
                all_funds["name"].str.contains(query, case=False, na=False)
            ]
            if not result.empty:
                results.extend(result.to_dict("records"))

        seen = set()
        unique_results = []
        for r in results:
            ts_code = r.get("ts_code", "")
            if ts_code not in seen:
                seen.add(ts_code)
                unique_results.append(r)

        return unique_results

    def get_fund_nav(self, ts_code: str, count: int = 250) -> Optional[pd.DataFrame]:
        """获取基金净值数据（日频）"""
        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=count * 4)).strftime("%Y%m%d")

            df = self._request(
                "fund_nav",
                ts_code=ts_code, start_date=start_date, end_date=end_date
            )
            if df is not None and not df.empty:
                df = df.sort_values("nav_date")
                df["nav_date"] = pd.to_datetime(df["nav_date"])
                df.set_index("nav_date", inplace=True)
                df["nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
                if "nav_rate" in df.columns:
                    df["nav_rate"] = pd.to_numeric(df["nav_rate"], errors="coerce")
                df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
                if "accum_nav" in df.columns:
                    df["accum_nav"] = pd.to_numeric(df["accum_nav"], errors="coerce")
                if "accum_nav_rate" in df.columns:
                    df["accum_nav_rate"] = pd.to_numeric(
                        df["accum_nav_rate"], errors="coerce"
                    )
                return df.tail(count)
            return None
        except Exception as e:
            print(f"获取基金净值失败: {e}")
            return None

    def get_fund_info(self, ts_code: str) -> Optional[Dict[str, Any]]:
        """获取基金基本信息"""
        try:
            df = self._request("fund_basic", ts_code=ts_code)
            if df is not None and not df.empty:
                return df.iloc[0].to_dict()
            return None
        except Exception as e:
            print(f"获取基金信息失败: {e}")
            return None

    def get_fund_manager(self, ts_code: str) -> Optional[pd.DataFrame]:
        """获取基金经理信息"""
        try:
            df = self._request("fund_manager", ts_code=ts_code)
            if df is not None and not df.empty:
                return df
            return None
        except Exception as e:
            print(f"获取基金经理信息失败: {e}")
            return None

    def get_fund_scale(
        self, ts_code: str, start_date: str = None, end_date: str = None
    ) -> Optional[pd.DataFrame]:
        """获取基金规模数据（通过份额*净值估算）"""
        try:
            if end_date is None:
                end_date = datetime.now().strftime("%Y%m%d")
            if start_date is None:
                start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

            share_df = self._request(
                "fund_share",
                ts_code=ts_code, start_date=start_date, end_date=end_date
            )
            if share_df is None or share_df.empty:
                return None

            nav_df = self._request(
                "fund_nav",
                ts_code=ts_code, start_date=start_date, end_date=end_date
            )
            if nav_df is None or nav_df.empty:
                return None

            share_df = share_df.sort_values("trade_date")
            nav_df = nav_df.sort_values("nav_date")

            merged = pd.merge_asof(
                share_df.sort_values("trade_date"),
                nav_df.sort_values("nav_date"),
                left_on="trade_date",
                right_on="nav_date",
                direction="backward",
            )

            if merged is not None and not merged.empty:
                merged["total_net_asset"] = merged["fd_share"] * merged["unit_nav"]
                return merged[["trade_date", "fd_share", "unit_nav", "total_net_asset"]]

            return None
        except Exception as e:
            print(f"获取基金规模失败: {e}")
            return None

    def get_fund_aum(self, ts_code: str) -> Optional[float]:
        """获取基金最新资产管理规模（亿元）"""
        try:
            share_df = self._request("fund_share", ts_code=ts_code, limit=1)
            if share_df is None or share_df.empty:
                return None

            nav_df = self._request("fund_nav", ts_code=ts_code, limit=1)
            if nav_df is None or nav_df.empty:
                return None

            latest_share = share_df["fd_share"].iloc[0]
            latest_nav = nav_df["unit_nav"].iloc[0]

            if pd.notna(latest_share) and pd.notna(latest_nav):
                return latest_share * latest_nav / 10000

            return None
        except Exception as e:
            print(f"获取基金规模失败: {e}")
            return None

    def get_fund_portfolio(
        self, ts_code: str, start_date: str = None, end_date: str = None
    ) -> Optional[pd.DataFrame]:
        """获取基金持仓数据（需要5000权限）"""
        try:
            if end_date is None:
                end_date = datetime.now().strftime("%Y%m%d")
            if start_date is None:
                start_date = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")

            df = self._request(
                "fund_portfolio",
                ts_code=ts_code, start_date=start_date, end_date=end_date
            )
            if df is not None and not df.empty:
                return df
            return None
        except Exception as e:
            print(f"获取基金持仓失败: {e}")
            return None

    def get_shanghai_index(self, count: int = 250) -> Optional[pd.DataFrame]:
        """获取上证指数日K线数据"""
        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")

            df = self._request(
                "index_daily",
                ts_code="000001.SH", start_date=start_date, end_date=end_date
            )
            if df is not None and not df.empty:
                df = df.sort_values("trade_date")
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df.set_index("trade_date", inplace=True)
                for col in ["open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                return df.tail(count)
            return None
        except Exception as e:
            print(f"获取上证指数失败: {e}")
            return None

    def get_current_nav(self, ts_code: str) -> Optional[float]:
        """获取最新净值"""
        try:
            df = self.get_fund_nav(ts_code, count=1)
            if df is not None and not df.empty:
                return float(df.iloc[-1]["nav"])
            return None
        except Exception as e:
            print(f"获取最新净值失败: {e}")
            return None

    def get_nav_ma(self, ts_code: str, days: int = 200) -> Optional[pd.Series]:
        """获取基金净值均线"""
        try:
            df = self.get_fund_nav(ts_code, count=days * 2)
            if df is None or df.empty or len(df) < days:
                return None

            ma = df["nav"].rolling(window=days).mean()
            return ma

        except Exception as e:
            print(f"计算均线失败: {e}")
            return None

    def get_nav_monthly_ma(self, ts_code: str, months: int = 20) -> Optional[pd.Series]:
        """获取基金净值月线均线"""
        try:
            df = self.get_fund_nav(ts_code, count=months * 30)
            if df is None or df.empty:
                return None

            monthly = df["nav"].resample("ME").last()
            ma = monthly.rolling(window=months).mean()
            return ma

        except Exception as e:
            print(f"计算月线均线失败: {e}")
            return None
    def get_fund_nav_adjusted(
        self, ts_code: str, count: int = 252
    ) -> Optional[pd.DataFrame]:
        """Get adjusted NAV series, preferring accum_nav when available."""
        df = self.get_fund_nav(ts_code, count=count * 2)
        if df is None or df.empty:
            return None

        adjusted = df.copy()
        if "accum_nav" in adjusted.columns and adjusted["accum_nav"].notna().any():
            adjusted["adjusted_nav"] = pd.to_numeric(
                adjusted["accum_nav"], errors="coerce"
            )
        else:
            adjusted["adjusted_nav"] = pd.to_numeric(
                adjusted["unit_nav"], errors="coerce"
            )

        adjusted = adjusted.dropna(subset=["adjusted_nav"])
        if adjusted.empty:
            return None
        if adjusted.index.has_duplicates:
            adjusted = adjusted[~adjusted.index.duplicated(keep="last")]
        return adjusted.tail(count)

    def get_sw_daily(
        self, index_code: str, count: int = 252
    ) -> Optional[pd.DataFrame]:
        """Get Shenwan industry daily series."""
        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")
            df = self._request(
                "sw_daily",
                ts_code=index_code, start_date=start_date, end_date=end_date
            )
            if df is None or df.empty:
                return None

            df = df.sort_values("trade_date")
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df.set_index("trade_date", inplace=True)
            for col in ["open", "high", "low", "close", "vol", "amount", "pct_change"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df.tail(count)
        except Exception as e:
            print(f"鑾峰彇鐢充竾琛屼笟鎸囨暟澶辫触: {e}")
            return None

    def get_index_member(self, index_code: str) -> Optional[pd.DataFrame]:
        """Get all historical members for a Shenwan industry index."""
        try:
            df = self._request("index_member", index_code=index_code, ttl_seconds=86400)
            if df is None or df.empty:
                return None
            return df
        except Exception as e:
            print(f"鑾峰彇琛屼笟鎴愬垎鑲″け璐? {e}")
            return None

    def get_market_index(self, count: int = 252) -> Optional[pd.DataFrame]:
        """Alias for the market reference index used by industry comparison."""
        return self.get_shanghai_index(count=count)

    def get_current_fund_managers(self, ts_code: str) -> Optional[pd.DataFrame]:
        """Get current managers, or the most recent historical row if current is absent."""
        manager_df = self.get_fund_manager(ts_code)
        if manager_df is None or manager_df.empty:
            return None

        df = manager_df.copy()
        for col in ["ann_date", "begin_date", "end_date"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce")

        if "end_date" in df.columns:
            current_df = df[df["end_date"].isna()]
            if not current_df.empty:
                return current_df.sort_values(
                    ["begin_date", "ann_date"], ascending=[False, False]
                ).reset_index(drop=True)

        sort_cols = [col for col in ["end_date", "ann_date", "begin_date"] if col in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols, ascending=False)
        return df.head(1).reset_index(drop=True)

    def get_manager_related_records(self, manager_name: str) -> Optional[pd.DataFrame]:
        """Get related fund manager records by manager name."""
        try:
            if not manager_name or not manager_name.strip():
                return None

            df = self._request("fund_manager", name=manager_name.strip())
            if df is None or df.empty:
                return None

            for col in ["ann_date", "begin_date", "end_date"]:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce")
            return df
        except Exception as e:
            print(f"鑾峰彇鍩洪噾缁忕悊鍏宠仈璁板綍澶辫触: {e}")
            return None


if __name__ == "__main__":
    pass
