#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""巨潮资讯公告标题规则分类与过滤。"""

from __future__ import annotations

import re
from typing import Any


def _hit(title: str, patterns: list[str]) -> bool:
    """检查标题是否命中任一正则。"""
    for pattern in patterns:
        if re.search(pattern, title, flags=re.IGNORECASE):
            return True
    return False


INQUIRY_REPLY = [
    r"问询.*回复",
    r"回复.*问询",
    r"问询函回复",
    r"审核问询.*回复",
    r"反馈意见.*回复",
]
ABNORMAL_VOLATILITY = [r"股价异常波动", r"股票交易异常波动"]
REGULATORY = [r"监管函", r"关注函", r"警示函", r"监管工作函", r"纪律处分"]
CAPITAL_EMPLOYEE = [r"员工持股计划"]
CAPITAL_PRIVATE_OFFER = [r"向特定对象发行", r"特定对象发行", r"定向增发"]
CAPITAL_EQUITY_INCENTIVE = [r"股权激励", r"限制性股票", r"股票期权", r"激励计划"]
INCREASE_HOLD = [r"增持", r"拟增持", r"增持计划"]
DECREASE_HOLD = [r"减持", r"拟减持", r"减持计划", r"减持股份"]
DECREASE_PROGRESS = [
    r"减持进展",
    r"减持进度",
    r"减持计划完成",
    r"减持时间过半",
    r"减持计划时间过半",
    r"减持数量过半",
    r"减持计划届满",
    r"实施进展",
]
MAJOR_COOPERATION = [r"重大合作", r"战略合作", r"合作框架", r"签署.*协议", r"投资.*项目", r"项目投资", r"中标", r"中选"]
QUICK_REPORT = [r"业绩快报", r"季度业绩快报", r"半年度业绩快报", r"年度业绩快报"]


def classify_cninfo_fulltext(title: str | None) -> dict[str, Any]:
    """按公告标题规则分类，并给出是否排除。"""
    normalized_title = (title or "").strip()

    if _hit(normalized_title, INQUIRY_REPLY):
        if _hit(normalized_title, ABNORMAL_VOLATILITY):
            return {
                "category": "上市公司公开信息",
                "subcategory": "其他",
                "rule_id": "cninfo.fulltext.excluded.abnormal_volatility_reply.v1",
                "excluded": True,
                "exclude_reason": "问询回复中的股价或股票交易异常波动公告",
                "tags": ["问询回复", "排除"],
            }
        return {
            "category": "上市公司公开信息",
            "subcategory": "问询回复",
            "rule_id": "cninfo.fulltext.inquiry_reply.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["问询回复"],
        }

    if _hit(normalized_title, REGULATORY):
        return {
            "category": "上市公司公开信息",
            "subcategory": "监管函",
            "rule_id": "cninfo.fulltext.regulatory_letter.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["监管函"],
        }

    if _hit(normalized_title, CAPITAL_EMPLOYEE):
        return {
            "category": "上市公司公开信息",
            "subcategory": "资本运作-员工持股计划",
            "rule_id": "cninfo.fulltext.capital.employee_stock_plan.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["资本运作", "员工持股计划"],
        }

    if _hit(normalized_title, CAPITAL_PRIVATE_OFFER):
        return {
            "category": "上市公司公开信息",
            "subcategory": "资本运作-特定对象发行",
            "rule_id": "cninfo.fulltext.capital.private_offering.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["资本运作", "特定对象发行"],
        }

    if _hit(normalized_title, CAPITAL_EQUITY_INCENTIVE):
        return {
            "category": "上市公司公开信息",
            "subcategory": "资本运作-股权激励",
            "rule_id": "cninfo.fulltext.capital.equity_incentive.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["资本运作", "股权激励"],
        }

    if _hit(normalized_title, INCREASE_HOLD):
        return {
            "category": "上市公司公开信息",
            "subcategory": "增持",
            "rule_id": "cninfo.fulltext.increase_hold.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["增持"],
        }

    if _hit(normalized_title, DECREASE_HOLD):
        if _hit(normalized_title, DECREASE_PROGRESS):
            return {
                "category": "上市公司公开信息",
                "subcategory": "其他",
                "rule_id": "cninfo.fulltext.excluded.decrease_progress.v1",
                "excluded": True,
                "exclude_reason": "减持进度或实施进展类公告",
                "tags": ["减持", "排除"],
            }
        return {
            "category": "上市公司公开信息",
            "subcategory": "减持",
            "rule_id": "cninfo.fulltext.decrease_hold.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["减持"],
        }

    if _hit(normalized_title, MAJOR_COOPERATION):
        return {
            "category": "上市公司公开信息",
            "subcategory": "重大合作/投资项目",
            "rule_id": "cninfo.fulltext.major_cooperation_project.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["合作", "投资项目"],
        }

    if _hit(normalized_title, QUICK_REPORT):
        return {
            "category": "上市公司公开信息",
            "subcategory": "快报",
            "rule_id": "cninfo.fulltext.quick_report.v1",
            "excluded": False,
            "exclude_reason": "",
            "tags": ["快报", "业绩快报"],
        }

    return {
        "category": "上市公司公开信息",
        "subcategory": "其他",
        "rule_id": "cninfo.fulltext.other.v1",
        "excluded": False,
        "exclude_reason": "",
        "tags": ["其他"],
    }
