"""产出：rent_summary.xlsx（多 sheet）+ REPORT.md（中文分析报告）。

聚焦「一群人整租合住、每人一间独立卧室」场景：
  2 人 → 2 房(1主+1普)；3 人 → 3 房(1主+2普)。
按预算筛选整租单位，按品质排序推荐，并给出每人分摊价（主:普=1.3:1，3房还原4:3:3）。
"""

from __future__ import annotations

import pandas as pd

import config
from src import analyze
from src.core.utils import log

DETAIL_COLS = [
    "distance_km", "distance_is_approx", "source", "listing_type",
    "room_category", "property_name", "title", "price", "total_rent",
    "master_price_est", "common_price_est", "bedrooms", "bathrooms",
    "floor_area_sqft", "score", "address", "url",
]


def _cand_rows(cands: list[dict], beds: int) -> pd.DataFrame:
    rows = []
    for c in cands:
        rows.append({
            "品质分": c["quality"],
            "预算内": "✓" if c.get("in_budget", True) else "✗",
            "类型": c["property_kind"],
            "距NTU(km)": round(c["distance_km"], 2),
            "总月租": int(c["total_rent"]),
            "主人房估": c["master_price_est"],
            f"普通房估(×{beds-1})": c["common_price_est"],
            "主合预算": "✓" if c["master_fit"] else "✗",
            "普合预算": "✓" if c["common_fit"] else "✗",
            "面积sqft": c.get("floor_area_sqft"),
            "配套": ", ".join(c.get("amenities") or []),
            "房源": c.get("property_name"),
            "来源": c.get("source"),
            "链接": c.get("url"),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------
def _benchmark_df(mkt: dict, srx: dict) -> pd.DataFrame:
    rows = []
    for (kind, beds), st in sorted(mkt.get("whole", {}).items()):
        rows.append({"基准": "NTU片区·整租", "类别": f"{kind} {beds}房", "样本": st["n"],
                     "最低": st["min"], "25分位": st["p25"], "中位": st["median"],
                     "75分位": st["p75"], "最高": st["max"]})
    label = {"master": "主人房", "common": "普通房", "single": "单人间",
             "double": "双人/合住", "triple": "三人间"}
    for cat, st in mkt.get("room", {}).items():
        rows.append({"基准": "NTU片区·单间", "类别": label.get(cat, cat), "样本": st["n"],
                     "最低": st["min"], "25分位": st["p25"], "中位": st["median"],
                     "75分位": st["p75"], "最高": st["max"]})
    for kind, d in (srx.get("by_kind") or {}).items():
        rt = d["rent"]
        rows.append({"基准": "SRX全岛(未按NTU过滤)", "类别": f"{kind} (psf {d['psf']})",
                     "样本": rt["n"], "最低": rt["min"], "25分位": rt["p25"],
                     "中位": rt["median"], "75分位": rt["p75"], "最高": rt["max"]})
    return pd.DataFrame(rows)


def _westwood_full_df(df: pd.DataFrame) -> pd.DataFrame:
    """Westwood 片区全部房源（整租 + 单间）明细，整租在前。"""
    sub = df[df.apply(analyze.is_westwood, axis=1)]
    rows = []
    for _, r in sub.iterrows():
        whole = r["listing_type"] == "whole_unit"
        am = r.get("amenities")
        rows.append({
            "类型": "整租" if whole else "单间",
            "楼盘": analyze._development(r),
            "卧室": r.get("bedrooms") if whole else None,
            "月租": int(r["total_rent"]) if whole and pd.notna(r.get("total_rent"))
                    else (int(r["price"]) if pd.notna(r.get("price")) else None),
            "主人房估": r.get("master_price_est"),
            "普通房估": r.get("common_price_est"),
            "距NTU(km)": round(float(r["distance_km"]), 2)
                         if pd.notna(r.get("distance_km")) else None,
            "配套": ", ".join(am) if isinstance(am, list) else "",
            "来源": r.get("source"),
            "房源名": r.get("property_name"),
            "链接": r.get("url"),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["_o"] = (out["类型"] == "单间").astype(int)
    return (out.sort_values(["_o", "卧室", "月租"], na_position="last")
               .drop(columns="_o").reset_index(drop=True))


def build_excel(df: pd.DataFrame, tables: dict, amenity_df: pd.DataFrame,
                cands: dict, mkt: dict | None = None, srx: dict | None = None) -> None:
    detail = df.copy()
    detail["amenities"] = detail["amenities"].apply(
        lambda a: ", ".join(a) if isinstance(a, list) else "")
    cols = [c for c in DETAIL_COLS if c in detail.columns]
    detail = detail[cols + [c for c in detail.columns if c not in cols
                            and c != "amenities"] + ["amenities"]]
    detail = detail.sort_values("distance_km", na_position="last")

    split_df = df[df["listing_type"] == "whole_unit"][[
        "property_name", "distance_km", "bedrooms", "total_rent",
        "master_price_est", "common_price_est", "source", "url",
    ]].sort_values("distance_km", na_position="last")

    price_frames = []
    for name, t in tables.items():
        if t.empty:
            continue
        t = t.copy()
        t.insert(0, "类别", name)
        price_frames.append(t)
    price_all = pd.concat(price_frames, ignore_index=True) if price_frames \
        else pd.DataFrame()

    with pd.ExcelWriter(config.SUMMARY_XLSX, engine="openpyxl") as xw:
        _cand_rows(cands.get(2, []), 2).to_excel(xw, sheet_name="2人合租推荐(2房)", index=False)
        _cand_rows(cands.get(3, []), 3).to_excel(xw, sheet_name="3人合租推荐(3房)", index=False)
        wz_df = _westwood_full_df(df)
        if not wz_df.empty:
            wz_df.to_excel(xw, sheet_name="Westwood专区", index=False)
        price_all.to_excel(xw, sheet_name="价格区间", index=False)
        bench_df = _benchmark_df(mkt or {}, srx or {})
        if not bench_df.empty:
            bench_df.to_excel(xw, sheet_name="市场价格基准", index=False)
        split_df.to_excel(xw, sheet_name="单间拆分(全部整租)", index=False)
        amenity_df.to_excel(xw, sheet_name="配套设施", index=False)
        detail.to_excel(xw, sheet_name="全部明细", index=False)
    log(f"[report] Excel 已生成: {config.SUMMARY_XLSX}")


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def _budget_tag(c: dict, lo: int, hi: int) -> str:
    if c.get("in_budget", True):
        return "✓预算内"
    tot = int(c["total_rent"])
    return f"⚠超{tot - hi}" if tot > hi else f"⚠低{lo - tot}"


def _rec_table(A, cands: list[dict], beds: int, top: int, budget: tuple[int, int]):
    commons = beds - 1
    lo, hi = budget
    A(f"| # | 品质分 | 预算 | 类型 | 距NTU | 总月租 | 主人房 | 普通房×{commons} | 配套 | 房源 | 链接 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(cands[:top], 1):
        am = ", ".join(a for a in (c.get("amenities") or [])
                       if a in ("pool", "gym", "ensuite_bath", "furnished",
                                "aircon", "cooking")) or "—"
        name = (c.get("property_name") or "")[:24]
        A(f"| {i} | {c['quality']} | {_budget_tag(c, lo, hi)} | {c['property_kind']} | "
          f"{c['distance_km']:.2f}km | S${int(c['total_rent'])} | "
          f"S${c['master_price_est']} | S${c['common_price_est']} | {am} | "
          f"{name} | {c.get('url') or ''} |")
    A("")


def _benchmark_section(A, mkt: dict, srx: dict) -> None:
    A("## 五、市场价格基准（挂牌价对照）\n")
    A("用于判断推荐房源是否物有所值。**主基准 = NTU 片区全部在抓房源**（最贴合）；"
      "SRX 为全岛对照（其搜索为硬核 SPA，未能按 NTU 过滤，仅作市场背景）。\n")

    # 主基准：整租
    A("### NTU 片区整租挂牌价（按房型 × 卧室数，SGD/月）\n")
    A("| 房型 | 卧室 | 样本 | 最低 | 25分位 | 中位 | 75分位 | 最高 |")
    A("|---|---|---|---|---|---|---|---|")
    rows = sorted(mkt.get("whole", {}).items(), key=lambda kv: (kv[0][0], kv[0][1]))
    for (kind, beds), st in rows:
        if st["n"] < 2:
            continue
        A(f"| {kind} | {beds}房 | {st['n']} | {st['min']} | {st['p25']} | "
          f"{st['median']} | {st['p75']} | {st['max']} |")
    A("")

    # 主基准：单间
    if mkt.get("room"):
        A("### NTU 片区单间挂牌价（按房型，SGD/月）\n")
        A("| 房型 | 样本 | 最低 | 25分位 | 中位 | 75分位 | 最高 |")
        A("|---|---|---|---|---|---|---|")
        label = {"master": "主人房", "common": "普通房", "single": "单人间",
                 "double": "双人/合住", "triple": "三人间"}
        for cat, st in mkt["room"].items():
            A(f"| {label.get(cat, cat)} | {st['n']} | {st['min']} | {st['p25']} | "
              f"{st['median']} | {st['p75']} | {st['max']} |")
        A("")

    # SRX 全岛对照
    if srx and srx.get("by_kind"):
        A(f"### SRX 全岛挂牌价对照（样本 {srx.get('n', 0)} 条，**未按 NTU 过滤**）\n")
        A("| 房型 | 样本 | 中位挂牌租金 | 中位 psf | 最低 | 最高 |")
        A("|---|---|---|---|---|---|")
        for kind, d in srx["by_kind"].items():
            rt = d["rent"]
            A(f"| {kind} | {rt['n']} | S${rt['median']} | ${d['psf']}/sqft | "
              f"S${rt['min']} | S${rt['max']} |")
        A("> ⚠️ SRX 默认 feed 偏中心区、且仅能干净解析约 1/3 卡片（偏老 condo），"
          "数值仅供“全岛量级”参考，**请以上方 NTU 片区基准为准**。")
        A("")


_AMEN_LABEL = {"pool": "泳池", "gym": "健身房", "ensuite_bath": "独立卫浴",
               "furnished": "家具", "aircon": "空调", "cooking": "可做饭",
               "near_mrt": "近地铁", "parking": "停车", "near_bus": "近巴士",
               "wifi": "wifi"}


def _wz_budget(c: dict) -> str:
    ib = c.get("in_budget")
    if ib is True:
        return "✅预算内"
    if ib is False:
        return "⚠️超预算"
    return "—"


def _westwood_section(A, wz: dict) -> None:
    A("## 七、Westwood 专区（首选片区）\n")
    src = "，".join(f"{k} {v}" for k, v in wz.get("by_source", {}).items())
    A(f"> **Westwood Avenue 一带**（Westwood Residences 执行公寓 + The Floravale 公寓），"
      f"距 NTU 约 **1.4–1.6 km**。共 **{wz['n']}** 条相关房源（{src}）："
      f"整租 **{len(wz['whole'])}** 套（去重后）、单间 **{wz['rooms_n']}** 间。\n")
    A("> 该片区以 3–4 房 condo/EC 为主、几乎没有 2 房 —— **适合 3 人（或 4 人）合租**。\n")

    A("### 整租单位（按卧室数 / 品质排序）\n")
    A("| 卧室 | 楼盘 | 总月租 | 主人房 | 普通房 | 预算 | 距NTU | 品质 | 配套 | 来源 | 链接 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    for c in wz["whole"]:
        beds = f"{c['bedrooms']}房" if c["bedrooms"] else "?"
        m = f"S${c['master_price_est']}" if c.get("master_price_est") else "—"
        cm = f"S${c['common_price_est']}" if c.get("common_price_est") else "—"
        am = "、".join(_AMEN_LABEL.get(a, a) for a in (c.get("amenities") or [])) or "—"
        dist = f"{c['distance_km']:.2f}km" if c.get("distance_km") is not None else "—"
        name = (c.get("property_name") or "")[:22]
        A(f"| {beds} | {c['development']} | S${c['total_rent']} | {m} | {cm} | "
          f"{_wz_budget(c)} | {dist} | {c['quality']} | {am} | {c['source']} | "
          f"{c.get('url') or ''} |")
    A("")

    rs = wz.get("rooms_stats")
    if rs:
        A("### 单间（co-living / 房间出租）\n")
        A("| 样本 | 最低 | 25分位 | 中位 | 75分位 | 最高 |")
        A("|---|---|---|---|---|---|")
        A(f"| {rs['n']} | S${rs['min']} | S${rs['p25']} | S${rs['median']} | "
          f"S${rs['p75']} | S${rs['max']} |")
        A("> 想先单租一间过渡，可按此价位参考；完整单间清单见 Excel「Westwood专区」sheet。")
        A("")


def build_markdown(df: pd.DataFrame, tables: dict, summ: dict, cands: dict,
                   mkt: dict | None = None, srx: dict | None = None,
                   wz: dict | None = None) -> None:
    lines: list[str] = []
    A = lines.append
    mb, cb = config.MASTER_BUDGET, config.COMMON_BUDGET
    wb2, wb3 = config.WHOLE_BUDGET[2], config.WHOLE_BUDGET[3]

    A("# 新加坡 NTU 周边合租房源推荐\n")
    A(f"> 场景：**一群人整租合住，每人一间独立卧室**　|　"
      f"搜索中心 NTU `(1.348, 103.683)`　|　半径 **{config.RADIUS_KM} km**\n")

    A("## 一、需求与预算\n")
    A(f"- **每人意愿价**：主人房 **S${mb[0]}–{mb[1]}**，普通房 **S${cb[0]}–{cb[1]}**（允许极值轻微浮动 ±{int(config.BUDGET_TOLERANCE*100)}%）。")
    A(f"- **2 人合租 → 2 房(1主+1普)**：整套总租金预算 **S${wb2[0]}–{wb2[1]}/月**。")
    A(f"- **3 人合租 → 3 房(1主+2普)**：整套总租金预算 **S${wb3[0]}–{wb3[1]}/月**。")
    A(f"- 单间分摊：主人房 : 普通房 = 1.3 : 1（3 房即 4:3:3，主人房 40% / 普通房各 30%）。\n")

    ib2 = sum(1 for c in cands.get(2, []) if c.get("in_budget"))
    ib3 = sum(1 for c in cands.get(3, []) if c.get("in_budget"))
    cond3 = sum(1 for c in cands.get(3, []) if c["property_kind"] == "condo" and c.get("in_budget"))
    A("## 二、推荐结论（先看这里）\n")
    A(f"- **3 人合租是甜区**：预算内 3 房共 **{ib3} 套**，其中 condo **{cond3} 套**"
      "（带泳池/健身房，品质好），且拆分后每人正好落在预算内。**首选 condo。**")
    A(f"- **2 人合租**：预算内 2 房共 **{ib2} 套**，该价位 NTU 3km 内**没有 condo 两房**，"
      "均为组屋(HDB)；建议挑家具齐全、近 Boon Lay/Pioneer MRT 的较新组屋，或考虑 3 人合租摊薄成本住进 condo。")
    A(f"- 两个场景均按品质分**各列前 20 个**；预算内不足 20 时，用品质排序的“邻近预算”房源补齐（表中「预算」列标 ⚠超/低 SGD）。")
    A("")

    # 3 人推荐
    A(f"## 三、3 人合租推荐（3 房，预算 S${wb3[0]}–{wb3[1]}）\n")
    if cands.get(3):
        A("按品质分排序（condo / 配套 / 面积 / 距离综合），列前 20：\n")
        _rec_table(A, cands[3], 3, 20, wb3)
        A(f"> 预算内 {ib3} 套，列前 20（不足时已用邻近预算补齐）。完整列表见 Excel「3人合租推荐(3房)」sheet。")
    else:
        A("_无符合预算房源。_")
    A("")

    # 2 人推荐
    A(f"## 四、2 人合租推荐（2 房，预算 S${wb2[0]}–{wb2[1]}）\n")
    if cands.get(2):
        A("该价位均为组屋，按品质分排序（家具/空调/近地铁/面积/距离），列前 20：\n")
        _rec_table(A, cands[2], 2, 20, wb2)
        A(f"> 预算内 {ib2} 套；NTU 3km 内 2 房较少，已用品质排序的邻近预算 2 房补齐至 20（「预算」列标注 ⚠）。")
    else:
        A("_无符合预算房源。_")
    A("")

    # 市场价格基准（主：NTU 片区；次：SRX 全岛）
    if mkt:
        _benchmark_section(A, mkt, srx or {})

    # 价格区间（整租，作背景参考）
    A("## 六、价格区间参考（SGD/月）\n")
    for name in ("整租单位",):
        t = tables.get(name)
        if t is None or t.empty:
            continue
        A(f"### {name}（全部 2–4 房）\n")
        A("| 价格区间 | 数量 | 最低 | 中位 | 最高 |")
        A("|---|---|---|---|---|")
        for _, r in t.iterrows():
            A(f"| {r['价格区间']} | {r['数量']} | {r['最低']} | {r['中位']} | {r['最高']} |")
        A("")
    # 每人分摊速查
    A("**每人分摊速查（按总租金 × 权重）**：\n")
    A("| 房型 | 总租金 | 主人房(估) | 普通房(估) |")
    A("|---|---|---|---|")
    for beds, samples in ((2, (3100, 3500, 4000)), (3, (4400, 5000, 5800))):
        for tot in samples:
            m, c = analyze._split_room_prices(tot, beds)
            A(f"| {beds}房 | S${tot} | S${m} | S${c} |")
    A("")

    # Westwood 专区
    if wz and wz.get("n"):
        _westwood_section(A, wz)

    # 注意事项
    A("## 八、注意事项\n")
    by_src = summ["by_source"]
    contributed = ", ".join(f"{k} {v}" for k, v in by_src.items())
    A(f"- **数据来源（3km 内入选）**：{contributed}。"
      "99.co(condo+hdb 整租/单间) + PropertyGuru(near-NTU) 为主力；"
      "roomies(邮编精确定位) + rentinsg(裕廊西/文礼/裕廊东 镇级) 经 Playwright 渲染后补入；"
      "SRX 为全岛挂牌价基准（其搜索为硬核 SPA，未能按 NTU 过滤，仅作市场背景对照）。")
    A("- **拆分为估算**：单间实际价受朝向/楼层/是否独立卫浴影响，主:普=1.3:1 仅作分摊参考；"
      "「主合预算/普合预算」列标 ✓ 表示该套拆分后两类房间都落在你们每人预算内。")
    A(f"- **直线距离**：haversine 算到 NTU；PropertyGuru 房源经 OneMap 按地址地理编码（块号级，较准）。")
    A("- **品质分**：condo +22，泳池/健身房各 +6，家具/独立卫浴 +5，空调/近地铁各 +4，叠加面积与距离；"
      "用于把破旧便宜房压到后面，**不是越便宜越高分**。")
    A("- **去重**：同楼盘同价同房型只保留一条；不同中介挂同一单位可能仍有少量重复。")
    A("- 数据仅供个人租房研究。\n")

    config.REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    log(f"[report] Markdown 报告已生成: {config.REPORT_MD}")


def generate(df: pd.DataFrame, bench_records: list | None = None) -> None:
    if df.empty:
        log("[report] 无数据，跳过报告生成")
        return
    df = analyze.apply_room_split(df)
    df = analyze.value_scores(df)
    tables = analyze.price_range_tables(df)
    amenity_df = analyze.amenities_matrix(df)
    summ = analyze.summarize(df)
    cands = analyze.group_rental_candidates(df)

    mkt = analyze.market_benchmark(df)
    srx = analyze.srx_benchmark(bench_records or [])
    wz = analyze.westwood_zone(df)

    df.to_csv(config.LISTINGS_CSV, index=False)
    log(f"[report] 明细 CSV 已保存: {config.LISTINGS_CSV}")
    build_excel(df, tables, amenity_df, cands, mkt, srx)
    build_markdown(df, tables, summ, cands, mkt, srx, wz)
