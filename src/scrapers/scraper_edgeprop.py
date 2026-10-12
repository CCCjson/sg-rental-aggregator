"""EdgeProp.sg 租房爬虫。

EdgeProp 套着 Cloudflare **托管挑战(Managed Challenge)**——会做浏览器指纹检测，
普通 Playwright/真 Chrome(经 CDP 控制)都过不去(一直卡在 "Performing security
verification")。这里用 **nodriver**(undetected-chromedriver 的后继，规避 CDP
Runtime.enable 指纹)放出真 Chrome 通过挑战，~8s 自动放行。

过挑战后页面是 Next.js SSR，房源在 `#__NEXT_DATA__` 的
`props.pageProps.listings[]`（每页 20 条）。字段含 project_name / street_name /
**postal_code**（→ OneMap 精确地理编码）/ asking_price / bedrooms / bathrooms /
floor_area_sqft / 相对 url。

注意：EdgeProp robots.txt 不鼓励抓 listings，这里仅供个人租房研究、温和限速、
小页数抓取，请勿滥用。
"""

from __future__ import annotations

import json
import re
from typing import Optional

import config
from src.core import geo
from src.core.utils import log, save_raw

BASE = config.SITES["edgeprop"]["base"]
USER_DATA_DIR = config.DATA_DIR / "browser_edgeprop"

# NTU 邻近的镇（3km 过滤交给下游）
TOWNS = ["jurong-west", "clementi", "jurong-east", "boon-lay", "bukit-batok"]
ROOM_SLUGS = ["jurong-west", "clementi"]


def _build_jobs() -> list[tuple]:
    """(route_path, listing_type)。优先精确卧室 slug（2 人合租重点抓 2 房整租），
    再 3 房，再通用整租兜底，最后房间。"""
    jobs: list[tuple] = []
    for t in TOWNS:                                  # 2 房整租（2 人合租核心）
        jobs.append((f"property-for-rent/{t}/2-bedroom", "whole_unit"))
    for t in ["jurong-west", "clementi", "jurong-east"]:  # 3 房整租（备选）
        jobs.append((f"property-for-rent/{t}/3-bedroom", "whole_unit"))
    for t in ["jurong-west", "clementi"]:            # 通用整租兜底
        jobs.append((f"property-for-rent/{t}", "whole_unit"))
    for t in ROOM_SLUGS:                             # 房间
        jobs.append((f"hdb-for-room-rental/{t}", "room"))
    return jobs

_CHALLENGE_MARKERS = ("just a moment", "security verif", "attention", "checking", "稍候", "稍后")


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        m = re.search(r"\d[\d,]*", str(v))
        return int(m.group(0).replace(",", "")) if m else None
    except Exception:  # noqa: BLE001
        return None


def _parse(ld: dict, listing_type: str) -> Optional[dict]:
    price = _to_int(ld.get("asking_price"))
    if not price or price <= 0:
        return None

    lid = (ld.get("listing_id") or ld.get("uid") or ld.get("asset_id")
           or ld.get("url"))
    rel = ld.get("url") or ""
    url = f"{BASE}/{rel.lstrip('/')}" if rel else BASE

    project = (ld.get("project_name") or "").strip() or None
    street = (ld.get("street_name") or "").strip() or None
    postal = (ld.get("postal_code") or "").strip() or None
    addr = " ".join(x for x in [street, f"S{postal}" if postal else None] if x) or None

    beds = _to_int(ld.get("bedrooms"))
    rec = {
        "source": "edgeprop",
        "id": f"edgeprop_{lid}",
        "url": url,
        "title": (ld.get("title") or project or street),
        "property_name": project or street,
        "address": addr,
        "lat": None,
        "lng": None,
        "price": price,
        "bedrooms": None if listing_type == "room" else beds,
        "bathrooms": _to_int(ld.get("bathrooms")),
        "floor_area_sqft": _to_int(ld.get("floor_area_sqft")),
        "listing_type": listing_type,
        "description": " ".join(x for x in [ld.get("title"), project, street,
                                            ld.get("property_type")] if x),
        "amenities_raw": [],
        "furnishing": None,
        "agent_or_owner": ld.get("agent_name"),
        "posted_date": ld.get("updatedAt") or ld.get("listed_date"),
        "_postal": postal,
    }
    return rec


def _geocode_inplace(rec: dict) -> None:
    """优先用 postal 精确定位，回退楼盘名。"""
    postal = rec.pop("_postal", None)
    lat = lng = dist = None
    for q in (postal, rec.get("property_name"), rec.get("address")):
        if not q:
            continue
        lat, lng, dist = geo.geocode_distance(q)
        if dist is not None:
            break
    if dist is not None:
        rec["lat"], rec["lng"] = lat, lng
        rec["distance_km"] = dist
        rec["distance_is_approx"] = (postal is None)  # 有邮编则精确


# --------------------------------------------------------------------------
# nodriver 抓取（异步），对外暴露同步 scrape()
# --------------------------------------------------------------------------
async def _read_listings(tab) -> list[dict]:
    """等待并读取当前页 NEXT_DATA 里的 listings。"""
    import asyncio
    for _ in range(30):  # 最多 ~60s 等过挑战 + SSR
        try:
            raw = await tab.evaluate(
                "JSON.stringify({t:document.title,"
                "nd:(document.querySelector('#__NEXT_DATA__')||{}).textContent||''})")
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(2)
            continue
        title = (obj.get("t") or "").lower()
        nd = obj.get("nd") or ""
        if nd and not any(m in title for m in _CHALLENGE_MARKERS):
            try:
                data = json.loads(nd)
                return (data.get("props", {}).get("pageProps", {})
                        .get("listings") or [])
            except Exception:  # noqa: BLE001
                return []
        await asyncio.sleep(2)
    return []


async def _scrape_async(max_pages: int) -> list[dict]:
    import asyncio
    import nodriver as uc

    parsed: list[dict] = []
    all_raw: list[dict] = []
    seen: set = set()

    browser = await uc.start(
        headless=False,
        user_data_dir=str(USER_DATA_DIR),
        browser_args=["--lang=en-SG", "--disable-blink-features=AutomationControlled"],
    )
    log("[edgeprop] 启动 nodriver，准备通过 Cloudflare 托管挑战 …")

    jobs = _build_jobs()

    try:
        tab = await browser.get("about:blank")
        for route_path, ltype in jobs:
            for pg in range(1, max_pages + 1):
                url = f"{BASE}/{route_path}" + (f"?page={pg}" if pg > 1 else "")
                try:
                    await tab.get(url)
                except Exception as exc:  # noqa: BLE001
                    log(f"[edgeprop] 打开失败 {url}: {str(exc)[:50]}")
                    break
                listings = await _read_listings(tab)
                if not listings:
                    if pg == 1:
                        log(f"[edgeprop] {route_path}: 第1页空（板块可能无房源）")
                    break
                new_here = 0
                for ld in listings:
                    rec = _parse(ld, ltype)
                    if not rec or rec["id"] in seen:
                        continue
                    seen.add(rec["id"])
                    all_raw.append({**ld, "_listing_type": ltype})
                    _geocode_inplace(rec)
                    d = rec.get("distance_km")
                    if d is not None and d > config.RADIUS_KM * 1.15:
                        continue
                    parsed.append(rec)
                    new_here += 1
                log(f"[edgeprop] {route_path} 第{pg}页：{len(listings)} 条，"
                    f"近 NTU 累计 {len(parsed)}")
                await asyncio.sleep(0.9)  # 温和限速
                if len(listings) < 20:  # 不足一页 = 到底
                    break
    finally:
        try:
            browser.stop()
        except Exception:  # noqa: BLE001
            pass

    save_raw("edgeprop", "listings", all_raw)
    log(f"[edgeprop] 完成：原始 {len(all_raw)} 条 → 近 NTU {len(parsed)} 条")
    return parsed


def scrape(max_pages: int = 8) -> list[dict]:
    import nodriver as uc
    return uc.loop().run_until_complete(_scrape_async(max_pages))


if __name__ == "__main__":
    import sys
    mp = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows = scrape(max_pages=mp)
    rooms = [r for r in rows if r["listing_type"] == "room"]
    whole = [r for r in rows if r["listing_type"] == "whole_unit"]
    within = [r for r in rows if (r.get("distance_km") or 9) <= config.RADIUS_KM]
    print(f"\nTOTAL near-NTU(1.15x): {len(rows)}  rooms={len(rooms)} whole={len(whole)}"
          f"  strictly<=3km={len(within)}")
    print(json.dumps(rows[:2], ensure_ascii=False, indent=2))
