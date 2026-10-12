"""roomies.sg 爬虫（西部单间/合租，Playwright 渲染）。

列表为 Angular SSR：卡片文本含「街道, 镇, CDC 邮编, 描述, $价/月」，**带 6 位邮编**，
故用 OneMap 按邮编精确地理编码。覆盖 NTU 所在西部：
  /rooms/singapore-west 和 /singapore-south-west-community-development-council
每页约 12–24 条（站点未提供更多分页），归一化阶段按 3km 半径过滤。
"""

from __future__ import annotations

import re
from typing import Optional

import config
from src.core import geo
from src.core.utils import log, save_raw

BASE = "https://www.roomies.sg"
WEST_PAGES = [
    "https://www.roomies.sg/rooms/singapore-west",
    "https://www.roomies.sg/singapore-south-west-community-development-council",
]
USER_DATA_DIR = config.DATA_DIR / "browser_roomies"

_PRICE_RE = re.compile(r"\$\s?([\d,]{3,6})")
_POSTAL_RE = re.compile(r"\b(\d{6})\b")
_PTYPE_RE = re.compile(r"\bin\s+an?\s+(apartment|condo(?:minium)?|house|hdb|flat|landed)", re.I)


def _parse_card(href: str, text: str) -> Optional[dict]:
    text = " ".join((text or "").split())
    pm = _PRICE_RE.search(text)
    if not pm:
        return None
    price = int(pm.group(1).replace(",", ""))
    if price < 150 or price > 9000:
        return None

    url = href if href.startswith("http") else BASE + href
    postal = None
    pmatch = _POSTAL_RE.search(text)
    if pmatch:
        postal = pmatch.group(1)

    # 房型/属性
    low = text.lower()
    if "shared" in low or "sharing" in low:
        rcat_hint = "double"
    elif "own bathroom" in low or "ensuite" in low or "en-suite" in low:
        rcat_hint = "single"   # 独卫单人
    else:
        rcat_hint = "single"

    pt = _PTYPE_RE.search(text)
    ptype = (pt.group(1).lower() if pt else None)

    # 地点文本（邮编之前的部分）：街道, 镇, CDC
    loc = text.split(str(postal))[0].strip(" ,") if postal else text[:pm.start()].strip(" ,")

    amenities_raw = []
    if "own bathroom" in low or "ensuite" in low:
        amenities_raw.append("private bath")
    if "aircon" in low or "air-con" in low or "air conditioned" in low:
        amenities_raw.append("aircon")
    if "wifi" in low or "wi-fi" in low:
        amenities_raw.append("wifi")
    if ptype:
        amenities_raw.append(ptype)

    return {
        "source": "roomies",
        "id": url,
        "url": url,
        "title": text[:120],
        "property_name": loc[:70],
        "address": (f"Singapore {postal}" if postal else loc[:80]),
        "lat": None, "lng": None,
        "price": price,
        "bedrooms": None,
        "bathrooms": None,
        "floor_area_sqft": None,
        "listing_type": "room",
        "description": text[:300],
        "amenities_raw": amenities_raw,
        "furnishing": "furnished" if "furnished" in low else None,
        "_postal": postal,
        "_ptype": ptype,
    }


def scrape(max_pages: int = 2) -> list[dict]:
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

        for url in WEST_PAGES:
            log(f"[roomies] 抓取: {url}")
            try:
                page.goto(url, wait_until="commit", timeout=45000)
                page.wait_for_timeout(7000)
                for _ in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)
            except Exception as exc:  # noqa: BLE001
                log(f"[roomies] 页面加载异常: {exc}")
                continue

            cards = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href*=\"/rooms/\"]'))"
                ".filter(a => /\\/rooms\\/\\d+/.test(a.getAttribute('href')||''))"
                ".map(a => ({h:a.getAttribute('href'), t:(a.innerText||'').replace(/\\s+/g,' ').trim()}))")
            for c in cards:
                rec = _parse_card(c["h"], c["t"])
                if not rec or rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                raw_dump.append({"href": c["h"], "text": c["t"]})
                out.append(rec)
            log(f"[roomies] 本页解析 {len(cards)} 卡片，累计 {len(out)} 条")

        ctx.close()

    # 邮编精确地理编码（无邮编则用地点文本）
    for rec in out:
        q = rec.pop("_postal", None) or rec.get("property_name")
        rec.pop("_ptype", None)
        lat, lng, d = geo.geocode_distance(q) if q else (None, None, None)
        if d is not None:
            rec["lat"], rec["lng"] = lat, lng
            rec["distance_km"] = d
            rec["distance_is_approx"] = False  # 邮编级，较精确

    save_raw("roomies", "cards", raw_dump)
    log(f"[roomies] 完成：{len(out)} 条（已地理编码，待 3km 过滤）")
    return out


if __name__ == "__main__":
    import json
    rows = scrape()
    within = [r for r in rows if (r.get("distance_km") or 99) <= config.RADIUS_KM]
    print(f"TOTAL {len(rows)}, within 3km: {len(within)}")
    print(json.dumps(within[:5], ensure_ascii=False, indent=2))
