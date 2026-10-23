#!/usr/bin/env python3
"""Environment doctor for the A-share research skills.

Two commands, both printing JSON for the model to read:

    python3 scripts/env_doctor.py setup [--install-deps [--python PATH | --no-venv] [--index-url URL]]
                                        [--tushare-token TOKEN | --tushare-token-stdin]
                                        [--db sqlite|postgresql] [--sqlite-dir DIR] [--pg-url URL]
    <CH_SKILLS_PYTHON> scripts/env_doctor.py check [--skills a-stock-earnings-forecast,...] [--no-network]

`setup --install-deps` creates a dedicated interpreter (~/.ch-skills/venv) and
installs the skills' packages from the Tsinghua PyPI mirror, then records it as
CH_SKILLS_PYTHON — agent hosts often put a managed `python3` first on PATH that
lacks these packages, so every skill script must run with this interpreter.
`setup` also writes the per-user config file (~/.ch-skills/config.env, or
$CH_SKILLS_CONFIG) that db_core and the Tushare clients read when the
environment is silent. This script itself is stdlib-only: any Python 3.9+ can
run it.

`check` reports which interpreter the skills should use and whether it has the
packages, which Tushare endpoints the token can call, whether the database
backend opens, and whether the data sources are reachable, then maps that onto
each skill's capability.

Facts only: which endpoint answered, which import failed. Explaining the
consequences to the user is the model's job.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import platform
import sqlite3
import stat
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

CONFIG_KEYS = ("CH_SKILLS_PYTHON", "TUSHARE_TOKEN", "ALPHA_DB_BACKEND", "ALPHA_SQLITE_DIR", "ALPHA_PG_URL")
DEFAULT_SQLITE_DIR = os.path.join("~", ".ch-skills", "db")
DEFAULT_VENV = os.path.join("~", ".ch-skills", "venv")
TSINGHUA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# import name -> pip package
PIP_NAME = {
    "tushare": "tushare", "pandas": "pandas", "requests": "requests",
    "fitz": "PyMuPDF", "PyPDF2": "PyPDF2", "psycopg2": "psycopg2-binary",
}

# Per skill: required imports, Tushare endpoints and what each one feeds.
# `required_endpoints` empty = the skill still produces output without Tushare.
SKILLS: Dict[str, Dict[str, Any]] = {
    "a-stock-earnings-forecast": {
        "imports": ["requests", "pandas", "tushare", "fitz", "PyPDF2"],
        "network": ["cninfo"],
        "required_endpoints": [],
        "endpoints": {
            "forecast": "预告差异校验与 IPO 内嵌预测兜底",
            "income": "单季同比/环比拆解与 trailing 营收",
            "daily": "股价断层与 K 线",
            "adj_factor": "K 线前复权",
            "daily_basic": "总市值与年化 PE",
            "stock_basic": "行业归类与产业综述",
            "trade_cal": "交易日定位（断层、市值日期）",
        },
    },
    "a-stock-quarterly-report": {
        "imports": ["requests", "pandas", "tushare", "fitz", "PyPDF2"],
        "network": ["cninfo"],
        "required_endpoints": ["disclosure_date", "income", "fina_indicator", "cashflow", "balancesheet"],
        "endpoints": {
            "disclosure_date": "发现已披露公司（披露日历）",
            "income": "利润表：营收/归母/扣非",
            "fina_indicator": "毛利率、ROE 等财务指标",
            "cashflow": "经营现金流覆盖",
            "balancesheet": "合同负债/应收/存货等前瞻信号",
            "express": "业绩快报兑现度参照",
            "forecast": "业绩预告兑现度参照",
            "daily": "股价断层与 K 线",
            "adj_factor": "K 线前复权",
            "daily_basic": "总市值与 PE",
            "stock_basic": "行业归类",
            "trade_cal": "交易日定位",
        },
    },
    "a-stock-earnings-review": {
        "imports": ["requests"],
        "network": ["sina", "eastmoney"],
        "required_endpoints": [],
        "endpoints": {},
    },
    "cninfo-announcement-search": {
        "imports": ["requests"], "network": ["cninfo"],
        "required_endpoints": [], "endpoints": {},
    },
    "chstock-interactive-qa": {
        "imports": ["requests"], "network": ["p5w"],
        "required_endpoints": [], "endpoints": {},
    },
}
DEFAULT_SKILLS = list(SKILLS)


def config_path() -> str:
    return os.path.expanduser(
        os.environ.get("CH_SKILLS_CONFIG") or os.path.join("~", ".ch-skills", "config.env")
    )


def read_config(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def effective(key: str, cfg: Dict[str, str]) -> Tuple[str, str]:
    """(value, source) with the same precedence the skills use: env > cwd/.env > config file."""
    value = os.environ.get(key, "").strip()
    if value:
        return value, "env"
    if key == "TUSHARE_TOKEN":
        local = read_config(os.path.join(os.getcwd(), ".env")).get(key, "")
        if local:
            return local, "cwd/.env"
    if cfg.get(key):
        return cfg[key], "config_file"
    return "", "unset"


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if len(token) > 10 else "***"


def venv_python(venv_dir: str) -> str:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return os.path.join(os.path.expanduser(venv_dir), *sub)


def pip_command(python: str, packages: List[str], index_url: str = TSINGHUA_INDEX) -> List[str]:
    return [python, "-m", "pip", "install", "-i", index_url, *packages]


def all_packages(backend: str = "sqlite") -> List[str]:
    names = sorted({n for spec in SKILLS.values() for n in spec["imports"]})
    if backend == "postgresql":
        names.append("psycopg2")
    return [PIP_NAME[n] for n in names]


def target_python(cfg: Dict[str, str]) -> Tuple[str, str]:
    """(interpreter the skill scripts should run with, where that choice came from)."""
    configured, source = effective("CH_SKILLS_PYTHON", cfg)
    if configured and os.path.exists(os.path.expanduser(configured)):
        return os.path.expanduser(configured), source
    return sys.executable, "running_interpreter"


def missing_modules(python: str, names: List[str]) -> Optional[List[str]]:
    """Modules `python` cannot import; None when that interpreter cannot be run."""
    if os.path.realpath(python) == os.path.realpath(sys.executable):
        return [n for n in names if importlib.util.find_spec(n) is None]
    code = ("import importlib.util, json, sys; "
            "print(json.dumps([n for n in sys.argv[1:] if importlib.util.find_spec(n) is None]))")
    try:
        out = subprocess.run([python, "-c", code, *names], capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout.strip() or "null")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_imports(python: str, names: List[str]) -> Dict[str, Any]:
    missing = missing_modules(python, names)
    if missing is None:
        return {"ok": False, "missing": list(names), "error": f"无法运行解释器 {python}"}
    return {"ok": not missing, "missing": missing}


def check_db(cfg: Dict[str, str]) -> Dict[str, Any]:
    backend, source = effective("ALPHA_DB_BACKEND", cfg)
    backend = (backend or "postgresql").lower()
    out: Dict[str, Any] = {"backend": backend, "backend_source": source if source != "unset" else "default"}
    if backend == "sqlite":
        raw_dir, _ = effective("ALPHA_SQLITE_DIR", cfg)
        db_dir = os.path.expanduser(raw_dir or ".")
        out["sqlite_dir"] = db_dir
        try:
            os.makedirs(db_dir, exist_ok=True)
            conn = sqlite3.connect(os.path.join(db_dir, "alpha.db"))
            conn.execute("SELECT 1")
            conn.close()
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001
            out.update(ok=False, error=str(exc)[:160])
        return out
    url, url_source = effective("ALPHA_PG_URL", cfg)
    url = url or os.environ.get("DATABASE_URL") or "postgresql://alpha_user:alpha_pass@/alpha_data?host=/tmp"
    out["pg_url_source"] = url_source if url_source != "unset" else "default"
    if importlib.util.find_spec("psycopg2") is None:
        out.update(ok=False, error="psycopg2 未安装")
        return out
    try:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(url, connect_timeout=5)
        conn.close()
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, error=str(exc).strip().splitlines()[0][:160])
    return out


def _classify_error(msg: str) -> str:
    low = msg.lower()
    if "权限" in msg or "没有接口" in msg or "permission" in low:
        return "no_permission"
    if "token" in low and ("invalid" in low or "不对" in msg or "错误" in msg):
        return "invalid_token"
    if "每分钟" in msg or "频率" in msg or "rate" in low:
        return "rate_limited"
    return "error"


def _probe_calls(today: dt.date) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    recent = (today - dt.timedelta(days=10)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    year_ago = f"{today.year - 1}0101"
    last_q = f"{today.year - 1}1231"
    code = "000001.SZ"
    return {
        "trade_cal": ("trade_cal", {"exchange": "SSE", "start_date": recent, "end_date": end}),
        "stock_basic": ("stock_basic", {"ts_code": code, "fields": "ts_code,name,industry"}),
        "daily": ("daily", {"ts_code": code, "start_date": recent, "end_date": end}),
        "adj_factor": ("adj_factor", {"ts_code": code, "start_date": recent, "end_date": end}),
        "daily_basic": ("daily_basic", {"ts_code": code, "start_date": recent, "end_date": end,
                                        "fields": "ts_code,trade_date,total_mv"}),
        "income": ("income", {"ts_code": code, "start_date": year_ago, "end_date": end,
                              "fields": "ts_code,end_date,n_income_attr_p"}),
        "fina_indicator": ("fina_indicator", {"ts_code": code, "period": last_q}),
        "cashflow": ("cashflow", {"ts_code": code, "start_date": year_ago, "end_date": end,
                                  "fields": "ts_code,end_date,n_cashflow_act"}),
        "balancesheet": ("balancesheet", {"ts_code": code, "start_date": year_ago, "end_date": end,
                                          "fields": "ts_code,end_date,total_assets"}),
        "forecast": ("forecast", {"ts_code": code, "start_date": year_ago, "end_date": end}),
        "express": ("express", {"ts_code": code, "start_date": year_ago, "end_date": end}),
        "disclosure_date": ("disclosure_date", {"end_date": last_q, "ts_code": code}),
    }


def check_tushare(token: str, endpoints: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"token_present": bool(token), "endpoints": {}}
    if not token:
        return out
    if importlib.util.find_spec("tushare") is None:
        out["error"] = "当前解释器没有 tushare，无法探测接口权限：用 python.target 重跑 check"
        return out
    import tushare as ts  # type: ignore

    pro = ts.pro_api(token)
    calls = _probe_calls(dt.date.today())
    for name in endpoints:
        api, kwargs = calls[name]
        try:
            df = getattr(pro, api)(**kwargs)
            out["endpoints"][name] = {"status": "ok", "rows": 0 if df is None else int(len(df))}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).strip()
            out["endpoints"][name] = {"status": _classify_error(msg), "message": msg[:160]}
    statuses = {v["status"] for v in out["endpoints"].values()}
    out["token_valid"] = "invalid_token" not in statuses
    return out


def check_network(targets: List[str]) -> Dict[str, Any]:
    if importlib.util.find_spec("requests") is None:
        return {t: {"ok": None, "error": "当前解释器没有 requests，未测网络：用 python.target 重跑 check"}
                for t in targets}
    import requests  # type: ignore

    probes = {
        "cninfo": ("POST", "http://www.cninfo.com.cn/new/hisAnnouncement/query",
                   {"data": {"pageNum": 1, "pageSize": 1, "column": "szse", "tabName": "fulltext"}}),
        "p5w": ("GET", "https://ir.p5w.net/", {}),
        # K 线（价格交叉验证）与估值历史的公开来源
        "sina": ("GET", "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                 {"params": {"symbol": "sh000300", "scale": 240, "ma": "no", "datalen": 1}}),
        "eastmoney": ("GET", "https://datacenter-web.eastmoney.com/api/data/v1/get",
                      {"params": {"reportName": "RPT_VALUEANALYSIS_DET", "columns": "TRADE_DATE",
                                  "filter": '(SECURITY_CODE="600519")', "pageSize": 1}}),
    }
    out: Dict[str, Any] = {}
    for target in targets:
        method, url, extra = probes[target]
        try:
            resp = requests.request(method, url, timeout=10,
                                    headers={"User-Agent": "Mozilla/5.0"}, **extra)
            out[target] = {"ok": resp.status_code < 500, "http_status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            out[target] = {"ok": False, "error": str(exc)[:160]}
    return out


def skill_capability(skill: str, imports: Dict[str, Any], db: Dict[str, Any],
                     tushare: Dict[str, Any], network: Dict[str, Any]) -> Dict[str, Any]:
    spec = SKILLS[skill]
    blockers: List[str] = []
    if imports[skill]["missing"]:
        blockers.append("missing_python_packages")
    for target in spec["network"]:
        if network and network.get(target, {}).get("ok") is False:
            blockers.append(f"network_unreachable:{target}")
    endpoint_status = tushare.get("endpoints", {})
    unavailable = {
        name: purpose for name, purpose in spec["endpoints"].items()
        if endpoint_status.get(name, {}).get("status") != "ok"
    }
    missing_required = [e for e in spec["required_endpoints"] if e in unavailable]
    if missing_required:
        blockers.append("tushare_required_endpoints:" + ",".join(missing_required))
    if spec["endpoints"] and not db.get("ok"):
        blockers.append("database_unavailable")  # ledgers (verdict/HTML) need it
    if blockers:
        level = "blocked"
    elif not spec["endpoints"] or not unavailable:
        level = "full"
    elif not tushare.get("token_present") or tushare.get("token_valid") is False:
        level = "cninfo_only"
    else:
        level = "partial"
    return {
        "level": level,
        "blockers": blockers,
        "tushare_unavailable_features": unavailable,
    }


def cmd_check(args: argparse.Namespace) -> int:
    skills = [s.strip() for s in (args.skills or ",".join(DEFAULT_SKILLS)).split(",") if s.strip()]
    unknown = [s for s in skills if s not in SKILLS]
    if unknown:
        print(json.dumps({"error": f"unknown skills: {unknown}", "known": DEFAULT_SKILLS},
                         ensure_ascii=False))
        return 2
    path = config_path()
    cfg = read_config(path)
    token, token_source = effective("TUSHARE_TOKEN", cfg)
    target, target_source = target_python(cfg)
    imports = {s: check_imports(target, SKILLS[s]["imports"]) for s in skills}
    db = check_db(cfg)
    endpoints = sorted({e for s in skills for e in SKILLS[s]["endpoints"]})
    tushare = check_tushare(token, endpoints) if not args.no_network else {
        "token_present": bool(token), "endpoints": {}, "skipped": "--no-network"}
    tushare["token_source"] = token_source
    if token:
        tushare["token_masked"] = mask(token)
    targets = sorted({t for s in skills for t in SKILLS[s]["network"]})
    network = {} if args.no_network else check_network(targets)
    all_missing = sorted({m for v in imports.values() for m in v["missing"]})
    running_is_target = os.path.realpath(target) == os.path.realpath(sys.executable)
    report = {
        "python": {
            "target": target,
            "target_source": target_source,
            "running": sys.executable,
            "running_version": platform.python_version(),
            "running_is_target": running_is_target,
            "ok": sys.version_info >= (3, 9),
            "note": None if target_source != "running_interpreter" else
            "还没有专用解释器：运行 setup --install-deps 建 ~/.ch-skills/venv 并从清华源装依赖",
        },
        "platform": platform.system(),
        "config_file": {"path": path, "exists": os.path.exists(path),
                        "keys": sorted(k for k in cfg if k in CONFIG_KEYS)},
        "packages": {
            "interpreter": target,
            "missing": all_missing,
            "fix": ("python3 scripts/env_doctor.py setup --install-deps" if all_missing else None),
            "pip_install": (" ".join(pip_command(target, [PIP_NAME[m] for m in all_missing]))
                            if all_missing else None),
        },
        "database": db,
        "tushare": tushare,
        "network": network,
        "skills": {s: skill_capability(s, imports, db, tushare, network) for s in skills},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def install_deps(args: argparse.Namespace, backend: str) -> Dict[str, Any]:
    """Create/choose the skills' interpreter and pip-install from the mirror."""
    if args.python:
        python = os.path.expanduser(args.python)
    elif args.no_venv:
        python = sys.executable
    else:
        venv_dir = os.path.expanduser(args.venv or DEFAULT_VENV)
        python = venv_python(venv_dir)
        if not os.path.exists(python):
            made = subprocess.run([sys.executable, "-m", "venv", venv_dir],
                                  capture_output=True, text=True, timeout=300)
            if made.returncode != 0 or not os.path.exists(python):
                return {"ok": False, "step": "venv", "python": sys.executable,
                        "error": (made.stderr or made.stdout).strip()[-400:],
                        "hint": "当前 Python 不能建虚拟环境：改用 --python <已装好的 Python 路径> 或 --no-venv"}
    packages = all_packages(backend)
    cmd = pip_command(python, packages, args.index_url)
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "step": "pip", "python": python, "error": str(exc)[:400]}
    result = {"ok": run.returncode == 0, "step": "pip", "python": python,
              "command": " ".join(cmd), "packages": packages}
    if run.returncode != 0:
        result["error"] = (run.stderr or run.stdout).strip()[-600:]
        if "externally-managed" in result["error"]:
            result["hint"] = "该解释器受系统管理、禁止直接 pip 安装：去掉 --no-venv/--python，改用默认虚拟环境"
    else:
        still = missing_modules(python, sorted({n for s in SKILLS.values() for n in s["imports"]}))
        result["missing_after_install"] = still
        result["ok"] = still == []
    return result


def cmd_setup(args: argparse.Namespace) -> int:
    path = config_path()
    cfg = read_config(path)
    token = args.tushare_token
    if args.tushare_token_stdin:
        token = sys.stdin.readline().strip()
    if token:
        cfg["TUSHARE_TOKEN"] = token
    if args.db:
        cfg["ALPHA_DB_BACKEND"] = args.db
    elif "ALPHA_DB_BACKEND" not in cfg:
        # Standalone installs have no PostgreSQL; SQLite keeps the ledgers local.
        cfg["ALPHA_DB_BACKEND"] = "sqlite"
    if cfg["ALPHA_DB_BACKEND"] == "sqlite":
        cfg["ALPHA_SQLITE_DIR"] = args.sqlite_dir or cfg.get("ALPHA_SQLITE_DIR") or DEFAULT_SQLITE_DIR
    if args.pg_url:
        cfg["ALPHA_PG_URL"] = args.pg_url
    deps: Optional[Dict[str, Any]] = None
    if args.install_deps:
        deps = install_deps(args, cfg["ALPHA_DB_BACKEND"])
        if deps["ok"]:
            cfg["CH_SKILLS_PYTHON"] = deps["python"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["# Written by chstock-env-setup. Environment variables override these values."]
    lines += [f"{k}={cfg[k]}" for k in CONFIG_KEYS if cfg.get(k)]
    lines += [f"{k}={v}" for k, v in cfg.items() if k not in CONFIG_KEYS and v]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # token inside: owner-only
    except OSError:
        pass
    shown = {k: (mask(v) if k == "TUSHARE_TOKEN" else v) for k, v in cfg.items() if k in CONFIG_KEYS}
    python = cfg.get("CH_SKILLS_PYTHON") or "python3"
    out: Dict[str, Any] = {"written": path, "values": shown,
                           "next": f"{python} scripts/env_doctor.py check"}
    if deps is not None:
        out["install_deps"] = deps
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if deps is None or deps["ok"] else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="A 股研究技能环境自检与配置")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="自检依赖、Tushare 接口权限、数据库与网络")
    c.add_argument("--skills", help="逗号分隔；默认全部：" + ",".join(DEFAULT_SKILLS))
    c.add_argument("--no-network", action="store_true", help="不发网络请求（只查本地）")
    s = sub.add_parser("setup", help="写入 ~/.ch-skills/config.env")
    s.add_argument("--tushare-token")
    s.add_argument("--tushare-token-stdin", action="store_true", help="从标准输入读 token（不进命令行历史）")
    s.add_argument("--db", choices=["sqlite", "postgresql"])
    s.add_argument("--sqlite-dir", help=f"默认 {DEFAULT_SQLITE_DIR}")
    s.add_argument("--pg-url")
    s.add_argument("--install-deps", action="store_true",
                   help=f"建专用虚拟环境（默认 {DEFAULT_VENV}）并从清华源安装全部依赖，记录为 CH_SKILLS_PYTHON")
    s.add_argument("--python", help="不建虚拟环境，装进这个已有的解释器")
    s.add_argument("--no-venv", action="store_true", help="装进当前运行本脚本的解释器")
    s.add_argument("--venv", help=f"虚拟环境目录（默认 {DEFAULT_VENV}）")
    s.add_argument("--index-url", default=TSINGHUA_INDEX, help="PyPI 镜像（默认清华源）")
    args = ap.parse_args(argv)
    return cmd_check(args) if args.cmd == "check" else cmd_setup(args)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
