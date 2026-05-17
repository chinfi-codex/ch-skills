#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观数据抓取模块

只负责获取和聚合原始数据，不包含分析、波动率计算或报告生成逻辑。
"""

import io
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# 金十 MCP 客户端可选依赖。缺失时优雅降级到 AlphaVantage / Stooq。
try:
    from jin10_mcp import Jin10McpClient, Jin10McpError  # type: ignore
    _JIN10_AVAILABLE = True
    _JIN10_IMPORT_ERROR = ""
except Exception as _jin10_import_exc:  # noqa: BLE001
    Jin10McpClient = None  # type: ignore[assignment]
    Jin10McpError = Exception  # type: ignore[assignment]
    _JIN10_AVAILABLE = False
    _JIN10_IMPORT_ERROR = str(_jin10_import_exc)

# 金十 MCP 默认关注品种代码与内部命名映射
JIN10_DEFAULT_CODES = ["UKOIL", "XAUUSD", "NGAS", "USDCNH"]
JIN10_CODE_MAP = {
    "UKOIL": "BRENT",
    "XAUUSD": "GOLD",
    "NGAS": "NATURAL_GAS",
    "USDCNH": "USD_CNY",
}

ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN")

STOOQ_BACKUP_URLS = {
    "BTC": "https://stooq.com/q/l/?s=btcusd&i=d",
    "GOLD": "https://stooq.com/q/l/?s=xauusd&i=d",
    "WTI": "https://stooq.com/q/l/?s=cl.f&i=d",
    "BRENT": "https://stooq.com/q/l/?s=bz.f&i=d",
    "NATURAL_GAS": "https://stooq.com/q/l/?s=ng.f&i=d",
    "NASDAQ_FUTURES": "https://stooq.com/q/l/?s=nq.f&i=d",
}


class AlphaVantageClient:
    """Alpha Vantage 客户端，内置免费额度限流控制。"""

    _last_request_time = 0.0
    _request_count = 0
    _minute_start = 0.0

    @classmethod
    def is_configured(cls) -> bool:
        """检查 Alpha Vantage 是否已配置。"""
        return bool(ALPHAVANTAGE_API_KEY)

    @classmethod
    def _rate_limited_request(cls, url: str, max_retries: int = 3) -> Dict:
        """发送带限流控制的请求。"""
        if not ALPHAVANTAGE_API_KEY:
            logger.warning("ALPHAVANTAGE_API_KEY 未配置，跳过 Alpha Vantage 请求")
            return {"_error": True}

        for attempt in range(max_retries):
            try:
                current_time = time.time()
                if current_time - cls._minute_start >= 60:
                    cls._minute_start = current_time
                    cls._request_count = 0

                if cls._request_count >= 5:
                    sleep_time = max(0, 60 - (current_time - cls._minute_start) + 1)
                    logger.warning("Alpha Vantage 限流，等待 %.0f 秒", sleep_time)
                    time.sleep(sleep_time)
                    cls._minute_start = time.time()
                    cls._request_count = 0

                elapsed = current_time - cls._last_request_time
                if elapsed < 12:
                    time.sleep(12 - elapsed)

                response = requests.get(url, timeout=30)
                cls._last_request_time = time.time()
                cls._request_count += 1

                if response.status_code != 200:
                    logger.error("HTTP %s: %s", response.status_code, response.text)
                    return {}

                data = response.json()
                if "Note" in data or "Information" in data:
                    logger.warning("Alpha Vantage API limit: %s", data)
                    if attempt < max_retries - 1:
                        time.sleep(15)
                        continue
                    return {"_limited": True}

                return data
            except Exception as exc:
                logger.error("Request failed (attempt %s): %s", attempt + 1, exc)
                if attempt < max_retries - 1:
                    time.sleep(5)

        return {"_error": True}

    @classmethod
    def get_usd_cny_rate(cls) -> Optional[float]:
        """获取 USD/CNY 实时汇率。"""
        url = (
            "https://www.alphavantage.co/query?"
            f"function=CURRENCY_EXCHANGE_RATE&from_currency=USD&to_currency=CNY&apikey={ALPHAVANTAGE_API_KEY}"
        )
        data = cls._rate_limited_request(url)
        if data.get("_limited") or data.get("_error"):
            return None

        try:
            rate_data = data.get("Realtime Currency Exchange Rate", {})
            return float(rate_data.get("5. Exchange Rate", 0))
        except Exception as exc:
            logger.error("解析汇率失败: %s", exc)
            return None

    @classmethod
    def get_treasury_yield(cls, maturity: str = "10year") -> Optional[float]:
        """获取美债收益率。"""
        url = (
            "https://www.alphavantage.co/query?"
            f"function=TREASURY_YIELD&interval=daily&maturity={maturity}&apikey={ALPHAVANTAGE_API_KEY}"
        )
        data = cls._rate_limited_request(url)
        if data.get("_limited") or data.get("_error"):
            return None

        try:
            data_list = data.get("data", [])
            if data_list:
                return float(data_list[0].get("value", 0))
            return None
        except Exception as exc:
            logger.error("解析美债收益率失败: %s", exc)
            return None

    @classmethod
    def get_btc_price(cls) -> Optional[float]:
        """获取 BTC/USD 价格。"""
        url = (
            "https://www.alphavantage.co/query?"
            f"function=CURRENCY_EXCHANGE_RATE&from_currency=BTC&to_currency=USD&apikey={ALPHAVANTAGE_API_KEY}"
        )
        data = cls._rate_limited_request(url)
        if data.get("_limited") or data.get("_error"):
            return None

        try:
            rate_data = data.get("Realtime Currency Exchange Rate", {})
            return float(rate_data.get("5. Exchange Rate", 0))
        except Exception as exc:
            logger.error("解析 BTC 价格失败: %s", exc)
            return None

    @classmethod
    def get_commodity_price(cls, commodity: str) -> Optional[float]:
        """获取商品价格。"""
        url = (
            "https://www.alphavantage.co/query?"
            f"function={commodity.upper()}&interval=daily&apikey={ALPHAVANTAGE_API_KEY}"
        )
        data = cls._rate_limited_request(url)
        if data.get("_limited") or data.get("_error"):
            return None

        try:
            data_list = data.get("data", [])
            if data_list:
                return float(data_list[0].get("value", 0))
            return None
        except Exception as exc:
            logger.error("解析商品价格失败: %s", exc)
            return None


class StooqBackupClient:
    """Stooq 备用数据源。"""

    @staticmethod
    def get_price(symbol_key: str) -> Optional[float]:
        """从 Stooq 获取单个品种价格。"""
        url = STOOQ_BACKUP_URLS.get(symbol_key)
        if not url:
            return None

        try:
            df = pd.read_csv(url)
            if not df.empty and "Close" in df.columns:
                return float(df["Close"].iloc[-1])
            return None
        except Exception as exc:
            logger.error("Stooq 获取 %s 失败: %s", symbol_key, exc)
            return None

    @classmethod
    def get_all_prices(cls) -> Dict[str, Optional[float]]:
        """获取全部备用行情。"""
        results: Dict[str, Optional[float]] = {}
        for key in STOOQ_BACKUP_URLS:
            results[key] = cls.get_price(key)
            time.sleep(0.5)
        return results


class TushareMacroClient:
    """Tushare 中国宏观数据客户端。"""

    _pro = None

    @classmethod
    def is_configured(cls) -> bool:
        """检查 Tushare 是否已配置。"""
        return bool(TUSHARE_TOKEN)

    @classmethod
    def _get_pro(cls):
        """获取 Tushare Pro 实例。"""
        if not cls.is_configured():
            logger.warning("TUSHARE_TOKEN 未配置，跳过 Tushare 请求")
            return None

        if cls._pro is None:
            try:
                import tushare as ts

                cls._pro = ts.pro_api(TUSHARE_TOKEN)
            except Exception as exc:
                logger.error("Tushare 初始化失败: %s", exc)
                return None
        return cls._pro

    @classmethod
    def get_latest_macro(cls) -> Dict[str, Dict]:
        """获取最新中国宏观数据。"""
        results: Dict[str, Dict] = {}
        cpi = cls.get_latest_cpi()
        if cpi:
            results["CPI"] = cpi

        ppi = cls.get_latest_ppi()
        if ppi:
            results["PPI"] = ppi

        soci = cls.get_latest_soci()
        if soci:
            results["SOCI"] = soci

        pmi = cls.get_latest_pmi()
        if pmi:
            results["PMI"] = pmi

        return results

    @classmethod
    def get_latest_cpi(cls) -> Optional[Dict]:
        """获取最新 CPI 数据。"""
        pro = cls._get_pro()
        if not pro:
            return None

        try:
            cpi = pro.cn_cpi(limit=1)
            if cpi.empty:
                return None
            return {
                "value": cpi.iloc[0].get("cpi"),
                "yoy": cpi.iloc[0].get("cpi_yoy"),
                "date": cpi.iloc[0].get("month"),
            }
        except Exception as exc:
            logger.error("获取 CPI 失败: %s", exc)
            return None

    @classmethod
    def get_latest_ppi(cls) -> Optional[Dict]:
        """获取最新 PPI 数据。"""
        pro = cls._get_pro()
        if not pro:
            return None

        try:
            ppi = pro.cn_ppi(limit=1)
            if ppi.empty:
                return None
            return {
                "value": ppi.iloc[0].get("ppi"),
                "yoy": ppi.iloc[0].get("ppi_yoy"),
                "date": ppi.iloc[0].get("month"),
            }
        except Exception as exc:
            logger.error("获取 PPI 失败: %s", exc)
            return None

    @classmethod
    def get_latest_soci(cls) -> Optional[Dict]:
        """获取最新社融数据。"""
        pro = cls._get_pro()
        if not pro:
            return None

        try:
            soci = pro.cn_soci(limit=1)
            if soci.empty:
                return None
            return {
                "value": soci.iloc[0].get("total"),
                "date": soci.iloc[0].get("month"),
            }
        except Exception as exc:
            logger.error("获取社融失败: %s", exc)
            return None

    @classmethod
    def get_latest_pmi(cls) -> Optional[Dict]:
        """获取最新 PMI 数据。"""
        pro = cls._get_pro()
        if not pro:
            return None

        try:
            pmi = pro.cn_pmi(limit=1)
            if pmi.empty:
                return None
            return {
                "value": pmi.iloc[0].get("pmi"),
                "date": pmi.iloc[0].get("month"),
            }
        except Exception as exc:
            logger.error("获取 PMI 失败: %s", exc)
            return None


class Jin10McpDataClient:
    """基于金十 MCP 的行情客户端。"""

    @staticmethod
    def is_available() -> bool:
        """探测金十 MCP 是否可用(依赖 + 鉴权)。"""
        if not _JIN10_AVAILABLE or Jin10McpClient is None:
            return False
        if not os.environ.get("JIN10_AUTH_TOKEN"):
            return False
        try:
            client = Jin10McpClient()
        except Exception:
            return False
        try:
            client.ensure_initialized()
            return True
        except Exception:
            return False
        finally:
            client.close()

    @staticmethod
    def fetch_quotes(codes: Optional[list] = None) -> Dict[str, Dict[str, Any]]:
        """
        获取金十报价。每个 code 单独 try-except,失败时该品种返回 _error。

        Returns:
            {内部命名: {name, code, time, open, close, high, low, volume,
                       ups_price, ups_percent, source}}
        """
        if not _JIN10_AVAILABLE or Jin10McpClient is None:
            return {}
        target_codes = codes or JIN10_DEFAULT_CODES
        try:
            client = Jin10McpClient()
        except Exception as exc:
            logger.warning("金十 MCP 初始化失败: %s", exc)
            return {}
        results: Dict[str, Dict[str, Any]] = {}
        try:
            for code in target_codes:
                internal_name = JIN10_CODE_MAP.get(code, code)
                try:
                    payload = client.get_quote(code)
                    data = payload.get("data") or {}
                    results[internal_name] = {
                        "name": data.get("name") or code,
                        "code": data.get("code") or code,
                        "time": data.get("time") or "",
                        "open": data.get("open"),
                        "close": data.get("close"),
                        "high": data.get("high"),
                        "low": data.get("low"),
                        "volume": data.get("volume"),
                        "ups_price": data.get("ups_price"),
                        "ups_percent": data.get("ups_percent"),
                        "source": "jin10_mcp",
                    }
                except Exception as exc:
                    results[internal_name] = {
                        "_error": str(exc),
                        "source": "jin10_mcp",
                    }
        finally:
            client.close()
        return results


class MacroDataMonitor:
    """宏观数据获取聚合器。"""

    def __init__(self):
        self.av_client = AlphaVantageClient()
        self.stooq_client = StooqBackupClient()
        self.tushare_client = TushareMacroClient()
        self.jin10_client = Jin10McpDataClient()

    def fetch_fx_data(self) -> Dict[str, Optional[float]]:
        """获取汇率数据。"""
        return {"USD_CNY": self.av_client.get_usd_cny_rate()}

    def fetch_us_rates_data(self) -> Dict[str, Optional[float]]:
        """获取美国利率相关数据。"""
        return {"US_TREASURY_10Y": self.av_client.get_treasury_yield("10year")}

    def fetch_crypto_data(self) -> Dict[str, Optional[float]]:
        """获取加密资产数据。"""
        return {"BTC": self.av_client.get_btc_price()}

    def fetch_energy_data(self) -> Dict[str, Optional[float]]:
        """获取能源商品数据。"""
        return {
            "WTI": self.av_client.get_commodity_price("WTI"),
            "BRENT": self.av_client.get_commodity_price("BRENT"),
            "NATURAL_GAS": self.av_client.get_commodity_price("NATURAL_GAS"),
        }

    def fetch_alpha_vantage_market_data(self) -> Dict[str, Optional[float]]:
        """获取 Alpha Vantage 可提供的市场数据。"""
        data: Dict[str, Optional[float]] = {}
        data.update(self.fetch_fx_data())
        data.update(self.fetch_us_rates_data())
        data.update(self.fetch_crypto_data())
        data.update(self.fetch_energy_data())
        return data

    def fetch_backup_market_data(self) -> Dict[str, Optional[float]]:
        """获取 Stooq 备用市场数据。"""
        stooq_data = self.stooq_client.get_all_prices()
        return {
            "BTC": stooq_data.get("BTC"),
            "GOLD": stooq_data.get("GOLD"),
            "WTI": stooq_data.get("WTI"),
            "BRENT": stooq_data.get("BRENT"),
            "NATURAL_GAS": stooq_data.get("NATURAL_GAS"),
            "NASDAQ_FUTURES": stooq_data.get("NASDAQ_FUTURES"),
        }

    def fetch_china_macro_data(self) -> Dict[str, Dict]:
        """获取中国宏观数据。"""
        return self.tushare_client.get_latest_macro()

    def fetch_china_cpi_data(self) -> Dict[str, Dict]:
        """获取中国 CPI 数据。"""
        data = self.tushare_client.get_latest_cpi()
        return {"CPI": data} if data else {}

    def fetch_china_ppi_data(self) -> Dict[str, Dict]:
        """获取中国 PPI 数据。"""
        data = self.tushare_client.get_latest_ppi()
        return {"PPI": data} if data else {}

    def fetch_china_soci_data(self) -> Dict[str, Dict]:
        """获取中国社融数据。"""
        data = self.tushare_client.get_latest_soci()
        return {"SOCI": data} if data else {}

    def fetch_china_pmi_data(self) -> Dict[str, Dict]:
        """获取中国 PMI 数据。"""
        data = self.tushare_client.get_latest_pmi()
        return {"PMI": data} if data else {}

    def fetch_market_data(self, use_backup: bool = False) -> Dict[str, Dict]:
        """
        获取市场数据,三层降级:
        1. 金十 MCP 优先(Brent / Gold / Natural Gas / USD-CNY)
        2. AlphaVantage 补充其他品种(BTC、美债等),或金十失败的品种
        3. 上述两层都失败或 --use-backup 时,Stooq 兜底

        返回结构保持 {"sources": {源名: 状态}, "data": {品种: 价格}}。
        """
        results: Dict[str, Dict] = {"sources": {}, "data": {}}

        if use_backup:
            results["sources"]["stooq_backup"] = "OK"
            results["data"].update(self.fetch_backup_market_data())
            return results

        # 1. 金十 MCP 优先
        if self.jin10_client.is_available():
            jin10_data = self.jin10_client.fetch_quotes()
            jin10_filled = False
            for key, payload in jin10_data.items():
                if "_error" in payload:
                    continue
                close = payload.get("close")
                if close is None:
                    continue
                try:
                    results["data"][key] = float(close)
                    jin10_filled = True
                except (TypeError, ValueError):
                    continue
            results["sources"]["jin10_mcp"] = "OK" if jin10_filled else "ERROR"
        else:
            if not _JIN10_AVAILABLE:
                results["sources"]["jin10_mcp"] = "MISSING_DEPENDENCY"
            elif not os.environ.get("JIN10_AUTH_TOKEN"):
                results["sources"]["jin10_mcp"] = "MISSING_CONFIG"
            else:
                results["sources"]["jin10_mcp"] = "ERROR"

        # 2. AlphaVantage 补充
        if not self.av_client.is_configured():
            results["sources"]["alpha_vantage"] = "MISSING_CONFIG"
        else:
            alpha_data = self.fetch_alpha_vantage_market_data()
            alpha_available = any(value is not None for value in alpha_data.values())
            if alpha_available:
                results["sources"]["alpha_vantage"] = "OK"
                for key, value in alpha_data.items():
                    if value is None or key in results["data"]:
                        continue
                    results["data"][key] = value
            else:
                results["sources"]["alpha_vantage"] = "RATE_LIMITED"

        # 3. Stooq 兜底
        primary_ok = (
            results["sources"].get("jin10_mcp") == "OK"
            or results["sources"].get("alpha_vantage") == "OK"
        )
        if not primary_ok:
            results["sources"]["stooq_backup"] = "OK"
            for key, value in self.fetch_backup_market_data().items():
                if value is None or key in results["data"]:
                    continue
                results["data"][key] = value

        return results

    def get_all_data(self, use_backup: bool = False) -> Dict[str, Dict]:
        """
        获取全部原始数据。

        保留旧方法名，兼容已有调用方。
        """
        results = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": {},
            "data": {},
        }

        market_results = self.fetch_market_data(use_backup=use_backup)
        results["sources"].update(market_results["sources"])
        results["data"].update(market_results["data"])

        macro_data = self.fetch_china_macro_data()
        if macro_data:
            results["sources"]["tushare"] = "OK"
        elif self.tushare_client.is_configured():
            results["sources"]["tushare"] = "ERROR"
        else:
            results["sources"]["tushare"] = "MISSING_CONFIG"
        results["data"]["CHINA_MACRO"] = macro_data

        return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch macro and market evidence as JSON")
    parser.add_argument(
        "dataset",
        nargs="?",
        default="all",
        choices=[
            "all",
            "market",
            "backup-market",
            "china-macro",
            "cpi",
            "ppi",
            "soci",
            "pmi",
            "fx",
            "us-rates",
            "crypto",
            "energy",
        ],
    )
    parser.add_argument("--use-backup", action="store_true", help="Force Stooq backup for market data")
    return parser


def fetch_dataset(dataset: str, use_backup: bool = False) -> Dict:
    monitor = MacroDataMonitor()
    if dataset == "all":
        return monitor.get_all_data(use_backup=use_backup)
    if dataset == "market":
        return monitor.fetch_market_data(use_backup=use_backup)
    if dataset == "backup-market":
        return {"sources": {"stooq_backup": "OK"}, "data": monitor.fetch_backup_market_data()}
    if dataset == "china-macro":
        return {"sources": {"tushare": "OK" if monitor.tushare_client.is_configured() else "MISSING_CONFIG"}, "data": monitor.fetch_china_macro_data()}
    if dataset == "cpi":
        return {"data": monitor.fetch_china_cpi_data()}
    if dataset == "ppi":
        return {"data": monitor.fetch_china_ppi_data()}
    if dataset == "soci":
        return {"data": monitor.fetch_china_soci_data()}
    if dataset == "pmi":
        return {"data": monitor.fetch_china_pmi_data()}
    if dataset == "fx":
        return {"data": monitor.fetch_fx_data()}
    if dataset == "us-rates":
        return {"data": monitor.fetch_us_rates_data()}
    if dataset == "crypto":
        return {"data": monitor.fetch_crypto_data()}
    if dataset == "energy":
        return {"data": monitor.fetch_energy_data()}
    raise ValueError(f"Unsupported dataset: {dataset}")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    args = build_parser().parse_args()
    payload = fetch_dataset(args.dataset, use_backup=args.use_backup)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
