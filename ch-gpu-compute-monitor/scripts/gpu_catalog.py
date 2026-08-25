"""Canonical GPU SKU 映射 —— 把各平台五花八门的原始标识收敛到一套 id。

只做查表，不做模糊匹配。模糊匹配在这里是有害的："H100" 前缀能同时命中
SXM / NVL / PCIe 三个价差 35% 的 SKU，一次误配就把价格中枢污染了，而且
错得很安静。所以别名必须在 config/gpu_catalog.yaml 里显式登记；表里没有的
原始标识记进 unmapped 交给模型看，不猜、不丢。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "gpu_catalog.yaml"


class GpuCatalog:
    def __init__(self, config_path: Optional[Path] = None) -> None:
        raw = yaml.safe_load(Path(config_path or CONFIG_PATH).read_text(encoding="utf-8"))
        self.models: List[Dict] = raw.get("models") or []
        self.watchlist: List[Dict] = raw.get("watchlist") or []
        self.generation_pairs: List[Dict] = raw.get("generation_pairs") or []
        # (source, raw_id) -> canonical id
        self._alias: Dict[str, str] = {}
        # source -> 原始大小写的标识列表（构造查询时要原样传给平台）
        self._raw_by_source: Dict[str, List[str]] = {}
        for entry in self.models + self.watchlist:
            for source, names in (entry.get("aliases") or {}).items():
                bucket = self._raw_by_source.setdefault(source.lower(), [])
                for name in names:
                    self._alias[self._key(source, name)] = entry["id"]
                    if name not in bucket:
                        bucket.append(name)
        self._primary = [m["id"] for m in self.models]
        self._watch = [w["id"] for w in self.watchlist]
        self._labels = {m["id"]: m.get("label", m["id"]) for m in self.models}

    @staticmethod
    def _key(source: str, raw_id: str) -> str:
        return f"{source.lower()}::{str(raw_id).strip().lower()}"

    def resolve(self, source: str, raw_id: Optional[str]) -> Optional[str]:
        """原始标识 → canonical id；认不出返回 None（调用方须记进 unmapped）。"""
        if not raw_id:
            return None
        return self._alias.get(self._key(source, raw_id))

    def source_aliases(self, source: str, primary_only: bool = False) -> List[str]:
        """该源下已登记的原始标识（保留平台原始大小写），用于构造只拉目标 SKU 的查询。"""
        names = list(self._raw_by_source.get(source.lower(), []))
        if not primary_only:
            return names
        return [n for n in names if self.resolve(source, n) in self._primary]

    @property
    def primary_models(self) -> List[str]:
        """MVP 三型号，首页同屏的那三个。"""
        return list(self._primary)

    @property
    def all_models(self) -> List[str]:
        return list(self._primary) + list(self._watch)

    def label(self, model_id: str) -> str:
        return self._labels.get(model_id, model_id)

    def is_primary(self, model_id: str) -> bool:
        return model_id in self._primary


_DEFAULT: Optional[GpuCatalog] = None


def load_catalog() -> GpuCatalog:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = GpuCatalog()
    return _DEFAULT
