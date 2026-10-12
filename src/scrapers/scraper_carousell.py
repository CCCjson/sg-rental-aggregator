"""Carousell.sg 租房爬虫（西部 / 近 NTU，curl_cffi 轻量方案）。

Carousell 搜索结果页（/search/<query>/）由 Next/SSR 渲染，**首屏 48 条结果**直接
内嵌在 <script type="application/json"> 的应用状态里（路径：
SearchListing.searchCache[*].results[*].listingCard），无 Cloudflare 挑战，
curl_cffi(impersonate=chrome120) 即可拿到 200。翻页走的是 protobuf+CSRF 的内部
POST 接口（/ds/filter/search/...，未带 token 时 403），实现复杂且收益有限，故本
爬虫改用「多条西部关键词 × 首屏 48 条」的策略覆盖近 NTU 房源，温和限速、去重。

listingCard 关键字段：
  id                       稳定 ID（URL 为 /p/<slug>-<id>）
  title / price            "S$850" 形式的字符串价
  belowFold[]              header_1=标题, header_2=价格, paragraph=房产/房型
  attributes[].content     形态一：[地点, 可入住, 房产类型, 房型]
                           形态二：[卧室数, 卫浴数, "200sqft", 地点]
  seller.username          卖家/中介
Carousell 卡片通常**无精确坐标**（marketPlace.location 是 Singapore 质心），故
lat/lng 留空，把镇名 / Blk 街道 / MRT 写进 address / property_name，交由下游
OneMap 地理编码 + 3km 半径过滤近似定位。
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional
from urllib.parse import quote

import config
from src.core.utils import log, save_raw

BASE = "https://www.carousell.sg"

# 西部 / 近 NTU 关键词（房间为主，含整租）
QUERIES = [
    "jurong west room rent",
    "jurong west whole unit rent",
    "boon lay room rent",
    "pioneer room rent",
    "nanyang rent",
    "clementi room rent",
    "bukit batok room rent",
    "jurong east room rent",
]

_PRICE_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
# 房产类型关键词
_PROP_TYPES = {
    "hdb": "HDB", "condo": "Condo", "apartment": "Apartment",
    "landed": "Landed Property", "house": "Landed Property",
    "walk-up": "Walk-up", "executive": "HDB",
}
# 房型关键词 → listing_type
_ROOM_KEYWORDS = ("common room", "master room", "shared room", "sharing room",
                  "studio room", "room for rent", "room rent")
_WHOLE_KEYWORDS = ("whole unit", "entire unit", "whole house", "studio apartment")

# 销售 / 非住宅 / 服务噪音关键词（出现即跳过）
_NOISE_RE = re.compile(
    r"\b(for\s*sale|selling|buy\b|loan|mortgage|cleaning\s*service|"
    r"renovation|moving\s*service|aircon\s*service|massage|tuition|"
    r"office\s*space|shop\s*space|warehouse|industrial|carpark\s*for)\b",
    re.IGNORECASE,
)
# 西部镇名（用于把缺失镇名补到 address，提升地理编码命中）
_WEST_TOWNS = (
    "jurong west", "jurong east", "boon lay", "pioneer", "lakeside",
    "chinese garden", "nanyang", "bukit batok", "clementi", "tengah",
    "choa chu kang", "jurong", "joo koon", "gombak",
)


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:80] or "listing"


def _parse_price(text) -> Optional[int]:
    """从 'S$1,200' / '1200' 之类提取整数月租。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = _PRICE_RE.search(str(text))
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", ""))))
    except ValueError:
        return None


def _classify(title: str, attrs_text: str, room_para: str) -> tuple[str, Optional[int]]:
    """返回 (listing_type, bedrooms)。"""
    blob = f"{title} {attrs_text} {room_para}".lower()
    # 明确整租信号
    if any(k in blob for k in _WHOLE_KEYWORDS):
        return "whole_unit", None
    # 明确房间信号（Carousell 主体）
    if any(k in blob for k in _ROOM_KEYWORDS) or "room" in (room_para or "").lower():
        return "room", None
    # 形态二里若卧室数 >= 1 且标题像整租，归为整租
    return "room", None


def _extract_from_attributes(attrs: list[str]) -> dict:
    """从 attributes.content 列表里抽出 location / proptype / roomtype / beds / baths / sqft。"""
    out = {"location": None, "proptype": None, "roomtype": None,
           "beds": None, "baths": None, "sqft": None}
    nums: list[int] = []
    for a in attrs:
        if not a:
            continue
        low = a.lower()
        # 面积
        sm = re.search(r"([\d,]+)\s*sqft", low)
        if sm:
            out["sqft"] = int(sm.group(1).replace(",", ""))
            continue
        # 地点：含 MRT 代号 "(EW28)" 或西部镇名
        if re.search(r"\([a-z]{2}\d+", low) or any(tw in low for tw in _WEST_TOWNS) \
                or re.search(r"\([a-z]{2}\d+\s+[a-z]{2}\d+\)", low):
            out["location"] = out["location"] or a
            continue
        # 房型
        if "room" in low or low in ("whole unit", "studio"):
            out["roomtype"] = out["roomtype"] or a
            continue
        # 房产类型
        if low in ("hdb", "condo", "apartment", "landed property",
                   "hdb apartment", "walk-up apartment"):
            out["proptype"] = out["proptype"] or a
            continue
        # 纯数字（卧室/卫浴）
        if a.strip().isdigit():
            nums.append(int(a.strip()))
    if nums:
        out["beds"] = nums[0]
        if len(nums) > 1:
            out["baths"] = nums[1]
    return out


def _parse_card(lc: dict) -> Optional[dict]:
    cid = str(lc.get("id") or "").strip()
    if not cid:
        return None

    title = (lc.get("title") or "").strip()
    # belowFold 提供 header/价格/房产/房型段落
    headers, paragraphs = [], []
    for blk in lc.get("belowFold") or []:
        comp = blk.get("component") or ""
        sc = blk.get("stringContent")
        if not sc:
            continue
        if comp.startswith("header"):
            headers.append(sc)
        elif comp == "paragraph":
            paragraphs.append(sc)
    if not title and headers:
        title = headers[0].strip()

    price = _parse_price(lc.get("price"))
    if price is None and headers:
        price = _parse_price(headers[-1])
    if price is None:
        return None

    attrs = [a.get("content") for a in (lc.get("attributes") or []) if a.get("content")]
    ax = _extract_from_attributes(attrs)
    attrs_text = " ".join(attrs)
    room_para = " ".join(paragraphs)

    blob = f"{title} {attrs_text} {room_para}"
    # 噪音过滤：销售 / 服务 / 非住宅
    if _NOISE_RE.search(blob):
        return None

    listing_type, beds_from_text = _classify(title, attrs_text, room_para)
    # attributes 给的房型优先
    rt_low = (ax["roomtype"] or "").lower()
    if "whole unit" in rt_low or "studio" in rt_low:
        listing_type = "whole_unit"
    elif "room" in rt_low:
        listing_type = "room"

    beds = ax["beds"] if listing_type == "whole_unit" else None
    if beds is not None and beds <= 0:
        beds = None

    # 价格合理性：月租 500–9000；房间通常 ~600-2500
    if price < 500 or price > 9000:
        return None
    # 房间价过高（可能是整套误判）→ 仍保留但不强行；> 4000 的"room"很可疑，剔除
    if listing_type == "room" and price > 4000:
        return None

    # 地点：attributes 的 location（MRT/镇）优先，否则从标题里捞西部镇名 / Blk
    location = ax["location"]
    addr = location
    blk_m = re.search(
        r"\b(blk\s*\d+[a-z]?(?:\s+(?!room|rooms|for|rent|rental|available|"
        r"unit|hdb|condo|sharing|shared|common|master)[a-z][\w'.]*){0,5})",
        blob, re.IGNORECASE)
    blk = blk_m.group(1).strip() if blk_m else None
    if not addr:
        # 标题里找西部镇名
        low = blob.lower()
        for tw in _WEST_TOWNS:
            if tw in low:
                addr = tw.title()
                break
    if blk:
        # Blk 街道更精确，优先做 address
        addr = blk
    if not addr:
        addr = location

    # property_name：MRT/镇名（去掉 MRT 代号括号便于 OneMap）
    prop_name = None
    if location:
        prop_name = re.sub(r"\s*\([^)]*\)", "", location).split(",")[0].strip() or location

    # 家具 / 配套
    amenities_raw: list[str] = []
    low = blob.lower()
    if ax["proptype"]:
        amenities_raw.append(ax["proptype"])
    if ax["roomtype"]:
        amenities_raw.append(ax["roomtype"])
    if "aircon" in low or "air-con" in low or "air con" in low:
        amenities_raw.append("aircon")
    if "wifi" in low or "wi-fi" in low:
        amenities_raw.append("wifi")
    if "mrt" in low or re.search(r"\([a-z]{2}\d+", low):
        amenities_raw.append("near mrt")
    furn = "furnished" if ("furnished" in low or "furnishing" in low) else None

    seller = lc.get("seller") or {}
    seller_name = seller.get("username") or seller.get("firstName")

    url = f"{BASE}/p/{_slugify(title)}-{cid}"

    return {
        "source": "carousell",
        "id": f"carousell-{cid}",
        "url": url,
        "title": title[:160],
        "property_name": prop_name,
        "address": addr,
        "lat": None,
        "lng": None,
        "price": price,
        "bedrooms": beds,
        "bathrooms": ax["baths"] if (ax["baths"] and ax["baths"] > 0) else None,
        "floor_area_sqft": ax["sqft"],
        "listing_type": listing_type,
        "description": (title + " | " + room_para + " | " + attrs_text).strip(" |")[:400],
        "amenities_raw": amenities_raw,
        "furnishing": furn,
        "agent_or_owner": seller_name,
        "posted_date": None,
    }


def _fetch_search(query: str) -> tuple[list[dict], Optional[str]]:
    """抓一条搜索词的首屏结果，返回 (listingCards, formattedTotal)。"""
    from src.core import client

    url = f"{BASE}/search/{quote(query)}/"
    resp = client.get(url, headers=client.build_headers(
        extra={"accept": "text/html,application/xhtml+xml,*/*",
               "sec-fetch-mode": "navigate", "sec-fetch-site": "none"}))
    if resp is None or resp.status_code != 200:
        log(f"[carousell] '{query}' 状态 {getattr(resp, 'status_code', 'None')}")
        return [], None

    t = resp.text
    idx = t.find('"listingCard"')
    if idx < 0:
        log(f"[carousell] '{query}' 页面无 listingCard")
        return [], None
    sc = t.rfind('<script type="application/json">', 0, idx)
    if sc < 0:
        return [], None
    start = sc + len('<script type="application/json">')
    end = t.find("</script>", idx)
    try:
        data = json.loads(t[start:end])
    except json.JSONDecodeError as exc:
        log(f"[carousell] '{query}' JSON 解析失败: {exc}")
        return [], None

    cache = (data.get("SearchListing") or {}).get("searchCache") or {}
    if not cache:
        return [], None
    res = next(iter(cache.values()))
    cards = [c["listingCard"] for c in (res.get("results") or [])
             if isinstance(c, dict) and c.get("listingCard")]
    return cards, res.get("formattedTotal")


def scrape(queries: Optional[list[str]] = None) -> list[dict]:
    queries = queries or QUERIES
    all_raw: list[dict] = []
    parsed: list[dict] = []
    seen: set[str] = set()

    for q in queries:
        cards, total = _fetch_search(q)
        all_raw.extend(cards)
        got = 0
        for lc in cards:
            rec = _parse_card(lc)
            if not rec or rec["id"] in seen:
                continue
            seen.add(rec["id"])
            parsed.append(rec)
            got += 1
        log(f"[carousell] '{q}': 卡片 {len(cards)}（站点总数 {total}）→ 有效新增 {got}，累计 {len(parsed)}")
        time.sleep(1.0)  # 温和限速

    save_raw("carousell", "cards", all_raw)
    log(f"[carousell] 完成：原始 {len(all_raw)} 卡片 → 有效 {len(parsed)} 条")
    return parsed


if __name__ == "__main__":
    rows = scrape()
    rooms = [r for r in rows if r["listing_type"] == "room"]
    whole = [r for r in rows if r["listing_type"] == "whole_unit"]
    print(f"\nTOTAL: {len(rows)}  (rooms: {len(rooms)}, whole: {len(whole)})")
    west = [r for r in rows if r.get("address") and any(
        tw in (r["address"] or "").lower() or tw in (r.get("property_name") or "").lower()
        for tw in _WEST_TOWNS)]
    print(f"west-located (town/MRT in address|property_name): {len(west)}")
    print(json.dumps(rows[:2], ensure_ascii=False, indent=2))
