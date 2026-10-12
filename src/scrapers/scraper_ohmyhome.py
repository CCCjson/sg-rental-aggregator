"""Ohmyhome 租房爬虫。

Ohmyhome 在 CloudFront + AWS WAF captcha 之后（curl 直接 405 / "Human Verification"）。
方案：headful 持久化浏览器（config.DATA_DIR/"browser_ohmyhome"）加载租房页，等待 AWS WAF
captcha 自动放行（token 存入持久目录，二次运行复用），随后用「页面内 fetch」调用
站点后端的 Elastic App Search API（同源、带公开 search 令牌），分页拉取全部租盘。

后端 API（探针确认）：
  POST https://<es-host>.ent-search.ap-southeast-1.aws.cloud.es.io
       /api/as/v1/engines/prd-listing/search.json
  Authorization: Bearer search-o7hejndqy25pkkgnyh9xrh6d  (公开只读 search key)
  body: {"query":"","page":{"current":N,"size":40},
         "filters":{"all":[{"country_code":"SGP"},
                            {"type":[HOME_RENTAL, *_BEDROOM_RENTAL...]}]}}
  返回 {"meta":{"page":{...total_pages,total_results}}, "results":[{字段:{"raw":值}}...]}

字段（均包在 {"raw": ...} 里）：uid/id/type/property_type/amount/number_of_bedrooms/
number_of_toilets/size(平方米)/latitude/longitude/full_address/project_name/
address_building_name/address_street_name/postal_code/furnishing/description/
created_at/account_uid。type 区分整租(HOME_RENTAL)与单间(*_BEDROOM_RENTAL)。

岛内总量很小（~175 条），全量拉取后由 3km 半径过滤出 NTU 西部少量房源。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import config
from src.core import geo
from src.core.utils import log, save_raw

BASE = "https://ohmyhome.com"
LIST_URL = f"{BASE}/en-sg/property-for-rent-singapore/"
USER_DATA_DIR = config.DATA_DIR / "browser_ohmyhome"

ES_ENDPOINT = (
    "https://12910455a1804be29f41f1d3878aa186.ent-search.ap-southeast-1.aws.cloud.es.io"
    "/api/as/v1/engines/prd-listing/search.json"
)
ES_SEARCH_KEY = "search-o7hejndqy25pkkgnyh9xrh6d"  # 公开只读令牌（前端内嵌）
PAGE_SIZE = 40

RENTAL_TYPES = [
    "HOME_RENTAL",
    "COMMON_BEDROOM_RENTAL",
    "MASTER_BEDROOM_RENTAL",
    "SHARED_BEDROOM_RENTAL",
]
ROOM_TYPES = {"COMMON_BEDROOM_RENTAL", "MASTER_BEDROOM_RENTAL", "SHARED_BEDROOM_RENTAL"}

WAF_TITLE_MARKERS = ("verification", "verify", "just a moment", "moment", "captcha")
SQM_TO_SQFT = 10.7639


# --------------------------------------------------------------------------
# AWS WAF captcha 处理
# --------------------------------------------------------------------------
def _passed_waf(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return not any(m in title for m in WAF_TITLE_MARKERS)


def _wait_waf(page, timeout_s: int = 120) -> bool:
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        if _passed_waf(page):
            return True
        page.wait_for_timeout(2500)
    return _passed_waf(page)


# --------------------------------------------------------------------------
# 字段解析
# --------------------------------------------------------------------------
def _raw(field: Any) -> Any:
    """Elastic App Search 每个字段是 {"raw": 值}（或缺失）。"""
    if isinstance(field, dict):
        return field.get("raw")
    return field


def _num(v: Any) -> Optional[float]:
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _slug(s: str) -> str:
    import re
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "listing"


def _parse(raw: dict) -> Optional[dict]:
    g = lambda k: _raw(raw.get(k))  # noqa: E731

    price = _num(g("amount"))
    if not price or price <= 0:
        return None

    uid = g("uid") or g("id")
    if not uid:
        return None

    rtype = (g("type") or "").upper()
    is_room = rtype in ROOM_TYPES

    lat = _num(g("latitude"))
    lng = _num(g("longitude"))

    street = g("address_street_name") or ""
    url = f"{BASE}/en-sg/listing/{_slug(street)}/{uid}/"

    # 面积：后端为平方米 → 转 sqft
    size_sqm = _num(g("size"))
    sqft = round(size_sqm * SQM_TO_SQFT, 1) if size_sqm else None

    project = g("project_name") or g("address_building_name") or None
    full_addr = g("full_address") or ""
    postal = g("postal_code")
    # 清理 full_address 里的 "Blk " 前缀噪声，保留可地理编码文本
    addr = full_addr or " ".join(x for x in [street, g("town_name")] if x)
    if not addr and postal:
        addr = f"Singapore {postal}"

    furn_raw = g("furnishing")
    furn = None
    if furn_raw:
        fl = str(furn_raw).upper()
        if "UNFURNISHED" in fl or "NONE" in fl or "BARE" in fl:
            furn = "unfurnished"
        elif "PARTIAL" in fl:
            furn = "partial"
        elif "FURNISH" in fl or "FULL" in fl:
            furn = "furnished"

    beds = _int(g("number_of_bedrooms"))
    baths = _int(g("number_of_toilets"))

    ptype = g("property_type") or g("sub_property_type")
    amenities_raw: list[str] = []
    if ptype:
        amenities_raw.append(str(ptype).lower())
    tags = g("tags")
    if isinstance(tags, list):
        amenities_raw.extend(str(t).lower() for t in tags if t)
    if furn:
        amenities_raw.append(furn)

    title = project or (street or "Ohmyhome rental")
    if rtype:
        rt_label = "Room" if is_room else "Whole unit"
        title = f"{title} ({rt_label})"

    return {
        "source": "ohmyhome",
        "id": str(uid),
        "url": url,
        "title": title[:140],
        "property_name": project or street or addr,
        "address": (f"{addr} S({postal})" if postal and postal not in (full_addr or "")
                    else addr),
        "lat": lat,
        "lng": lng,
        "price": int(price),
        "bedrooms": None if is_room else beds,
        "bathrooms": baths,
        "floor_area_sqft": sqft,
        "listing_type": "room" if is_room else "whole_unit",
        "description": g("description") or "",
        "amenities_raw": amenities_raw,
        "furnishing": furn,
        "agent_or_owner": ("Ohmyhome agent" if str(g("is_omh_agent")).lower() == "true"
                           else g("account_uid")),
        "posted_date": g("created_at"),
    }


def _fetch_page(page, current: int) -> Optional[dict]:
    """页面内 fetch 调 Elastic App Search（同源，带 WAF token），返回解析后的 JSON。"""
    body = {
        "query": "",
        "page": {"current": current, "size": PAGE_SIZE},
        "filters": {
            "all": [
                {"country_code": "SGP"},
                {"type": RENTAL_TYPES},
            ],
            "any": [],
        },
        "sort": [{"created_at": "desc"}],
    }
    try:
        txt = page.evaluate(
            """async ({url, key, body}) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'authorization': 'Bearer ' + key,
                        'content-type': 'application/json',
                        'x-swiftype-client': 'elastic-app-search-javascript',
                        'x-swiftype-client-version': '8.5.1',
                    },
                    body: JSON.stringify(body),
                });
                if (r.status !== 200) return JSON.stringify({__err: r.status});
                return await r.text();
            }""",
            {"url": ES_ENDPOINT, "key": ES_SEARCH_KEY, "body": body},
        )
        data = json.loads(txt)
        if isinstance(data, dict) and "__err" in data:
            log(f"[ohmyhome] API {data['__err']} (page {current})")
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        log(f"[ohmyhome] fetch 异常: {exc}")
        return None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def scrape(max_pages: int = 25, headless: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright

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

        log("[ohmyhome] 打开站点并等待 AWS WAF 放行 …")
        try:
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"[ohmyhome] 首页加载异常: {exc}")
        if not _wait_waf(page, timeout_s=120):
            log("[ohmyhome] ⚠️ 未通过 AWS WAF 验证（headful 下可手动完成验证后重试）")
            ctx.close()
            return []
        log("[ohmyhome] ✅ 已通过 WAF")

        total_pages = max_pages
        for current in range(1, max_pages + 1):
            data = _fetch_page(page, current)
            page.wait_for_timeout(900)  # 温和限速 ~700-1500ms
            if not data:
                break
            results = data.get("results") or []
            meta_page = (data.get("meta") or {}).get("page") or {}
            total_results = meta_page.get("total_results", 0)
            total_pages = meta_page.get("total_pages", total_pages)
            if not results:
                break
            all_raw.extend(results)
            got = 0
            for raw in results:
                rec = _parse(raw)
                if not rec or rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                if rec["lat"] is not None and rec["lng"] is not None:
                    d = geo.distance_to_ntu(rec["lat"], rec["lng"])
                    rec["distance_km"] = d
                    rec["distance_is_approx"] = False
                    if d is not None and d > config.RADIUS_KM * 1.15:
                        continue  # 岛内大量远离 NTU，提前丢弃
                parsed.append(rec)
                got += 1
            log(f"[ohmyhome] 第 {current}/{total_pages} 页：本页 {len(results)} 条，"
                f"近 NTU 有效累计 {len(parsed)}（岛内总数 {total_results}）")
            if current >= total_pages:
                break

        ctx.close()

    save_raw("ohmyhome", "listings", all_raw)
    log(f"[ohmyhome] 完成：原始 {len(all_raw)} 条 → 近 NTU 有效 {len(parsed)} 条")
    return parsed


if __name__ == "__main__":
    import sys
    mp = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 25
    hl = "--headless" in sys.argv
    rows = scrape(max_pages=mp, headless=hl)
    rooms = [r for r in rows if r["listing_type"] == "room"]
    whole = [r for r in rows if r["listing_type"] == "whole_unit"]
    within = [r for r in rows if (r.get("distance_km") or 99) <= config.RADIUS_KM]
    print(f"\nTOTAL: {len(rows)}  (rooms: {len(rooms)}, whole: {len(whole)})")
    print(f"within {config.RADIUS_KM}km of NTU: {len(within)}")
    print(json.dumps(rows[:2], ensure_ascii=False, indent=2))
