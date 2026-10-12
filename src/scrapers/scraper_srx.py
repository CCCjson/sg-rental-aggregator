"""SRX 爬虫（价格基准用，best-effort）。

SRX 是硬核 jQuery SPA：无内嵌结构化数据、无 GET 过滤、DOM 扁平、文本模式不全，
无法可靠按 NTU 区过滤或逐卡对齐。退而求其次：用 Playwright 过 JS 挑战后翻页，
从列表容器纯文本里用正则提取「类型 · 家具 · Built-年 · sqft | $psf · $月租」这类
**能干净解析**的整租挂牌块，作为**市场挂牌价基准**（默认全岛，标注西部命中）。

注意：返回的是“基准样本”，带 is_benchmark=True；run.py/分析里不混入合租推荐，
仅用于生成「市场价格基准」对照表。
"""

from __future__ import annotations

import re

import config
from src.core.utils import log, save_raw

BASE = "https://www.srx.com.sg"
RESULTS = "https://www.srx.com.sg/singapore-property-listings/property-for-rent?page={page}"
USER_DATA_DIR = config.DATA_DIR / "browser_srx"

# 列表块：捕获 类型(Condo/HDB/Apartment...) [· 家具] · Built-年  {sqft} sqft | $psf psf(Built|Land) ${price}
_BLOCK_RE = re.compile(
    r"([A-Za-z][A-Za-z /]*?(?:Condo|HDB|Apartment|Executive Condominium|Walk-up|Landed|Cluster House|Terrace))"
    r"(?:\s*•\s*([A-Za-z ]+?))?"
    r"(?:\s*•\s*Built-(\d{4}))?\s*"
    r"([\d,]+)\s*sqft\s*\|\s*\$[\d.]+\s*psf\([^)]*\)\s*\$([\d,]+)",
    re.I)

# NTU 西部关键词（命中即标记 west_match，便于报告区分本地 vs 全岛）
WEST_KW = ("jurong", "boon lay", "pioneer", "nanyang", "lakeside", "taman jurong",
           "gek poh", "hong kah", "corporation", "yunnan", "clementi", "bukit batok", "tengah")


def _kind(type_text: str) -> str:
    t = type_text.lower()
    if "hdb" in t:
        return "hdb"
    if "condo" in t or "apartment" in t or "executive" in t:
        return "condo"
    return "其他"


def scrape(max_pages: int = 10) -> list[dict]:
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

        for pg in range(1, max_pages + 1):
            try:
                page.goto(RESULTS.format(page=pg),
                          wait_until="domcontentloaded", timeout=50000)
                page.wait_for_timeout(6000)
            except Exception as exc:  # noqa: BLE001
                log(f"[srx] page {pg} 异常: {exc}")
                continue
            data = page.evaluate(
                "() => { const c = document.querySelector('.container.listing-search-results') || document.body;"
                "  return (c.innerText || '').replace(/\\s+/g, ' '); }")
            blocks = _BLOCK_RE.findall(data)
            raw_dump.append({"page": pg, "n_blocks": len(blocks)})
            got = 0
            for typ, furn, built, sqft, price in blocks:
                price_i = int(price.replace(",", ""))
                sqft_i = int(sqft.replace(",", ""))
                if price_i < 800 or price_i > 30000 or sqft_i < 100:
                    continue
                key = (typ.strip(), sqft_i, price_i)
                if key in seen:
                    continue
                seen.add(key)
                # west 命中：在该块前后窗口里找西部关键词
                idx = data.find(f"{sqft} sqft")
                window = data[max(0, idx - 90): idx + 30].lower() if idx >= 0 else ""
                west = any(k in window for k in WEST_KW)
                out.append({
                    "source": "srx",
                    "is_benchmark": True,
                    "property_kind": _kind(typ),
                    "type_text": typ.strip(),
                    "furnishing": (furn or "").strip().lower() or None,
                    "built_year": int(built) if built else None,
                    "floor_area_sqft": sqft_i,
                    "price": price_i,
                    "psf": round(price_i / sqft_i, 2),
                    "west_match": west,
                })
                got += 1
            log(f"[srx] page {pg}: 干净块 {len(blocks)} → 入样 {got}（累计 {len(out)}，"
                f"西部 {sum(1 for r in out if r['west_match'])}）")
            page.wait_for_timeout(600)

        ctx.close()

    save_raw("srx", "benchmark", raw_dump)
    log(f"[srx] 完成：基准样本 {len(out)} 条（西部命中 "
        f"{sum(1 for r in out if r['west_match'])}）")
    return out


if __name__ == "__main__":
    import json
    from statistics import median
    rows = scrape(max_pages=8)
    print(f"\nTOTAL benchmark rows: {len(rows)}")
    for kind in ("condo", "hdb"):
        sub = [r["price"] for r in rows if r["property_kind"] == kind]
        if sub:
            print(f"  {kind}: n={len(sub)} median=${int(median(sub))} "
                  f"min=${min(sub)} max=${max(sub)}")
    print("west sample:", json.dumps([r for r in rows if r["west_match"]][:4], ensure_ascii=False))
