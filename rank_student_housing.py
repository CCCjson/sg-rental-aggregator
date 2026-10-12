"""给近 NTU 学生公寓/co-living 排名：基础设施 + 距离 + 价格 + 运营方。

读取 data/coliving_ntu.json（uhomes 采集结果），逐栋重新抓 uhomes 详情页的
"Facilities:" 文本块，检测设施集合，按加权打分排序，产出：
  data/STUDENT_HOUSING_RANKED.md（排名表 + 评分口径）

评分（满分 100）：
  基础设施 0–45 | 距 NTU 0–30 | 价格性价比 0–15 | 运营方/学生友好 0–10
"""
from __future__ import annotations

import html
import json
import re
import time

import config
from src.core import client
from src.core.utils import log, save_json

SRC = config.DATA_DIR / "coliving_ntu.json"
MD_OUT = config.DATA_DIR / "STUDENT_HOUSING_RANKED.md"
JSON_OUT = config.DATA_DIR / "student_housing_ranked.json"

# 设施关键词 → (标签, 权重)。在「Facilities:」文本块里检测。
AMENITIES = [
    ("gym", r"\bgym\b|fitness", "健身房", 8),
    ("pool", r"swimming\s*pool|\bpool\b", "泳池", 7),
    ("study", r"reading\s*room|study\s*room|study\s*area|library|co-?working", "自习/阅读室", 6),
    ("ensuite", r"independent\s*bathroom|en\W?suite|private\s*bathroom|own\s*bathroom", "独立卫浴", 6),
    ("laundry", r"laundry|washing\s*machine|washer|dryer", "洗衣房", 5),
    ("kitchen", r"\bkitchen\b|induction|cooker", "厨房", 5),
    ("security", r"smart\s*lock|digital\s*(?:smart\s*)?lock|cctv|24[\s-]*hour|security|guard", "门禁/安保", 5),
    ("food", r"supermarket|food\s*court|hawker|\bmall\b|jurong\s*point|\bjem\b|\bimm\b|amenities\s*nearby", "近超市/食阁", 4),
    ("parking", r"car\s*park|parking", "停车", 3),
    ("furnished", r"wardrobe|\bdesk\b|furnished|refrigerator|\bfridge\b|television|\btv\b", "家具齐全", 3),
    ("bbq", r"\bbbq\b|barbecue|barbeque", "BBQ", 2),
    ("wifi", r"wi-?fi|wireless\s*network|unlimited\s*(?:wireless|internet)|\binternet\b", "WiFi", 2),
    ("aircon", r"air[\s-]*condition|air[\s-]*con\b", "空调", 2),
]
FACILITY_CAP = 45

# 真·学生公寓/co-living 运营方（灵活短租、免中介、社群）
_STUDENT_OP = re.compile(r"coliwoo|westwood\s*hostel|hostel|co-?living|blk979", re.I)

# 物业类型（uhomes 详情页设施文本对 condo/HDB 不可靠，按楼盘名推断基线设施）
_CONDO_RE = re.compile(
    r"floravale|centris|caspian|lakefront|lakeville|lake\s*grande|parc\s*vista|"
    r"summerdale|westwood\s*residences|lakeshore|gardens|residences|condominium|condo",
    re.I)
_COLIVING_RE = re.compile(r"coliwoo|westwood\s*hostel|hostel", re.I)

# 按类型的基线设施（condo 普遍配套；co-living 走 uhomes 文本；HDB 基础）
_BASELINE = {
    "condo": ["pool", "gym", "security", "parking", "bbq"],
    "coliving": ["gym", "security", "laundry", "wifi"],
    "hdb": [],
}


def _kind(name: str) -> str:
    if _COLIVING_RE.search(name or ""):
        return "coliving"
    if _CONDO_RE.search(name or ""):
        return "condo"
    return "hdb"

_FAC_RE = re.compile(r"Facilities?\s*[:：](.*?)(?:House\s*Rules|Fees|Location|Price\s*Trend|Floor\s*Plan|$)",
                     re.I | re.S)


def _facilities_text(page_text: str) -> str:
    flat = re.sub(r"<[^>]+>", " ", page_text)
    flat = re.sub(r"\s+", " ", html.unescape(flat))
    m = _FAC_RE.search(flat)
    seg = m.group(1) if m else flat
    return seg[:2500]


def _detect(fac_text: str) -> list[str]:
    low = fac_text.lower()
    found = []
    for key, pat, _label, _w in AMENITIES:
        if re.search(pat, low, re.I):
            found.append(key)
    return found


def _facility_score(keys: list[str]) -> int:
    s = sum(w for k, _p, _l, w in AMENITIES if k in keys)
    return min(FACILITY_CAP, s)


def _distance_score(dist) -> float:
    if dist is None:
        return 0.0
    return round(max(0.0, (5.0 - float(dist)) / 5.0 * 30.0), 1)


def _value_score(price_low) -> int:
    if not price_low:
        return 6
    p = float(price_low)
    for thr, sc in [(700, 15), (1000, 13), (1300, 10), (1700, 7), (2300, 4)]:
        if p <= thr:
            return sc
    return 2


def _operator_score(name: str, operator) -> int:
    if operator or _STUDENT_OP.search(name or ""):
        return 10           # 真·学生公寓/co-living 运营方
    if re.search(r"BESPOKE|-\s*(?:My|WB|WPL|LL|Allan|Nova)\b", name or "", re.I):
        return 6            # uhomes 托管房（统一管理、配套较全）
    return 3                # 普通中介散租间


_LABEL = {k: l for k, _p, l, _w in AMENITIES}


def main():
    props = json.load(open(SRC, encoding="utf-8"))
    ranked = []
    for n, p in enumerate(props, 1):
        url = p["url"]
        resp = client.get(url, headers=client.build_headers(
            extra={"accept": "text/html,*/*", "sec-fetch-mode": "navigate"}))
        text_keys = []
        if resp is not None and resp.status_code == 200:
            text_keys = _detect(_facilities_text(resp.text))
        # 是否有 ensuite 房型
        has_ensuite = any(rt.get("ensuite") for rt in p.get("room_types_aggregated", []))
        # 合并：楼盘类型基线 ∪ uhomes 文本检测 ∪ ensuite
        kind = _kind(p["name"])
        keys = sorted(set(_BASELINE[kind]) | set(text_keys)
                      | ({"ensuite"} if has_ensuite else set()))
        fac = _facility_score(keys)
        dist_s = _distance_score(p["distance_km"])
        val_s = _value_score(p.get("price_low"))
        op_s = _operator_score(p["name"], p.get("operator"))
        total = round(fac + dist_s + val_s + op_s, 1)
        ranked.append({
            "name": p["name"], "operator": p.get("operator"), "kind": kind,
            "distance_km": p["distance_km"], "price_low": p.get("price_low"),
            "price_high": p.get("price_high"), "url": url,
            "facilities": keys, "has_ensuite": has_ensuite,
            "score_facility": fac, "score_distance": dist_s,
            "score_value": val_s, "score_operator": op_s, "score_total": total,
        })
        log(f"[rank] [{n}/{len(props)}] {p['name'][:34]:34} 总分 {total} "
            f"(设施{fac}/距{dist_s}/价{val_s}/运营{op_s}) 设施={keys}")
        time.sleep(0.7)

    ranked.sort(key=lambda x: -x["score_total"])
    save_json(ranked, JSON_OUT)
    _write_md(ranked)
    print(f"\n排名完成 → {MD_OUT}")
    for i, r in enumerate(ranked[:10], 1):
        print(f"  #{i:>2} {r['score_total']:>5} | {r['distance_km']}km | "
              f"S${r['price_low']} | {r['name'][:38]}")


def _clean_name(n: str) -> str:
    n = re.split(r",\s*Singapore", n)[0]
    return n.replace("|", "/").strip()


def _write_md(ranked: list[dict]):
    lines = ["# NTU 学生公寓 · 综合排名(基础设施 + 距离 + 价格 + 运营方)", ""]
    lines.append("> 评分满分 100 = **基础设施 45** + **距 NTU 30** + **价格性价比 15** + "
                 "**运营方/学生友好 10**。基础设施按健身房/泳池/自习室/独立卫浴/洗衣房/厨房/"
                 "门禁安保/近超市食阁/停车/家具/BBQ/WiFi/空调 加权(取自各楼 uhomes「Facilities」)。"
                 "数据抓取于 2026-06-04,库存与价格动态变化,价格为「起价」近似。")
    lines.append("")
    _KIND_CN = {"condo": "公寓", "coliving": "学生公寓", "hdb": "组屋"}
    lines.append("| # | 综合分 | 房源 | 类型 | 距NTU | 起价/间 | 基础设施(关键) | 设施分 | 距离分 | 价格分 | 运营分 | 链接 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(ranked, 1):
        fac_labels = "、".join(_LABEL[k] for k in r["facilities"][:6]) or "—"
        price = f"S${r['price_low']:,}" if r.get("price_low") else "—"
        lines.append(
            f"| {i} | **{r['score_total']}** | {_clean_name(r['name'])}"
            f"{(' ('+r['operator']+')') if r['operator'] else ''} | {_KIND_CN.get(r['kind'], r['kind'])} | "
            f"{r['distance_km']:.2f}km | "
            f"{price} | {fac_labels} | {r['score_facility']} | {r['score_distance']} | "
            f"{r['score_value']} | {r['score_operator']} | [详情]({r['url']}) |")
    lines.append("")
    lines.append("## 评分口径")
    lines.append("")
    lines.append("- **基础设施(45)**:健身房 8 · 泳池 7 · 自习/阅读室 6 · 独立卫浴 6 · 洗衣房 5 · "
                 "厨房 5 · 门禁安保 5 · 近超市/食阁 4 · 停车 3 · 家具 3 · BBQ 2 · WiFi 2 · 空调 2(封顶 45)")
    lines.append("- **距 NTU(30)**:(5 − 距离km) / 5 × 30,越近越高")
    lines.append("- **价格性价比(15)**:起价 ≤700→15、≤1000→13、≤1300→10、≤1700→7、≤2300→4、更高→2")
    lines.append("- **运营方(10)**:真·学生公寓/co-living(Coliwoo/Westwood Hostel/BLK979 等)10 · "
                 "uhomes 托管房 6 · 普通散租间 3")
    lines.append("")
    lines.append("> 数据仅供个人租房研究;约看前请向运营方/中介核实该房型实际价格与空房。")
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")
    log(f"[rank] 已写出 {MD_OUT.name}")


if __name__ == "__main__":
    main()
