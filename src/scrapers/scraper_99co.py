"""99.co 租房爬虫（主力数据源）。

99.co 有 Cloudflare Turnstile。方案：headful 持久化浏览器通过挑战（cf_clearance
存入持久目录，二次运行复用），随后用「页面内 fetch」调用内部 listings API
（同源、自动带 cf cookie），分页拉取 condo / hdb 两个类目（房间以 beds=-1 内嵌其中）。

内部 API（探针确认）：
  GET /api/v10/web/search/listings
      ?listing_type=rent&main_category={condo|hdb}&query_coords={lat},{lng}
      &query_type=google&radius_max={m}&rental_type=all&page_size=36&page_num={n}
  返回 {"data":{"sections":[{"count":N,"listings":[...]}]}}
"""

from __future__ import annotations

import json
from typing import Any, Optional

import config
from src.core import geo
from src.core.utils import log, save_raw

BASE = config.SITES["99co"]["base"]
USER_DATA_DIR = config.DATA_DIR / "browser_99co"
CF_TITLE_MARKERS = ("just a moment", "attention required", "verifying", "security verification")
CATEGORIES = ["condo", "hdb"]  # 房间内嵌在这两类里（beds=-1）

API_TMPL = (
    "/api/v10/web/search/listings"
    "?listing_type=rent&main_category={cat}"
    "&query_coords={lat},{lng}&query_type=google"
    "&radius_max={radius_m}&rental_type=all"
    "&page_size=36&page_num={page}"
)


# --------------------------------------------------------------------------
# Cloudflare 处理
# --------------------------------------------------------------------------
def _passed_cf(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if any(m in title for m in CF_TITLE_MARKERS):
        return False
    try:
        return bool(page.evaluate(
            "() => !!document.querySelector('a[href*=\"/singapore/rent\"]')"))
    except Exception:  # noqa: BLE001
        return False


def _wait_cf(page, timeout_s: int = 90) -> bool:
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        if _passed_cf(page):
            return True
        page.wait_for_timeout(2000)
    return _passed_cf(page)


# --------------------------------------------------------------------------
# 解析单条房源
# --------------------------------------------------------------------------
def _parse(raw: dict) -> Optional[dict]:
    attrs = raw.get("attributes") or {}
    price = attrs.get("price")
    if not price or price <= 0:
        return None

    beds = attrs.get("bedrooms")
    is_room = beds == -1
    coords = (raw.get("location") or {}).get("coordinates") or {}
    lat, lng = coords.get("lat"), coords.get("lng")

    listing_url = raw.get("listing_url") or ""
    url = BASE + listing_url if listing_url.startswith("/") else listing_url

    # 家具状态
    furn = None
    for t in raw.get("formatted_tags") or []:
        txt = (t.get("text") or "").upper()
        if "UNFURNISHED" in txt:
            furn = "unfurnished"
        elif "FURNISHED" in txt:
            furn = "furnished"
        elif "PARTIAL" in txt:
            furn = "partial"

    # 最近 MRT（作为配套设施信息）
    wdq = raw.get("within_distance_from_query") or {}
    mrt = wdq.get("closest_mrt") or {}
    mrt_info = None
    if mrt.get("title"):
        mrt_info = f"{mrt['title']} {mrt.get('walking_time_in_mins', '?')}min walk"

    addr = raw.get("address_line_2") or " ".join(
        x for x in [raw.get("street_number"), raw.get("street_name")] if x)

    amenities_raw = []
    if mrt_info:
        amenities_raw.append(mrt_info)
    if furn:
        amenities_raw.append(furn)

    return {
        "source": "99co",
        "id": raw.get("id"),
        "url": url,
        "title": raw.get("address_name") or raw.get("project_name"),
        "property_name": raw.get("project_name") or raw.get("address_name"),
        "address": (f"{addr} S{raw.get('postal_code')}" if raw.get("postal_code")
                    else addr),
        "lat": float(lat) if lat is not None else None,
        "lng": float(lng) if lng is not None else None,
        "price": int(price),
        "bedrooms": None if is_room else beds,
        "bathrooms": attrs.get("bathrooms") if attrs.get("bathrooms", -1) != -1 else None,
        "floor_area_sqft": attrs.get("area_size"),
        "listing_type": "room" if is_room else "whole_unit",
        "description": raw.get("description") or "",
        "amenities_raw": amenities_raw,
        "closest_mrt": mrt_info,
        "furnishing": furn,
        "agent_or_owner": (raw.get("user") or {}).get("name"),
        "posted_date": raw.get("created_date_formatted") or raw.get("date_formatted"),
        "main_category": raw.get("main_category"),
    }


def _fetch_json(page, path: str) -> Optional[Any]:
    """页面内 fetch（同源，带 cf cookie），返回 JSON。"""
    try:
        txt = page.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {'accept':'application/json'}});
                if (r.status !== 200) return JSON.stringify({__err: r.status});
                return await r.text();
            }""", path)
        data = json.loads(txt)
        if isinstance(data, dict) and "__err" in data:
            log(f"[99co] API {data['__err']}: {path[:80]}")
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        log(f"[99co] fetch 异常: {exc}")
        return None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def scrape(max_pages: int = 25, headless: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright

    radius_m = int(config.RADIUS_KM * 1000)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_raw: list[dict] = []
    parsed: list[dict] = []
    seen: set = set()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            user_agent=config.USER_AGENTS[0],
            viewport={"width": 1440, "height": 900},
            locale="en-SG",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        log("[99co] 打开站点并通过 Cloudflare …")
        try:
            page.goto(f"{BASE}/singapore/rent",
                      wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"[99co] 首页加载异常: {exc}")
        if not _wait_cf(page, timeout_s=90):
            log("[99co] ⚠️ 未通过 Cloudflare（headful 下可手动点 Turnstile 后重试）")
            ctx.close()
            return []
        log("[99co] ✅ 已通过 Cloudflare")

        for cat in CATEGORIES:
            for page_num in range(1, max_pages + 1):
                path = API_TMPL.format(cat=cat, lat=config.NTU_LAT,
                                       lng=config.NTU_LNG, radius_m=radius_m,
                                       page=page_num)
                data = _fetch_json(page, path)
                page.wait_for_timeout(700)  # 温和限速
                if not data:
                    break
                sections = (data.get("data") or {}).get("sections") or []
                listings = sections[0].get("listings") if sections else []
                if not listings:
                    break
                total = sections[0].get("count", 0)
                all_raw.extend(listings)
                got = 0
                for raw in listings:
                    rec = _parse(raw)
                    if not rec or rec["id"] in seen:
                        continue
                    seen.add(rec["id"])
                    d = geo.distance_to_ntu(rec["lat"], rec["lng"])
                    rec["distance_km"] = d
                    rec["distance_is_approx"] = False
                    if d is not None and d > config.RADIUS_KM * 1.15:
                        continue
                    parsed.append(rec)
                    got += 1
                log(f"[99co] {cat} 第 {page_num} 页：本页 {len(listings)} 条，"
                    f"累计有效 {len(parsed)}（类目总数 {total}）")
                if page_num * 36 >= total:
                    break

        ctx.close()

    save_raw("99co", "listings", all_raw)
    log(f"[99co] 完成：原始 {len(all_raw)} 条 → 有效 {len(parsed)} 条")
    return parsed


if __name__ == "__main__":
    import sys
    mp = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    hl = "--headless" in sys.argv
    rows = scrape(max_pages=mp, headless=hl)
    rooms = [r for r in rows if r["listing_type"] == "room"]
    print(f"\nTOTAL: {len(rows)}  (rooms: {len(rooms)}, whole: {len(rows)-len(rooms)})")
    print(json.dumps(rows[:3], ensure_ascii=False, indent=2))
