"""Co-living / 学生公寓采集器（近 NTU，"两人各租一间独立房"为目标）。

目标场景：2 人小队希望**各睡一间独立私人房**（两间单人房 / 两间 studio /
两间 ensuite suite），而**不是** twin-share（两人挤一间）。因此对每个房型打上
`each_own_room` 标记：单人占用的私人房/studio = True；twin/shared = False。

数据源（已探明，curl_cffi(chrome120) 直连即可，无 Cloudflare）：
  1) uhomes（主力聚合站，最全）：
     - 列表页 `…/nanyang-technological-university-ntu` 的 HTML 里用正则
       `detail-apartments-(\\d+)` 抽出 ~28 个房源 ID（动态，会变）。
     - 每个详情页 `…/detail-apartments-{id}` 是 Nuxt SSR 静态 HTML（~96KB），
       关键数据全部内嵌，无需 JS：
         * ld+json `@type=Product` → name / category / offers(lowPrice,
           highPrice, offerCount)  —— 价格区间权威来源
         * ld+json `@type=LocalBusiness` → geo(lat,lng) / 真实街道地址 / 邮编
         * `<div class="seo-room-type-hidden">` → 房型分类树：
             <h3>=分类(Single Room / Shared Room / Studio / En-suite / 3B…)
             <p> =分类说明（过滤掉）
             <h4>=该分类下的具体房型名（Common Room / Master Room / Twin / …）
         * 正文文本里的 "X km/kilometers away from NTU" → 距离佐证
     - 顶部展示价 "SG$1,730 /month" 仅部分页面有；价格统一以 ld+json 区间为准。

  2) Coliwoo（运营方直营，次要）：每间房价格走 JS 动态加载，静态 HTML 拿不到
     单房价格；uhomes 已收录 Coliwoo Boon Lay（含价格），故此处对 Coliwoo 仅做
     "优雅跳过"，不强行抓。

房型 → each_own_room 判定（基于 uhomes 的分类树语义）：
  Single Room / Studio / En-suite / Suite / Master / Common / Queen …  → 单人独住 = True
  Shared Room / Twin / Sharing / Bunk                                  → 两人合住一间 = False
  3B / 2B / Whole / Entire（整套）                                      → 标记为 whole_unit（非按间出租）
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Optional

import config
from src.core import client, geo
from src.core.utils import log, save_raw

BASE = "https://en.uhomes.com"
INDEX_URL = f"{BASE}/sg/singapore/nanyang-technological-university-ntu"
DETAIL_URL = f"{BASE}/sg/singapore/detail-apartments-{{id}}"

# co-living near NTU 多在 Boon Lay/Pioneer/Jurong West/Yunnan（~4km），
# 故此采集器用 5km 截断（比项目默认 3km 宽），好选项常落在 3km 外一点点。
RADIUS_KM = 5.0

# 列表页动态抽不到时的兜底种子（probe 已验证可用）
SEED_IDS = [
    "1444591", "1505097", "1508523", "1515284", "1519219", "1524807",
    "1564836", "1577924", "1578000", "1601282", "1619240", "1619299",
    "1624372", "1626378", "1672215", "1672216", "1675687", "1677399",
    "1677516", "1678569", "1678914", "1679079", "1710316", "1710922",
    "1712316", "1727015", "1747161", "1751326",
    # 已知优质（Coliwoo Boon Lay / Westwood Hostel / BLK979 等）
    "1498596", "1652567", "1498331", "1670540",
]

_DELAY = 0.8  # 礼貌限速 ~0.6-1s/请求

# --------------------------------------------------------------------------
# 房型语义判定
# --------------------------------------------------------------------------
# 明确 twin / 合住一间（两人一间）→ each_own_room=False
_TWIN_RE = re.compile(r"\b(twin|shared\s*room|sharing|share\s*room|bunk|double\s*share)\b", re.I)
# 明确整套（按整间公寓出租，非按房间）→ whole_unit
_WHOLE_RE = re.compile(r"\b(\d\s*b(?:ed(?:room)?)?\b|whole\s*(?:unit|apartment|house)|entire\s*(?:unit|apartment))", re.I)
# 单人独住私人房 / studio 的正向信号
_PRIVATE_RE = re.compile(
    r"\b(single|studio|en\W?suite|ensuite|suite|master|common\s*room|queen|premium|deluxe|standard|small|large|modified|private\s*room)\b",
    re.I,
)
# ensuite / 独立卫浴信号
_ENSUITE_RE = re.compile(r"\b(en\W?suite|ensuite|studio|master|private\s*bath|independent\s*bathroom|own\s*bathroom)\b", re.I)

# 分类标题里属于"整套/多卧"的（不是单间）
_CATEGORY_WHOLE = re.compile(r"^\s*\d\s*b(?:ed(?:room)?)?\b|whole|entire|apartment$", re.I)


def _classify_room(category: str, room_name: str) -> dict:
    """返回 {each_own_room, ensuite, listing_kind}。

    category = uhomes 分类树的 <h3>（如 Single Room / Shared Room / Studio / 3B）
    room_name = 该分类下的 <h4>（如 Master Room / Twin / Queen Studio）
    """
    blob = f"{category} {room_name}".strip()
    low = blob.lower()

    # 整套（按整间公寓出租，两人可在内各占一间，但非"按间报价"）
    if _WHOLE_RE.search(blob) or _CATEGORY_WHOLE.search(category or ""):
        return {"each_own_room": True, "ensuite": None, "listing_kind": "whole_unit"}

    # twin / 合住一间 → 两人挤一间，非目标
    if _TWIN_RE.search(blob):
        return {"each_own_room": False, "ensuite": False, "listing_kind": "twin_share"}

    # 其余按单人独住私人房处理（Single/Studio/Suite/Master/Common/Queen…）
    if _PRIVATE_RE.search(blob):
        ensuite = bool(_ENSUITE_RE.search(blob))
        return {"each_own_room": True, "ensuite": ensuite, "listing_kind": "private_room"}

    # 兜底：分类名含 "room" 视为单人私人房，否则未知按私人房保守处理
    ensuite = bool(_ENSUITE_RE.search(blob))
    return {"each_own_room": True, "ensuite": ensuite, "listing_kind": "private_room"}


# --------------------------------------------------------------------------
# 详情页解析
# --------------------------------------------------------------------------
_LDJSON_RE = re.compile(
    r'type="application/ld\+json"[^>]*>(\{.*?\})</script>', re.DOTALL
)
_SEO_BLOCK_RE = re.compile(
    r'<div[^>]*class="seo-room-type-hidden"[^>]*>(.*?)</div>\s*(?:<!---->|<section)',
    re.DOTALL,
)
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL)
_H4_RE = re.compile(r"<h4[^>]*>(.*?)</h4>", re.DOTALL)
_DIST_RES = [
    re.compile(r"([\d.]+)\s*kilometers?\s*away\s*from\s*NTU", re.I),
    re.compile(r"([\d.]+)\s*km\s*(?:away\s*)?from\s*NTU", re.I),
    re.compile(r"from\s*NTU[^.]{0,30}?([\d.]+)\s*(?:km|kilometers?)", re.I),
]


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _parse_ldjson(text: str) -> dict:
    """收集 Product(offers) + LocalBusiness(geo/address) 关键字段。"""
    out: dict = {}
    for raw in _LDJSON_RE.findall(text):
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = j.get("@type")
        if t == "Product":
            out["name"] = _clean(html.unescape(j.get("name") or ""))
            out["category"] = j.get("category")
            offers = j.get("offers") or {}
            out["low_price"] = offers.get("lowPrice")
            out["high_price"] = offers.get("highPrice")
            out["offer_count"] = offers.get("offerCount")
            out["currency"] = offers.get("priceCurrency")
        elif t == "LocalBusiness":
            geo_o = j.get("geo") or {}
            lat, lng = geo_o.get("latitude"), geo_o.get("longitude")
            if lat is not None and lng is not None:
                out["lat"], out["lng"] = float(lat), float(lng)
            addr = j.get("address") or {}
            parts = [addr.get("streetAddress"), addr.get("addressLocality"),
                     addr.get("postalCode")]
            out["address"] = ", ".join(p for p in parts if p)
            out["postal"] = addr.get("postalCode")
            if not out.get("name"):
                out["name"] = _clean(j.get("name") or "")
    return out


def _parse_room_types(text: str) -> list[dict]:
    """从 seo-room-type-hidden 区块解析 [分类<h3> → 房型<h4>] 树。"""
    m = _SEO_BLOCK_RE.search(text)
    if not m:
        return []
    block = m.group(1)
    # 用 <h3> 切分为各分类小节
    rooms: list[dict] = []
    # 在每个 <h3> 前插入分隔符，便于按分类切
    cats = list(_H3_RE.finditer(block))
    if not cats:
        return []
    for i, cm in enumerate(cats):
        category = _clean(cm.group(1))
        seg_start = cm.end()
        seg_end = cats[i + 1].start() if i + 1 < len(cats) else len(block)
        seg = block[seg_start:seg_end]
        names = [_clean(h) for h in _H4_RE.findall(seg)]
        names = [n for n in names if n]
        if not names:
            # 分类本身即房型（无叶子），用分类名兜底
            names = [category]
        for nm in names:
            info = _classify_room(category, nm)
            rooms.append({
                "category": category,
                "type": nm,
                **info,
            })
    return rooms


def _extract_distance_text(text: str) -> Optional[float]:
    flat = _clean(text)
    for rx in _DIST_RES:
        mm = rx.search(flat)
        if mm:
            try:
                return round(float(mm.group(1)), 3)
            except ValueError:
                pass
    return None


def _parse_detail(pid: str, text: str) -> Optional[dict]:
    ld = _parse_ldjson(text)
    name = ld.get("name") or f"uhomes property {pid}"
    rooms = _parse_room_types(text)

    # 距离：优先用 geo 坐标算 haversine，其次用页面"X km from NTU"文本，
    # 再次用地址 OneMap 地理编码。
    lat, lng = ld.get("lat"), ld.get("lng")
    dist = geo.distance_to_ntu(lat, lng)
    dist_text = _extract_distance_text(text)
    if dist is None and ld.get("address"):
        glat, glng, gdist = geo.geocode_distance(ld["address"])
        if gdist is not None:
            lat, lng, dist = glat, glng, gdist
    if dist is None:
        dist = dist_text  # 文本距离兜底（"about 4km away from NTU"）

    # 价格：ld+json offers 区间。each_own_room 房型取 low_price 作为"每间月租"近似
    # （uhomes offers 覆盖该房源全部房型，lowPrice 即最便宜单间/studio 起价）。
    low = ld.get("low_price")
    high = ld.get("high_price")

    # 为每个房型构造 room_type 记录
    room_types: list[dict] = []
    for r in rooms:
        rt = {
            "type": r["type"],
            "category": r["category"],
            "price": low if low else None,   # 起价（按间）近似；区间见 price_range
            "price_range": [low, high] if low else None,
            "is_private_room": r["each_own_room"] and r["listing_kind"] != "twin_share",
            "each_own_room": r["each_own_room"],
            "ensuite": r["ensuite"],
            "listing_kind": r["listing_kind"],
            "size_sqft": None,        # uhomes 静态 HTML 未稳定提供单房面积
            "min_lease_months": None,  # 同上，详情正文偶有，未结构化
        }
        room_types.append(rt)

    return {
        "source": "uhomes",
        "id": f"uhomes-{pid}",
        "url": DETAIL_URL.format(id=pid),
        "name": name,
        "operator": _guess_operator(name),
        "category": ld.get("category"),
        "address": ld.get("address"),
        "postal": ld.get("postal"),
        "lat": lat,
        "lng": lng,
        "distance_km": dist,
        "distance_km_text": dist_text,
        "price_low": low,
        "price_high": high,
        "offer_count": ld.get("offer_count"),
        "currency": ld.get("currency") or "SGD",
        "room_types": room_types,
    }


_OPERATOR_HINTS = {
    "coliwoo": "Coliwoo", "westwood": "Westwood Hostel", "lyf": "lyf",
    "hmlet": "Hmlet", "cove": "Cove", "casa": "Casa", "uhostel": "uHostel",
}


def _guess_operator(name: str) -> Optional[str]:
    low = (name or "").lower()
    for k, v in _OPERATOR_HINTS.items():
        if k in low:
            return v
    return None


# --------------------------------------------------------------------------
# 列表页：抽取房源 ID
# --------------------------------------------------------------------------
def _fetch_ids() -> list[str]:
    resp = client.get(INDEX_URL, headers=client.build_headers(
        extra={"accept": "text/html,application/xhtml+xml,*/*",
               "sec-fetch-mode": "navigate", "sec-fetch-site": "none"}))
    ids: list[str] = []
    if resp is not None and resp.status_code == 200:
        ids = re.findall(r"detail-apartments-(\d+)", resp.text)
        log(f"[coliving] 列表页抽出 {len(set(ids))} 个房源 ID")
    else:
        log(f"[coliving] 列表页状态 {getattr(resp, 'status_code', None)}，改用种子 ID")
    # 合并种子，去重保持顺序
    merged: list[str] = []
    seen: set[str] = set()
    for i in list(ids) + SEED_IDS:
        if i not in seen:
            seen.add(i)
            merged.append(i)
    return merged


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def scrape() -> list[dict]:
    ids = _fetch_ids()
    log(f"[coliving] 共 {len(ids)} 个房源待抓取")
    out: list[dict] = []
    raw_html_meta: list[dict] = []

    for n, pid in enumerate(ids, 1):
        url = DETAIL_URL.format(id=pid)
        resp = client.get(url, headers=client.build_headers(
            extra={"accept": "text/html,application/xhtml+xml,*/*",
                   "sec-fetch-mode": "navigate", "sec-fetch-site": "none"}))
        if resp is None or resp.status_code != 200:
            log(f"[coliving] {pid} 状态 {getattr(resp, 'status_code', None)}，跳过")
            time.sleep(_DELAY)
            continue
        try:
            rec = _parse_detail(pid, resp.text)
        except Exception as exc:  # noqa: BLE001
            log(f"[coliving] {pid} 解析异常: {exc}")
            time.sleep(_DELAY)
            continue
        if rec:
            out.append(rec)
            raw_html_meta.append({
                "id": pid, "name": rec["name"], "distance_km": rec["distance_km"],
                "price_low": rec["price_low"], "n_rooms": len(rec["room_types"]),
            })
            log(f"[coliving] [{n}/{len(ids)}] {rec['name'][:42]} | "
                f"{rec['distance_km']}km | ${rec['price_low']} | {len(rec['room_types'])} 房型")
        time.sleep(_DELAY)

    save_raw("uhomes", "coliving_meta", raw_html_meta)
    log(f"[coliving] 完成：{len(out)} 个房源")
    return out


# --------------------------------------------------------------------------
# 交付物：data/coliving_ntu.json + data/COLIVING_NTU.md
# --------------------------------------------------------------------------
JSON_OUT = config.DATA_DIR / "coliving_ntu.json"
MD_OUT = config.DATA_DIR / "COLIVING_NTU.md"

# 标准化房型类目标签（用于聚合，避免同一栋楼几十个 Common Room 重复列出）
def _norm_type_label(category: str, type_name: str) -> str:
    low = f"{category} {type_name}".lower()
    if re.search(r"en\W?suite|ensuite", low):
        return "Ensuite Suite"
    if "studio" in low:
        return "Studio"
    if "master" in low:
        return "Master Room"
    if "common" in low or "single room" in low:
        return "Common / Single Room"
    if "suite" in low:
        return "Suite"
    if "twin" in low or "shared" in low or "sharing" in low:
        return "Twin / Shared (2 share 1)"
    if re.search(r"\d\s*b", low):
        return "Whole Unit (multi-bedroom)"
    if "queen" in low:
        return "Studio"  # uhomes 的 Queen* 多为 studio 内单间
    return type_name.strip() or category.strip() or "Room"


def _aggregate_rooms(prop: dict) -> list[dict]:
    """把一栋楼里同类房型聚合成一行（取最低价、是否 ensuite、each_own_room）。"""
    buckets: dict[str, dict] = {}
    for rt in prop["room_types"]:
        if rt["listing_kind"] == "whole_unit":
            continue  # 整套单列，不进"按间"表
        label = _norm_type_label(rt["category"], rt["type"])
        b = buckets.setdefault(label, {
            "label": label, "each_own_room": rt["each_own_room"],
            "ensuite": rt["ensuite"], "price_from": rt.get("price"),
            "n": 0, "listing_kind": rt["listing_kind"],
        })
        b["n"] += 1
        if rt["ensuite"]:
            b["ensuite"] = True
        if rt.get("price") and (b["price_from"] is None or rt["price"] < b["price_from"]):
            b["price_from"] = rt["price"]
    return list(buckets.values())


def _build_json(props: list[dict]) -> list[dict]:
    """结构化交付：每栋楼 + 聚合房型，仅保留 ≤5km。"""
    out = []
    for p in props:
        d = p["distance_km"]
        if d is None or d > RADIUS_KM:
            continue
        agg = _aggregate_rooms(p)
        out.append({
            "name": p["name"],
            "operator": p["operator"],
            "source": p["source"],
            "url": p["url"],
            "address": p["address"],
            "area": _area_of(p["address"]),
            "lat": p["lat"], "lng": p["lng"],
            "distance_km": d,
            "price_low": p["price_low"], "price_high": p["price_high"],
            "currency": p["currency"],
            "offer_count": p["offer_count"],
            "category": p["category"],
            "room_types_aggregated": agg,
            "room_types_raw": p["room_types"],
        })
    out.sort(key=lambda x: (x["distance_km"], x["price_low"] or 99999))
    return out


def _area_of(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    low = address.lower()
    for area in ("yunnan", "boon lay", "pioneer", "jurong west", "jurong east",
                 "lakeside", "chinese garden", "clementi", "bukit batok", "jurong"):
        if area in low:
            return area.title()
    return address.split(",")[0].strip()


def _md_link(name: str, url: str) -> str:
    return f"[{name}]({url})"


def _write_md(records: list[dict], today: str) -> None:
    own_rows: list[tuple] = []   # (dist, price, prop, room)
    twin_rows: list[tuple] = []
    for p in records:
        for rt in p["room_types_aggregated"]:
            row = (p["distance_km"], rt["price_from"] or 99999, p, rt)
            if rt["listing_kind"] == "twin_share" or not rt["each_own_room"]:
                twin_rows.append(row)
            else:
                own_rows.append(row)
    own_rows.sort(key=lambda r: (r[0], r[1]))
    twin_rows.sort(key=lambda r: (r[0], r[1]))

    lines = []
    lines.append("# 近 NTU 联合公寓 / 学生公寓：两人各租一间独立房（每人一间）")
    lines.append("")
    lines.append(f"> 面向「2 人小队、各睡一间独立私人房」的场景（两间单人房 / 两间 studio / "
                 f"两间 ensuite，**而非** twin-share 两人挤一间）。数据抓取于 {today}，"
                 f"联合公寓库存为动态，价格/房态随时变动，仅供个人研究参考。")
    lines.append("")
    lines.append(f"> 数据源：uhomes.com（NTU 学生公寓聚合）。距离按房源真实坐标到 NTU 的直线距离计算，"
                 f"截断半径 {RADIUS_KM:.0f}km（好选项常落在 3km 外一点的 Boon Lay/Yunnan/Jurong West）。"
                 f"**月租/间为该栋楼该类房型的「起价」**（uhomes 静态页仅提供楼盘价格区间，"
                 f"未公开每间精确价；以最低档近似）。")
    lines.append("")
    lines.append("## 每人一间（each own room）选项")
    lines.append("")
    lines.append("| 房源 / Operator | 距 NTU | 房型 | 月租/间(起) | 独立卫浴 | 最短租期 | 链接 |")
    lines.append("|---|---|---|---|---|---|---|")
    for dist, _, p, rt in own_rows:
        op = f" ({p['operator']})" if p["operator"] else ""
        name = p["name"].replace("|", "/")
        price = f"S${rt['price_from']:,}起" if rt["price_from"] and rt["price_from"] < 99999 else "—"
        ens = "✅" if rt["ensuite"] else ("—" if rt["ensuite"] is False else "?")
        lease = "—"  # uhomes 静态页未结构化最短租期
        lines.append(f"| {name}{op} | {dist:.2f}km | {rt['label']} | {price} | {ens} | "
                     f"{lease} | {_md_link('详情', p['url'])} |")
    lines.append("")
    lines.append(f"_共 {len(own_rows)} 个「每人一间」房型行（按 距离↑、价格↑ 排序）。_")
    lines.append("")

    if twin_rows:
        lines.append("## Twin-share（两人合住一间，不推荐用于本场景）")
        lines.append("")
        lines.append("| 房源 / Operator | 距 NTU | 房型 | 月租/间(起) | 独立卫浴 | 链接 |")
        lines.append("|---|---|---|---|---|---|")
        for dist, _, p, rt in twin_rows:
            op = f" ({p['operator']})" if p["operator"] else ""
            name = p["name"].replace("|", "/")
            price = f"S${rt['price_from']:,}起" if rt["price_from"] and rt["price_from"] < 99999 else "—"
            ens = "✅" if rt["ensuite"] else "—"
            lines.append(f"| {name}{op} | {dist:.2f}km | {rt['label']} | {price} | {ens} | "
                         f"{_md_link('详情', p['url'])} |")
        lines.append("")

    MD_OUT.write_text("\n".join(lines), encoding="utf-8")
    log(f"[coliving] 已写出 {MD_OUT.name}")


def build_deliverables(props: list[dict], today: Optional[str] = None) -> list[dict]:
    import datetime as _dt
    today = today or _dt.date.today().isoformat()
    records = _build_json(props)
    from src.core.utils import save_json
    save_json(records, JSON_OUT)
    log(f"[coliving] 已写出 {JSON_OUT.name}（{len(records)} 栋 ≤{RADIUS_KM:.0f}km）")
    _write_md(records, today)
    return records


if __name__ == "__main__":
    props = scrape()

    # 统计
    all_rooms = [(p, rt) for p in props for rt in p["room_types"]]
    own_rooms = [(p, rt) for p, rt in all_rooms
                 if rt["each_own_room"] and rt["listing_kind"] == "private_room"]

    def _within(km):
        return [(p, rt) for p, rt in own_rooms
                if p["distance_km"] is not None and p["distance_km"] <= km]

    own_3 = _within(3.0)
    own_5 = _within(5.0)

    print(f"\n{'='*64}")
    print(f"PROPERTIES: {len(props)}")
    print(f"TOTAL ROOM TYPES: {len(all_rooms)}")
    print(f"each_own_room (private, single-occupancy) room types: {len(own_rooms)}")
    print(f"  within 3km of NTU: {len(own_3)}")
    print(f"  within 5km of NTU: {len(own_5)}")
    twin = [rt for _, rt in all_rooms if rt["listing_kind"] == "twin_share"]
    whole = [rt for _, rt in all_rooms if rt["listing_kind"] == "whole_unit"]
    print(f"twin-share room types: {len(twin)} | whole-unit listings: {len(whole)}")

    # 最便宜的 3 个 each-own-room（含距离）
    priced = [(p, rt) for p, rt in own_5 if rt.get("price")]
    priced.sort(key=lambda x: (x[1]["price"], x[0]["distance_km"] or 99))
    print("\nCheapest each-own-room options (within 5km):")
    for p, rt in priced[:5]:
        print(f"  ${rt['price']:>5} | {p['distance_km']}km | {rt['type'][:28]:<28} | {p['name'][:36]}")

    print("\nSample properties:")
    for p in props[:3]:
        print(f"  - {p['name']} | {p['distance_km']}km | "
              f"${p['price_low']}-{p['price_high']} | {len(p['room_types'])} room types")

    # 交付物
    recs = build_deliverables(props)
    print(f"\nDeliverables written: {JSON_OUT}  ({len(recs)} props ≤{RADIUS_KM:.0f}km)")
    print(f"                      {MD_OUT}")
