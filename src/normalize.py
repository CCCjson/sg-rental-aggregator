"""把各站点原始房源统一成标准 schema，并补全距离/房型分类/配套设施。"""

from __future__ import annotations

import re
from typing import Optional

import config
from src.core import geo

# 标准字段
SCHEMA_FIELDS = [
    "id", "source", "url", "title", "property_name", "address",
    "lat", "lng", "distance_km", "distance_is_approx",
    "listing_type", "room_category", "bedrooms", "bathrooms",
    "total_rent", "price", "master_price_est", "common_price_est",
    "floor_area_sqft", "furnishing", "amenities", "agent_or_owner",
    "posted_date",
]

# 配套设施关键词 → 标准化标签
AMENITY_PATTERNS = {
    "pool": r"\b(swimming\s*pool|pool|泳池)\b",
    "gym": r"\b(gym|fitness|健身)\b",
    "aircon": r"\b(air[\s-]?con|aircon|a/c|空调)\b",
    "ensuite_bath": r"\b(ensuite|en-suite|attached\s*bath|private\s*bath|独立卫浴)\b",
    "furnished": r"\b(fully\s*furnished|furnished|家具齐全|带家具)\b",
    "near_mrt": r"\b(mrt|subway|地铁|metro)\b",
    "near_bus": r"\b(bus\s*stop|巴士|公交)\b",
    "wifi": r"\b(wifi|wi-fi|internet|宽带)\b",
    "cooking": r"\b(cooking\s*allowed|kitchen|可做饭|厨房)\b",
    "parking": r"\b(car\s*park|parking|停车)\b",
}

# 房型判定
_ROOM_TYPE_PATTERNS = {
    "master": r"\b(master\s*room|master\s*bedroom|主人房|主卧)\b",
    "common": r"\b(common\s*room|common\s*bedroom|普通房|次卧)\b",
    "triple": r"\b(triple|3\s*pax|three\s*pax|三人|3人)\b",
    "double": r"\b(double|sharing|shared\s*room|2\s*pax|two\s*pax|双人|2人|合租)\b",
    "single": r"\b(single|1\s*pax|one\s*pax|单人|独住)\b",
}


def extract_amenities(text: str, extra: Optional[list] = None) -> list[str]:
    text = (text or "").lower()
    if extra:
        text += " " + " ".join(str(x).lower() for x in extra)
    found = [tag for tag, pat in AMENITY_PATTERNS.items()
             if re.search(pat, text, re.IGNORECASE)]
    return found


def classify_room(text: str, listing_type: str) -> str:
    """返回 room_category: master/common/single/double/triple/NA。"""
    if listing_type == "whole_unit":
        return "NA"
    t = (text or "").lower()
    # 占用人数优先（双人/三人间是用户重点）
    for cat in ("triple", "double", "single"):
        if re.search(_ROOM_TYPE_PATTERNS[cat], t, re.IGNORECASE):
            return cat
    for cat in ("master", "common"):
        if re.search(_ROOM_TYPE_PATTERNS[cat], t, re.IGNORECASE):
            return cat
    return "single"  # 默认按单间处理


def normalize_one(raw: dict) -> Optional[dict]:
    out = {f: None for f in SCHEMA_FIELDS}
    out.update({
        "id": raw.get("id"),
        "source": raw.get("source"),
        "url": raw.get("url"),
        "title": raw.get("title"),
        "property_name": raw.get("property_name"),
        "address": raw.get("address"),
        "lat": raw.get("lat"),
        "lng": raw.get("lng"),
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "floor_area_sqft": raw.get("floor_area_sqft"),
        "agent_or_owner": raw.get("agent_or_owner"),
        "posted_date": raw.get("posted_date"),
    })

    listing_type = raw.get("listing_type") or "whole_unit"
    out["listing_type"] = listing_type

    text_blob = " ".join(str(raw.get(k) or "") for k in
                         ("title", "description", "property_name", "address"))

    # 价格 / 总租金
    price = raw.get("price")
    if listing_type == "whole_unit":
        out["total_rent"] = price
        out["price"] = price
    else:
        out["price"] = price
        out["room_category"] = classify_room(text_blob, listing_type)

    if listing_type == "whole_unit":
        out["room_category"] = "NA"

    # 配套设施
    out["amenities"] = extract_amenities(text_blob, raw.get("amenities_raw"))
    out["furnishing"] = "furnished" if "furnished" in out["amenities"] else (
        raw.get("furnishing"))

    # 距离：优先用已有经纬度；否则用 OneMap 地理编码楼盘名/地址
    dist = raw.get("distance_km")
    approx = raw.get("distance_is_approx", False)
    if dist is None:
        if out["lat"] is not None and out["lng"] is not None:
            dist = geo.distance_to_ntu(out["lat"], out["lng"])
            approx = False
        else:
            q = raw.get("property_name") or raw.get("address") or raw.get("title")
            lat, lng, d = geo.geocode_distance(q) if q else (None, None, None)
            if d is not None:
                out["lat"], out["lng"] = lat, lng
                dist, approx = d, True
    out["distance_km"] = dist
    out["distance_is_approx"] = approx
    return out


def normalize_all(raw_records: list[dict]) -> list[dict]:
    out = []
    for r in raw_records:
        try:
            n = normalize_one(r)
            if n:
                out.append(n)
        except Exception as exc:  # noqa: BLE001
            from src.core.utils import log
            log(f"[normalize] 跳过一条: {exc}")
    return out
