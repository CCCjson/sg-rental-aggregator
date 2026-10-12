"""rentinsingapore.com.sg 爬虫（Playwright 渲染，西部片区）。

AngularJS SSR 列表，按「镇名 slug」过滤：
  /rooms-for-rent/{jurong-west,boon-lay,jurong-east}   （单间，每镇约 10 条）
  /properties-for-rent/{...}                           （整租，含 3R/3房，喂合租分析）
卡片文本：「Room for Rent|Whole Unit - 镇 $价 标题/描述 TAGS」。
无经纬度，按 Blk+镇 / 镇 经 OneMap 地理编码（镇级近似），3km 过滤。
"""

from __future__ import annotations

import re
from typing import Optional

import config
from src.core import geo
from src.core.utils import log, save_raw

BASE = "https://rentinsingapore.com.sg"
USER_DATA_DIR = config.DATA_DIR / "browser_rentinsg"

# NTU 西部有效镇 slug（pioneer/nanyang 非有效 slug 会回落到全岛默认列表，故不用）
WEST_TOWNS = ["jurong-west", "boon-lay", "jurong-east"]
ROUTES = ["rooms-for-rent", "properties-for-rent"]

_PRICE_RE = re.compile(r"\$\s?([\d,]{3,6})")
_BLK_RE = re.compile(r"\bblk\s*([0-9]+[a-z]?)\b", re.I)
_BEDS_RE = re.compile(r"(\d+)\s*(?:R\b|-?\s*room|-?\s*bedroom|\s*br\b)", re.I)


def _town_label(slug: str) -> str:
    return slug.replace("-", " ").title()


def _parse_card(href: str, text: str, want_town: str) -> Optional[dict]:
    text = " ".join((text or "").split())
    low = text.lower()
    pm = _PRICE_RE.search(text)
    if not pm:
        return None
    price = int(pm.group(1).replace(",", ""))
    if price < 200 or price > 12000:
        return None

    is_whole = low.startswith("whole unit")
    # 镇：取 "- " 与 " $" 之间
    town = want_town.replace("-", " ")
    mt = re.match(r"(?:room for rent|whole unit)\s*-\s*([a-z ]+?)\s*\$", low)
    if mt:
        town = mt.group(1).strip()
    # 回落检测：卡片镇名与请求 slug 不符则丢弃（命中了默认混合列表）
    if want_town.replace("-", " ") not in town:
        return None

    url = href if href.startswith("http") else BASE + href

    beds = None
    rcat_hint = None
    if is_whole:
        bm = _BEDS_RE.search(text)
        if bm:
            beds = int(bm.group(1))
    else:
        if "master" in low:
            rcat_hint = "master"
        elif "common" in low:
            rcat_hint = "common"

    blk = _BLK_RE.search(text)
    geo_q = (f"Blk {blk.group(1)} {_town_label(want_town)}" if blk
             else _town_label(want_town))

    amenities_raw = []
    for kw, tag in (("aircon", "aircon"), ("air-con", "aircon"),
                    ("air conditioned", "aircon"), ("wifi", "wifi"),
                    ("cooking", "cooking"), ("mrt", "mrt")):
        if kw in low and tag not in amenities_raw:
            amenities_raw.append(tag)
    ptype = None
    if "hdb" in low or "flat" in low:
        ptype = "hdb"
    elif "condo" in low:
        ptype = "condo"
    elif "house" in low or "landed" in low:
        ptype = "house"
    if ptype:
        amenities_raw.append(ptype)

    name = (f"Blk {blk.group(1)} {_town_label(want_town)}" if blk
            else _town_label(want_town))
    # 描述里补 master/common 关键词，便于 normalize 房型分类
    desc = text[:300]
    if rcat_hint and rcat_hint not in low:
        desc += f" {rcat_hint} room"

    return {
        "source": "rentinsg",
        "id": url,
        "url": url,
        "title": text[:120],
        "property_name": name,
        "address": _town_label(want_town) + ", Singapore",
        "lat": None, "lng": None,
        "price": price,
        "bedrooms": beds,
        "bathrooms": None,
        "floor_area_sqft": None,
        "listing_type": "whole_unit" if is_whole else "room",
        "description": desc,
        "amenities_raw": amenities_raw,
        "furnishing": "furnished" if "furnished" in low else None,
        "_geo_q": geo_q,
    }


def scrape(max_pages: int = 1) -> list[dict]:
    from playwright.sync_api import sync_playwright

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    seen: set = set()
    raw_dump: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR), headless=False,
            user_agent=config.USER_AGENTS[0], viewport={"width": 1440, "height": 900},
            locale="en-SG",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for route in ROUTES:
            for town in WEST_TOWNS:
                url = f"{BASE}/{route}/{town}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(5000)
                except Exception as exc:  # noqa: BLE001
                    log(f"[rentinsg] {url} 异常: {exc}")
                    continue
                cards = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href^=\"/ID\"]'))"
                    ".map(a => ({h:a.getAttribute('href'), t:(a.innerText||'').replace(/\\s+/g,' ').trim()}))")
                got = 0
                for c in cards:
                    rec = _parse_card(c["h"], c["t"], town)
                    if not rec or rec["id"] in seen:
                        continue
                    seen.add(rec["id"])
                    raw_dump.append({"route": route, "town": town, **c})
                    out.append(rec)
                    got += 1
                log(f"[rentinsg] {route}/{town}: {len(cards)} 卡片 → 命中 {got}（累计 {len(out)}）")

        ctx.close()

    # 镇级 / Blk 级地理编码
    for rec in out:
        q = rec.pop("_geo_q", None)
        lat, lng, d = geo.geocode_distance(q) if q else (None, None, None)
        if d is not None:
            rec["lat"], rec["lng"] = lat, lng
            rec["distance_km"] = d
            rec["distance_is_approx"] = True  # 镇/块级，近似

    save_raw("rentinsg", "cards", raw_dump)
    log(f"[rentinsg] 完成：{len(out)} 条（已地理编码，待 3km 过滤）")
    return out


if __name__ == "__main__":
    import json
    rows = scrape()
    within = [r for r in rows if (r.get("distance_km") or 99) <= config.RADIUS_KM]
    whole = [r for r in within if r["listing_type"] == "whole_unit"]
    print(f"TOTAL {len(rows)}, within 3km: {len(within)} (whole_unit: {len(whole)})")
    print(json.dumps(within[:6], ensure_ascii=False, indent=2))
