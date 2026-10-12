"""总编排：依次运行各站爬虫 → 归一化（地理编码+半径过滤）→ 分析 → 产出 Excel/Markdown。

用法：
  python run.py                 # 全部站点
  python run.py --sites 99co,propertyguru
  python run.py --pages 25      # 每站最大页数
  python run.py --headless      # 99.co 用无头（可能被 Cloudflare 拦，默认 headful）
  python run.py --reuse 99co,propertyguru   # 这些站复用 data/raw 已存数据，其余实时抓
  python run.py --from-raw      # 全部复用 raw（仅 99co/pg 支持离线重建）
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

import config
from src import normalize, report
from src.core import geo
from src.core.utils import dedup_by_url, log, save_json

ALL_SITES = ["99co", "propertyguru", "roomies", "rentinsg", "srx",
             "edgeprop", "carousell", "ohmyhome"]


def run_site(name: str, pages: int, headless: bool) -> list[dict]:
    try:
        if name == "99co":
            from src.scrapers import scraper_99co
            return scraper_99co.scrape(max_pages=pages, headless=headless)
        if name == "propertyguru":
            from src.scrapers import scraper_propertyguru
            return scraper_propertyguru.scrape(max_pages=pages)
        if name == "roomies":
            from src.scrapers import scraper_roomies
            return scraper_roomies.scrape()
        if name == "rentinsg":
            from src.scrapers import scraper_rentinsg
            return scraper_rentinsg.scrape()
        if name == "srx":
            from src.scrapers import scraper_srx
            return scraper_srx.scrape()
        if name == "edgeprop":
            from src.scrapers import scraper_edgeprop
            return scraper_edgeprop.scrape(max_pages=min(pages, 8))
        if name == "carousell":
            from src.scrapers import scraper_carousell
            return scraper_carousell.scrape()
        if name == "ohmyhome":
            from src.scrapers import scraper_ohmyhome
            return scraper_ohmyhome.scrape(max_pages=pages)
    except Exception as exc:  # noqa: BLE001
        log(f"[run] 站点 {name} 抓取异常（已跳过）: {exc}")
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default=",".join(ALL_SITES))
    ap.add_argument("--pages", type=int, default=25)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--from-raw", action="store_true",
                    help="跳过抓取，直接用 data/raw 下已保存数据重跑分析")
    ap.add_argument("--reuse", default="",
                    help="逗号分隔：这些站复用 data/raw（仅 99co/pg 支持），其余实时抓")
    args = ap.parse_args()

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    reuse = {s.strip() for s in args.reuse.split(",") if s.strip()}
    raw_records: list[dict] = []

    if args.from_raw:
        raw_records = _load_from_raw(sites)
    else:
        if reuse:
            raw_records.extend(_load_from_raw([s for s in sites if s in reuse]))
        for site in sites:
            if site in reuse:
                continue
            log(f"========== 抓取 {site} ==========")
            recs = run_site(site, args.pages, not args.headless and False or args.headless)
            log(f"[run] {site}: {len(recs)} 条")
            raw_records.extend(recs)

    # 基准样本（SRX）单独剥离，不进归一化/半径过滤/合租推荐
    bench_records = [r for r in raw_records if r.get("is_benchmark")]
    raw_records = [r for r in raw_records if not r.get("is_benchmark")]
    if bench_records:
        save_json(bench_records, config.DATA_DIR / "benchmark.json")
        log(f"[run] 基准样本（SRX）{len(bench_records)} 条，单独保存")

    raw_records = dedup_by_url(raw_records)
    log(f"[run] 合并去重后共 {len(raw_records)} 条，开始归一化 + 地理编码 …")

    norm = normalize.normalize_all(raw_records)
    # 半径过滤：保留 distance_km <= RADIUS 的（无距离的丢弃）
    kept = [n for n in norm
            if n.get("distance_km") is not None
            and n["distance_km"] <= config.RADIUS_KM]
    dropped = len(norm) - len(kept)
    log(f"[run] 半径 {config.RADIUS_KM}km 过滤：保留 {len(kept)}，剔除 {dropped}")

    save_json(kept, config.DATA_DIR / "normalized.json")
    df = pd.DataFrame(kept)
    report.generate(df, bench_records)

    # 控制台小结
    log("========== 完成 ==========")
    by_src = df["source"].value_counts().to_dict()
    log(f"[run] 来源分布: {by_src}")
    log(f"[run] 产出: {config.SUMMARY_XLSX}")
    log(f"[run] 产出: {config.REPORT_MD}")


def _load_from_raw(sites: list[str]) -> list[dict]:
    """从已保存 raw 文件重建记录（用于离线重跑分析）。"""
    out: list[dict] = []
    if "99co" in sites:
        from src.scrapers.scraper_99co import _parse as p99
        raw = json.load(open(config.RAW_DIR / "99co_listings.json"))
        for r in raw:
            rec = p99(r)
            if rec:
                rec["distance_km"] = geo.distance_to_ntu(rec["lat"], rec["lng"])
                rec["distance_is_approx"] = False
                out.append(rec)
    if "propertyguru" in sites:
        from src.scrapers.scraper_propertyguru import _parse as ppg
        raw = json.load(open(config.RAW_DIR / "propertyguru_listings.json"))
        out += [r for r in (ppg(x) for x in raw) if r]
    log(f"[run] 从 raw 载入 {len(out)} 条（仅 99co/pg 支持离线重建）")
    return out


if __name__ == "__main__":
    main()
