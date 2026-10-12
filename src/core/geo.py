"""地理计算：到 NTU 的直线距离 (haversine) + OneMap 地理编码（带缓存）。"""

from __future__ import annotations

import math
from typing import Optional

import config
from src.core import client
from src.core.utils import load_json, log, save_json

_cache: Optional[dict] = None


def haversine_km(lat: float, lng: float,
                 ref_lat: float = config.NTU_LAT,
                 ref_lng: float = config.NTU_LNG) -> float:
    """两点间直线（大圆）距离，单位 km。"""
    r = 6371.0088
    p1, p2 = math.radians(lat), math.radians(ref_lat)
    dphi = math.radians(ref_lat - lat)
    dlmb = math.radians(ref_lng - lng)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return round(2 * r * math.asin(math.sqrt(a)), 3)


def distance_to_ntu(lat: Optional[float], lng: Optional[float]) -> Optional[float]:
    if lat is None or lng is None:
        return None
    return haversine_km(float(lat), float(lng))


# --------------------------------------------------------------------------
# OneMap 地理编码 (新加坡官方免费 API，无需 key)
# --------------------------------------------------------------------------
def _load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = load_json(config.GEOCODE_CACHE, default={}) or {}
    return _cache


def _save_cache() -> None:
    if _cache is not None:
        save_json(_cache, config.GEOCODE_CACHE)


def geocode(query: str) -> Optional[tuple[float, float]]:
    """把楼盘名 / 地址 / 邮编解析成 (lat, lng)。失败返回 None。"""
    if not query or not query.strip():
        return None
    key = query.strip().lower()
    cache = _load_cache()
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None

    resp = client.get(
        config.ONEMAP_SEARCH,
        params={"searchVal": query, "returnGeom": "Y",
                "getAddrDetails": "N", "pageNum": 1},
        headers=client.build_headers(),
        expect_json=True,
    )
    result = None
    if resp is not None and resp.status_code == 200:
        try:
            data = resp.json()
            if data.get("found", 0) > 0 and data.get("results"):
                top = data["results"][0]
                result = (float(top["LATITUDE"]), float(top["LONGITUDE"]))
        except Exception as exc:  # noqa: BLE001
            log(f"[geo] OneMap 解析失败 '{query}': {exc}")

    cache[key] = list(result) if result else None
    _save_cache()
    import time as _t
    _t.sleep(0.25)  # OneMap 较宽松，轻量限速即可
    return result


def geocode_distance(query: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """返回 (lat, lng, distance_km)；无法定位则三者皆 None。"""
    coords = geocode(query)
    if not coords:
        return None, None, None
    lat, lng = coords
    return lat, lng, distance_to_ntu(lat, lng)
