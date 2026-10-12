"""PropertyGuru 爬虫（Playwright 过 Cloudflare + 双通道覆盖）。

PropertyGuru 现已上 Cloudflare "Just a moment…" 挑战（与 99.co 同款），curl_cffi 直取会 403。
方案：headful 持久化上下文过 CF（cf_clearance 落盘复用），过后页面仍是 Next.js SSR，
解析 `#__NEXT_DATA__` 里的 `props.pageProps.pageData.data.listingsData[].listingData`。

两个通道（按 listing id 去重合并）：
  1) near-ntu 策展板块 `/property-for-rent/p/near-ntu`（~22 页）—— 近 NTU 的合租间/co-living；
     该板块 fullAddress 为空，靠标题楼盘名地理编码（近似）。
  2) District 22 搜索 `?districtCode=D22&listingType=rent`（Boon Lay/Jurong/Tuas，含 NTU 片区）——
     fullAddress 有值 → 精确地理编码；含真实 2/3 房整租（合租推荐用）。限页数 + 下游 3km 过滤收敛。
     （D23 多在 3km 外，默认不抓；如需可加入 DISTRICTS。）

距离仍由 normalize 阶段用 fullAddress/楼盘名走 OneMap 地理编码 + 3km 过滤。
raw 落盘仍为 listingData 原始 dict（兼容 run.py --reuse propertyguru / _load_from_raw）。
"""

from __future__ import annotations

import json
import random
import re
from typing import Optional

import config
from src.core.utils import log, save_raw

BASE = config.SITES["propertyguru"]["base"]
NEAR_NTU = "/property-for-rent/p/near-ntu"
DISTRICTS = ["D22"]  # NTU 在 D22；D23 多在 3km 外，默认略过
USER_DATA_DIR = config.DATA_DIR / "browser_pg"
CF_TITLE_MARKERS = ("just a moment", "attention required", "verifying", "checking your browser")


# --------------------------------------------------------------------------
# 解析单条房源（沿用旧逻辑，地址优先用 fullAddress）
# --------------------------------------------------------------------------
def _parse(ld: dict) -> Optional[dict]:
    price = (ld.get("price") or {}).get("value")
    if not price or price <= 0:
        return None
    beds = ld.get("bedrooms")
    title = ld.get("localizedTitle") or ""

    def _feat_text(f):
        return f.get("text", "") if isinstance(f, dict) else ""

    feat_list = ld.get("listingFeatures") or []
    feats = " ".join(_feat_text(f) for f in feat_list)
    # PG: bedrooms 为 0/-1/None，或文本含 room/co-living → 房间
    is_room = (beds in (None, 0, -1)) or bool(
        re.search(r"\b(room|co-?living|sharing)\b", f"{feats} {title}", re.I))

    url = ld.get("url") or ""
    if url and url.startswith("/"):
        url = BASE + url

    mrt = (ld.get("mrt") or {}).get("nearbyText") or ""
    amen = []
    if mrt:
        amen.append(mrt)
    amen += [_feat_text(f) for f in feat_list]

    badges = " ".join(b.get("text", "") for b in (ld.get("badges") or [])
                      if isinstance(b, dict))

    # 地理编码用：优先完整地址（District 结果有值，精确），否则标题楼盘名（去括号）
    full_addr = ld.get("fullAddress") or ""
    geo_name = full_addr or re.sub(r"\(.*?\)", "", title).strip()

    return {
        "source": "propertyguru",
        "id": f"pg_{ld.get('id')}",
        "url": url,
        "title": title,
        "property_name": geo_name,
        "address": full_addr or geo_name,
        "lat": None,
        "lng": None,
        "price": int(price),
        "bedrooms": None if is_room else beds,
        "bathrooms": ld.get("bathrooms"),
        "floor_area_sqft": ld.get("floorArea"),
        "listing_type": "room" if is_room else "whole_unit",
        "description": f"{ld.get('localizedTitle','')} {feats} {badges} {mrt}",
        "amenities_raw": amen,
        "closest_mrt": mrt or None,
        "agent_or_owner": (ld.get("agent") or {}).get("name"),
        "posted_date": (ld.get("postedOn") or {}).get("text"),
    }


# --------------------------------------------------------------------------
# 取数：第 1 页整页导航过 CF（拿 buildId）；后续页用页内 fetch _next/data（不再导航、不再触发 CF 整页挑战）
# --------------------------------------------------------------------------
def _read_next_json(page) -> Optional[dict]:
    """读 live page 的 #__NEXT_DATA__ 全量 json（含 buildId）。导航竞态时返回 None 待重试。"""
    try:
        el = page.query_selector("#__NEXT_DATA__")
        if el:
            txt = el.text_content()
            if txt and "listingsData" in txt:
                return json.loads(txt)
    except Exception:  # noqa: BLE001 导航中执行上下文销毁等
        pass
    return None


def _pagedata(j: Optional[dict]) -> Optional[dict]:
    """兼容两种形态：
      __NEXT_DATA__（HTML script）: {props:{pageProps:{pageData:{data}}}}
      _next/data（fetch 接口返回）:   {pageProps:{pageData:{data}}}  ← 少一层 props
    """
    if not isinstance(j, dict):
        return None
    node = j.get("props", j)
    try:
        return node["pageProps"]["pageData"]["data"]
    except (KeyError, TypeError):
        return None


def _load_doc(page, url: str, settle_s: int = 80) -> Optional[dict]:
    """整页导航过 CF + 读 __NEXT_DATA__，返回完整 json。仅 CF 挑战时用（每通道首页/兜底）。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:  # noqa: BLE001
        log(f"[pg] goto 异常 {url[:80]}: {exc}")
    iters = settle_s // 2
    reload_at = iters // 3
    for i in range(iters):
        j = _read_next_json(page)
        if j is not None:
            return j
        if i == reload_at:
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(2000)
    return None


def _load_doc_retry(page, url: str, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries + 1):
        j = _load_doc(page, url)
        if j is not None:
            return j
        if attempt < retries:
            cooldown = 15 + attempt * 15
            log(f"[pg] CF 卡顿，冷却 {cooldown}s 后重试（{attempt + 1}/{retries}）…")
            page.wait_for_timeout(cooldown * 1000)
    try:
        title = (page.title() or "").lower()
        if any(m in title for m in CF_TITLE_MARKERS):
            log(f"[pg] ⚠️ 仍卡在 Cloudflare（title={title[:40]!r}），可手动点选 Turnstile 后会自动继续")
    except Exception:  # noqa: BLE001
        pass
    return None


_FETCH_DEBUG = {"n": 0}  # 仅前几次失败打印诊断，避免刷屏


def _fetch_data(page, data_url: str) -> Optional[dict]:
    """页内 fetch Next.js 的 _next/data JSON（同源、带 cf_clearance、不导航）。
    被 CF 挑战 / 非 200 / 非 JSON 时返回 None（交由整页兜底）。"""
    try:
        res = page.evaluate(
            """async (u) => {
                try {
                    const r = await fetch(u, {headers: {'accept': 'application/json', 'x-nextjs-data': '1'}});
                    const t = await r.text();
                    return {s: r.status, t: t, ct: r.headers.get('content-type') || ''};
                } catch (e) { return {s: -1, t: String(e), ct: ''}; }
            }""", data_url)
    except Exception:  # noqa: BLE001
        return None
    t = (res or {}).get("t") or ""
    status = (res or {}).get("s")
    ok = status == 200 and "listingsData" in t and "just a moment" not in t.lower()
    if not ok and _FETCH_DEBUG["n"] < 3:
        _FETCH_DEBUG["n"] += 1
        snippet = t[:90].replace("\n", " ")
        log(f"[pg] fetch 诊断: status={status} ct={(res or {}).get('ct','')[:30]} "
            f"len={len(t)} 含挑战={'just a moment' in t.lower()} url={data_url[:80]} head={snippet!r}")
    if not ok:
        return None
    try:
        return _pagedata(json.loads(t))
    except Exception:  # noqa: BLE001
        return None


def _crawl_channel(page, label: str, doc_url, data_cands, max_pages: int,
                   raw_all: list, out: list, seen: set, shared: dict) -> None:
    """doc_url(n)->整页URL；data_cands(build,n)->[候选 _next/data URL]。
    shared 跨通道共享 {build, ready}：一旦某通道整页过了 CF，后续通道（含其首页）
    直接复用 buildId + 会话走 fetch —— 整个 scrape 全程理想情况只需过 1 次 CF。
    后续页/已就绪首页优先 fetch _next/data（命中模板后固定复用），失败回退整页。"""
    total = max_pages
    build = shared.get("build")
    tmpl_idx = None  # 记住命中的候选模板下标，后续页直接复用
    for n in range(1, max_pages + 1):
      try:
        pdata = None
        via = "整页"
        # 可 fetch 的条件：已有 buildId 且（非首页 或 CF 会话已就绪）
        if build and (n > 1 or shared.get("ready")):
            cands = data_cands(build, n)
            order = ([tmpl_idx] + [i for i in range(len(cands)) if i != tmpl_idx]
                     if tmpl_idx is not None else list(range(len(cands))))
            for i in order:
                pd = _fetch_data(page, cands[i])
                if pd is not None:
                    pdata, via, tmpl_idx = pd, "fetch", i
                    break
        # 首页 / fetch 失败：整页导航过 CF
        if pdata is None:
            j = _load_doc_retry(page, doc_url(n))
            if j is not None:
                build = build or j.get("buildId")
                shared["build"], shared["ready"] = build, True
                pdata = _pagedata(j)
                via = "整页(过CF)"
        if not pdata:
            log(f"[pg] {label} 第 {n} 页无数据，停止")
            break
        if n == 1:
            tp = (pdata.get("paginationData") or {}).get("totalPages") or max_pages
            total = min(max_pages, tp)
            log(f"[pg] {label}: totalPages={tp}，抓取前 {total} 页（buildId={build}）")
        listings = pdata.get("listingsData") or []
        if not listings:
            break
        got = 0
        for item in listings:
            ld = item.get("listingData") or {}
            rec = _parse(ld)
            if not rec or rec["id"] in seen:
                continue
            seen.add(rec["id"])
            raw_all.append(ld)
            out.append(rec)
            got += 1
        log(f"[pg] {label} 第 {n}/{total} 页 [{via}]：本页 {len(listings)} 条，新增 {got}（累计 {len(out)}）")
        if n >= total:
            break
        # fetch 很轻可少等；整页导航多等以免再触发 CF
        lo, hi = (800, 1800) if via == "fetch" else (3000, 6000)
        page.wait_for_timeout(int(random.uniform(lo, hi)))
      except Exception as exc:  # noqa: BLE001
        # 浏览器/页面中途崩溃(常见于 VPN 抖动)——保住已抓取的，停止本通道
        log(f"[pg] {label} 第 {n} 页异常，停止本通道（已抓 {len(out)} 条）: {str(exc)[:60]}")
        shared["dead"] = True  # 标记浏览器可能已死，后续通道快速跳过
        break


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def scrape(max_pages: int = 22, district_max_pages: int = 40,
           districts: Optional[list] = None) -> list[dict]:
    from playwright.sync_api import sync_playwright

    districts = districts or DISTRICTS
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    raw_all: list[dict] = []
    seen: set = set()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR), headless=False,
            user_agent=config.USER_AGENTS[0], viewport={"width": 1440, "height": 900},
            locale="en-SG",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        shared = {"build": None, "ready": False}  # 跨通道共享 buildId + CF 会话

        # 通道 1：near-ntu 策展板块（近 NTU 合租间/co-living）—— 路径分页
        _crawl_channel(
            page, "near-ntu",
            lambda n: f"{BASE}{NEAR_NTU}" if n == 1 else f"{BASE}{NEAR_NTU}/{n}",
            lambda b, n: [
                f"/_next/data/{b}/property-for-rent/p/near-ntu/{n}.json",
                f"/_next/data/{b}/en-sg/property-for-rent/p/near-ntu/{n}.json",
                f"/_next/data/{b}/en/property-for-rent/p/near-ntu/{n}.json",
            ],
            max_pages, raw_all, out, seen, shared)

        # 通道 2：District 搜索（整租为主，地址精确）—— query 分页
        # buildId 全站通用：复用通道 1 的会话，D22 首页也走 fetch（理想全程 1 次 CF）
        for d in districts:
            if shared.get("dead"):
                log(f"[pg] 浏览器已中断，跳过 {d} 通道")
                break
            durl = f"{BASE}/property-for-rent?districtCode={d}&listingType=rent"
            _crawl_channel(
                page, d,
                lambda n, u=durl: u if n == 1 else f"{u}&page={n}",
                lambda b, n, dc=d: [
                    f"/_next/data/{b}/property-for-rent.json?districtCode={dc}&listingType=rent&page={n}",
                    f"/_next/data/{b}/en-sg/property-for-rent.json?districtCode={dc}&listingType=rent&page={n}",
                ],
                district_max_pages, raw_all, out, seen, shared)

        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    save_raw("propertyguru", "listings", raw_all)
    rooms = sum(1 for r in out if r["listing_type"] == "room")
    whole = len(out) - rooms
    log(f"[pg] 完成：原始 {len(raw_all)} 条 → 解析 {len(out)} 条"
        f"（room {rooms} / whole_unit {whole}，距离待 normalize 地理编码）")
    return out


if __name__ == "__main__":
    import sys
    mp = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    dmp = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    rows = scrape(max_pages=mp, district_max_pages=dmp)
    rooms = [r for r in rows if r["listing_type"] == "room"]
    whole23 = [r for r in rows if r["listing_type"] == "whole_unit" and r.get("bedrooms") in (2, 3)]
    with_addr = [r for r in rows if r.get("address") and not r["address"].startswith("Near")]
    print(f"\nTOTAL: {len(rows)} (rooms {len(rooms)}, whole_unit {len(rows)-len(rooms)}, "
          f"2/3房整租 {len(whole23)}, 带地址 {len(with_addr)})")
    print(json.dumps(whole23[:3], ensure_ascii=False, indent=2))
