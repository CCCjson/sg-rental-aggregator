"""分析：4:3:3 拆分、价格区间表、配套设施矩阵、性价比评分。"""

from __future__ import annotations

import re
from statistics import pstdev
from typing import Optional

import pandas as pd

import config


# --------------------------------------------------------------------------
# 整租 → 房型(condo/hdb) 识别
# --------------------------------------------------------------------------
def property_kind(row) -> str:
    blob = " ".join(str(row.get(k) or "") for k in
                    ("url", "property_name", "address", "title"))
    if re.search(r"-condo-|\bcondo\b|residences|centris|caspian|lakefront|"
                 r"parc|westwood|floravale|gardens|lakeholmz|regent|"
                 r"apartment|执行公寓|公寓", blob, re.I):
        return "condo"
    if re.search(r"-hdb-|\bhdb\b|jurong west (street|st|ctrl|ave)|"
                 r"boon lay (dr|drive|ave|place)|组屋", blob, re.I):
        return "hdb"
    return "其他"


def _split_room_prices(total: float, beds: int):
    """按 主:普 = MASTER_WEIGHT:COMMON_WEIGHT 拆单间价。3房还原 4:3:3。"""
    commons = beds - 1
    denom = config.MASTER_WEIGHT + config.COMMON_WEIGHT * commons
    master = total * config.MASTER_WEIGHT / denom
    common = total * config.COMMON_WEIGHT / denom
    return round(master), round(common)


# --------------------------------------------------------------------------
# 1) 单间拆分：整租单位 → 主人房 / 普通房 单间估价
# --------------------------------------------------------------------------
def apply_room_split(df: pd.DataFrame) -> pd.DataFrame:
    def split(row):
        if row.get("listing_type") != "whole_unit":
            return pd.Series({"master_price_est": None, "common_price_est": None})
        total = row.get("total_rent")
        beds = row.get("bedrooms")
        if total is None or pd.isna(total):
            return pd.Series({"master_price_est": None, "common_price_est": None})
        try:
            total = float(total)
            beds = int(beds) if beds and not pd.isna(beds) else None
        except (TypeError, ValueError):
            return pd.Series({"master_price_est": None, "common_price_est": None})
        if not beds or beds < 2:
            return pd.Series({"master_price_est": round(total),
                              "common_price_est": None})
        m, c = _split_room_prices(total, beds)
        return pd.Series({"master_price_est": m, "common_price_est": c})

    splits = df.apply(split, axis=1)
    df["master_price_est"] = splits["master_price_est"]
    df["common_price_est"] = splits["common_price_est"]
    return df


# --------------------------------------------------------------------------
# 品质评分（解决"太破"：奖励 condo / 家具 / 配套 / 面积 / 近距离）
# --------------------------------------------------------------------------
_AMENITY_WEIGHT = {
    "pool": 6, "gym": 6, "ensuite_bath": 5, "furnished": 5, "aircon": 4,
    "cooking": 3, "parking": 2, "near_mrt": 4, "near_bus": 2, "wifi": 2,
}


_STALE_RE = re.compile(r"\b(\d+)\s*(year|yr|mth|month)s?\s*ago\b", re.I)


def staleness_penalty(row) -> float:
    """挂牌时间惩罚：'X years ago' 多是 99co 把旧帖翻出来（很可能已租出）；
    'X mths ago' 略旧。返回负分。天/周/ISO 日期/近期 视为新，无惩罚。"""
    p = str(row.get("posted_date") or "")
    m = _STALE_RE.search(p)
    if not m:
        return 0.0
    unit = m.group(2).lower()
    if unit in ("year", "yr"):
        return -18.0
    return -6.0  # mth/month


def quality_score(row) -> float:
    score = 0.0
    area = row.get("floor_area_sqft")
    area_val = float(area) if area and not pd.isna(area) else None
    # condo 加分：但 2 房面积 <600sqft 多为标错的小户/组屋，不给 condo 红利
    if property_kind(row) == "condo" and not (
            (row.get("bedrooms") in (2,)) and area_val is not None and area_val < 600):
        score += 22            # condo 普遍有物业/泳池/健身房
    for a in (row.get("amenities") or []):
        score += _AMENITY_WEIGHT.get(a, 0)
    # 面积加分（每 100 sqft 约 +2，封顶 16）
    if area_val is not None:
        score += min(16, area_val / 100 * 2)
    # 距离：越近越好（3km 内线性 0~18）
    d = row.get("distance_km")
    if d is not None and not pd.isna(d):
        score += max(0, (config.RADIUS_KM - float(d)) / config.RADIUS_KM * 18)
    # 挂牌时间惩罚（旧帖很可能已租出/下架）
    score += staleness_penalty(row)
    return round(score, 1)


def _within(total, lo, hi, tol=config.BUDGET_TOLERANCE):
    return lo * (1 - tol) <= total <= hi * (1 + tol)


_POSTAL_RE = re.compile(r"\b(\d{6})\b")


def _postal_of(r: dict) -> Optional[str]:
    for k in ("address", "title", "property_name"):
        m = _POSTAL_RE.search(str(r.get(k) or ""))
        if m:
            return m.group(1)
    return None


def _dedup_units(rows: list[dict]) -> list[dict]:
    """跨源去重：优先按 (6 位邮编, 价格) —— 同一套房 99co 与 PropertyGuru 各挂一次，
    地址写法不同但邮编+价格相同。无邮编时回退 (楼盘名, 价格, 卧室数)。
    保留信息更全的一条（有面积/家具/较新者）。"""
    def _richness(r):
        p = str(r.get("posted_date") or "").lower()
        fresh = 0 if _STALE_RE.search(p) else 1
        return (1 if r.get("floor_area_sqft") else 0) + (1 if r.get("furnishing") else 0) + fresh

    best: dict = {}
    order: list = []
    for r in rows:
        postal = _postal_of(r)
        key = (postal, r.get("price")) if postal else \
              (r.get("property_name"), r.get("price"), r.get("bedrooms"))
        if key not in best:
            best[key] = r
            order.append(key)
        elif _richness(r) > _richness(best[key]):
            best[key] = r
    return [best[k] for k in order]


def group_rental_candidates(df: pd.DataFrame, min_fill: int = 20) -> dict[int, list[dict]]:
    """返回 {2: [...2房候选...], 3: [...3房候选...]}，按品质排序+去重。

    预算内（±BUDGET_TOLERANCE）的房源标 in_budget=True 排前；若不足 min_fill，
    用品质排序的“邻近预算”同房型单位补齐（in_budget=False，明确标注略超/低于预算），
    使每个场景至少能展示 min_fill 个推荐。
    """
    out: dict[int, list[dict]] = {}
    whole = df[df["listing_type"] == "whole_unit"].copy()
    for n_people, (lo, hi) in config.WHOLE_BUDGET.items():
        beds = n_people  # 2 人→2 房，3 人→3 房
        sub = whole[whole["bedrooms"] == beds]

        def _mk(r) -> Optional[dict]:
            total = r.get("total_rent")
            if total is None or pd.isna(total):
                return None
            total = float(total)
            d = dict(r)
            d["property_kind"] = property_kind(r)
            d["quality"] = quality_score(r)
            m, c = _split_room_prices(total, beds)
            d["master_price_est"], d["common_price_est"] = m, c
            d["master_fit"] = config.MASTER_BUDGET[0] * 0.9 <= m <= config.MASTER_BUDGET[1] * 1.1
            d["common_fit"] = config.COMMON_BUDGET[0] * 0.9 <= c <= config.COMMON_BUDGET[1] * 1.1
            d["in_budget"] = _within(total, lo, hi)
            return d

        rows = [d for d in (_mk(r) for _, r in sub.iterrows()) if d]
        rows = _dedup_units(rows)
        # 预算内优先，其次品质；in_budget 内部按品质→距离，补齐区按“离预算近 + 品质”
        in_b = sorted([d for d in rows if d["in_budget"]],
                      key=lambda x: (-x["quality"], x["distance_km"]))
        out_b = [d for d in rows if not d["in_budget"]]
        for d in out_b:
            tot = float(d["total_rent"])
            d["_over"] = (tot - hi) if tot > hi else (lo - tot)  # 超出预算的绝对额
        out_b = sorted(out_b, key=lambda x: (max(0, x["_over"]) // 300, -x["quality"],
                                             x["distance_km"]))
        cands = in_b + (out_b if len(in_b) < min_fill else [])
        out[n_people] = cands
    return out


# --------------------------------------------------------------------------
# Westwood 专区（用户首选片区：Westwood Residences EC + The Floravale + Westwood Ave）
# --------------------------------------------------------------------------
_WESTWOOD_RE = re.compile(r"westwood|floravale", re.I)


def is_westwood(row) -> bool:
    blob = " ".join(str(row.get(k) or "") for k in
                    ("property_name", "title", "address", "url"))
    return bool(_WESTWOOD_RE.search(blob))


def _development(row) -> str:
    blob = " ".join(str(row.get(k) or "") for k in
                    ("property_name", "title", "url")).lower()
    if "westwood residences" in blob:
        return "Westwood Residences(EC)"
    if "floravale" in blob:
        return "The Floravale"
    return "Westwood Ave 其他"


def westwood_zone(df: pd.DataFrame) -> dict:
    """Westwood 片区聚焦：整租（去重、按卧室数+品质排序、含分摊与预算判定）+ 单间统计。"""
    sub = df[df.apply(is_westwood, axis=1)].copy()
    whole: list[dict] = []
    seen = set()
    for _, r in sub[sub["listing_type"] == "whole_unit"].iterrows():
        total = r.get("total_rent")
        if total is None or pd.isna(total):
            continue
        beds = r.get("bedrooms")
        beds_i = int(beds) if beds is not None and not pd.isna(beds) else None
        key = (r.get("property_name"), int(float(total)), beds_i)
        if key in seen:
            continue
        seen.add(key)
        d = {
            "development": _development(r),
            "bedrooms": beds_i,
            "total_rent": int(float(total)),
            "distance_km": round(float(r["distance_km"]), 2)
                           if pd.notna(r.get("distance_km")) else None,
            "quality": quality_score(r),
            "amenities": [a for a in (r.get("amenities") or []) if a in _AMENITY_WEIGHT],
            "source": r.get("source"),
            "url": r.get("url"),
            "property_name": r.get("property_name"),
            "master_price_est": None, "common_price_est": None, "in_budget": None,
        }
        if beds_i and beds_i >= 2:
            m, c = _split_room_prices(float(total), beds_i)
            d["master_price_est"], d["common_price_est"] = m, c
            if beds_i in config.WHOLE_BUDGET:
                lo, hi = config.WHOLE_BUDGET[beds_i]
                d["in_budget"] = _within(float(total), lo, hi)
        whole.append(d)
    whole.sort(key=lambda x: (x["bedrooms"] or 99, -x["quality"], x["total_rent"]))

    room_prices = sub[sub["listing_type"] == "room"]["price"].dropna().tolist()
    return {
        "n": len(sub),
        "whole": whole,
        "rooms_n": int((sub["listing_type"] == "room").sum()),
        "rooms_stats": _stats(room_prices),
        "by_source": {k: int(v) for k, v in sub["source"].value_counts().items()},
    }


# --------------------------------------------------------------------------
# 2) 价格区间表
# --------------------------------------------------------------------------
def _bucket(prices: list[float]) -> pd.DataFrame:
    s = pd.Series([p for p in prices if p is not None and not pd.isna(p)])
    if s.empty:
        return pd.DataFrame(columns=["价格区间", "数量", "最低", "中位", "最高"])
    cats = pd.cut(s, bins=config.PRICE_BINS, labels=config.PRICE_BIN_LABELS,
                  right=False)
    rows = []
    for label in config.PRICE_BIN_LABELS:
        vals = s[cats == label]
        if len(vals) == 0:
            continue
        rows.append({
            "价格区间": label, "数量": int(len(vals)),
            "最低": int(vals.min()), "中位": int(vals.median()),
            "最高": int(vals.max()),
        })
    return pd.DataFrame(rows)


def price_range_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """按类别返回价格区间表。"""
    tables: dict[str, pd.DataFrame] = {}

    # 整租单位
    whole = df[df["listing_type"] == "whole_unit"]
    tables["整租单位"] = _bucket(whole["total_rent"].tolist())

    # 主人房（实际单间 + 整租拆分估）
    master_actual = df[(df["listing_type"] == "room") &
                       (df["room_category"] == "master")]["price"].tolist()
    master_est = whole["master_price_est"].dropna().tolist()
    tables["主人房(实际+拆分估)"] = _bucket(master_actual + master_est)

    # 普通房（实际 + 拆分估）
    common_actual = df[(df["listing_type"] == "room") &
                       (df["room_category"] == "common")]["price"].tolist()
    common_est = whole["common_price_est"].dropna().tolist()
    tables["普通房(实际+拆分估)"] = _bucket(common_actual + common_est)

    # 单/双/三人间（实际房源）
    for cat, name in (("single", "单人间"), ("double", "双人间"), ("triple", "三人间")):
        vals = df[(df["listing_type"] == "room") &
                  (df["room_category"] == cat)]["price"].tolist()
        tables[name] = _bucket(vals)

    return tables


# --------------------------------------------------------------------------
# 3) 配套设施矩阵
# --------------------------------------------------------------------------
def amenities_matrix(df: pd.DataFrame) -> pd.DataFrame:
    from src.normalize import AMENITY_PATTERNS
    cols = list(AMENITY_PATTERNS.keys())
    rows = []
    for _, r in df.iterrows():
        am = set(r.get("amenities") or [])
        row = {
            "房源": (r.get("title") or r.get("property_name") or r.get("url") or "")[:50],
            "来源": r.get("source"),
            "距NTU(km)": r.get("distance_km"),
            "价格": r.get("price"),
        }
        for c in cols:
            row[c] = "✓" if c in am else ""
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 4) 性价比评分（同类内：价格越低、距离越近 → 分越高）
# --------------------------------------------------------------------------
def value_scores(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    def group_key(r):
        if r["listing_type"] == "whole_unit":
            return f"whole_{r.get('bedrooms') or '?'}"
        return f"room_{r.get('room_category') or 'NA'}"

    df["_grp"] = df.apply(group_key, axis=1)
    df["score"] = 0.0

    for grp, sub in df.groupby("_grp"):
        prices = sub["price"].dropna()
        dists = sub["distance_km"].dropna()
        if len(prices) == 0:
            continue
        p_mean, p_sd = prices.mean(), (pstdev(prices) or 1)
        d_mean = dists.mean() if len(dists) else config.RADIUS_KM
        d_sd = (pstdev(dists) or 1) if len(dists) > 1 else 1
        for idx, r in sub.iterrows():
            price = r["price"]
            dist = r["distance_km"]
            if price is None or pd.isna(price):
                continue
            # 价格 z：越便宜越好（取负）
            pz = -(price - p_mean) / p_sd
            # 距离 z：越近越好（取负）
            dz = 0.0
            if dist is not None and not pd.isna(dist):
                dz = -(dist - d_mean) / d_sd
            raw = 0.6 * pz + 0.4 * dz  # 价格权重略高
            score = max(0, min(100, 50 + raw * 18))
            df.at[idx, "score"] = round(score, 1)

    df = df.drop(columns=["_grp"])
    return df.sort_values("score", ascending=False)


def _stats(vals: list[float]) -> Optional[dict]:
    s = pd.Series([v for v in vals if v is not None and not pd.isna(v)])
    if s.empty:
        return None
    return {
        "n": int(len(s)), "min": int(s.min()),
        "p25": int(s.quantile(0.25)), "median": int(s.median()),
        "p75": int(s.quantile(0.75)), "max": int(s.max()),
    }


def market_benchmark(df: pd.DataFrame) -> dict:
    """用 NTU 片区全部房源算市场挂牌价基准（主基准，NTU 专属）。

    返回 {
      "whole": {(kind, beds): stats},   # 整租：condo/hdb × 卧室数
      "room": {category: stats},        # 单间：master/common/single/double...
      "by_source": {source: n},
    }
    """
    out = {"whole": {}, "room": {}, "by_source": {}}
    whole = df[df["listing_type"] == "whole_unit"].copy()
    for _, r in whole.iterrows():
        beds = r.get("bedrooms")
        if beds is None or pd.isna(beds):
            continue
        kind = property_kind(r)
        key = (kind, int(beds))
        out["whole"].setdefault(key, []).append(r.get("total_rent"))
    out["whole"] = {k: _stats(v) for k, v in out["whole"].items() if _stats(v)}

    rooms = df[df["listing_type"] == "room"]
    for cat in ("master", "common", "single", "double", "triple"):
        st = _stats(rooms[rooms["room_category"] == cat]["price"].tolist())
        if st:
            out["room"][cat] = st

    out["by_source"] = df["source"].value_counts().to_dict()
    return out


def srx_benchmark(bench_records: list[dict]) -> dict:
    """SRX 全岛挂牌价对照（次要，未能按 NTU 过滤）。按房型给挂牌租金 + psf 统计。"""
    if not bench_records:
        return {}
    out = {"by_kind": {}, "west": {}, "n": len(bench_records)}
    for kind in ("condo", "hdb"):
        sub = [r for r in bench_records if r.get("property_kind") == kind]
        if sub:
            out["by_kind"][kind] = {
                "rent": _stats([r["price"] for r in sub]),
                "psf": round(pd.Series([r["psf"] for r in sub]).median(), 2),
            }
    west = [r for r in bench_records if r.get("west_match")]
    if west:
        out["west"]["rent"] = _stats([r["price"] for r in west])
    return out


def summarize(df: pd.DataFrame) -> dict:
    """返回报告用的汇总统计。"""
    out = {
        "total": len(df),
        "by_source": df["source"].value_counts().to_dict(),
        "by_type": df["listing_type"].value_counts().to_dict(),
        "by_room_cat": df[df["listing_type"] == "room"]["room_category"]
                        .value_counts().to_dict(),
        "dist_median": round(df["distance_km"].median(), 2)
                        if df["distance_km"].notna().any() else None,
        "price_median_whole": _safe_median(
            df[df["listing_type"] == "whole_unit"]["total_rent"]),
        "price_median_room": _safe_median(
            df[df["listing_type"] == "room"]["price"]),
    }
    return out


def _safe_median(s) -> Optional[float]:
    s = s.dropna()
    return round(float(s.median())) if len(s) else None
