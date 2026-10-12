"""数据清洗对比：把旧 normalized 快照与重新抓取后的新 normalized 做差，
找出「已消失」的房源（多半是已被租出/下架），并统计新增与留存。

用法：
  python clean_diff.py                      # 比较 data/normalized_prev.json vs data/normalized.json
  python clean_diff.py old.json new.json
对比维度：按 source 分组，用 (source,id) 唯一键；id 缺失时回退用 url。
仅对「两次都抓取的 source」判定消失（避免把本来没跑的源误判为全部下架）。
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import config


def _key(r: dict):
    return (r.get("source"), str(r.get("id") or r.get("url")))


def _load(p: Path) -> list[dict]:
    return json.load(open(p, encoding="utf-8"))


def main():
    old_p = Path(sys.argv[1]) if len(sys.argv) > 1 else config.DATA_DIR / "normalized_prev.json"
    new_p = Path(sys.argv[2]) if len(sys.argv) > 2 else config.DATA_DIR / "normalized.json"
    old, new = _load(old_p), _load(new_p)

    old_by = {_key(r): r for r in old}
    new_by = {_key(r): r for r in new}
    old_srcs = {r.get("source") for r in old}
    new_srcs = {r.get("source") for r in new}
    common_srcs = old_srcs & new_srcs  # 只对两边都跑过的源判定下架

    gone, kept, added = [], [], []
    for k, r in old_by.items():
        if r.get("source") not in common_srcs:
            continue
        (kept if k in new_by else gone).append(r)
    for k, r in new_by.items():
        if k not in old_by and r.get("source") in common_srcs:
            added.append(r)

    print(f"旧快照: {old_p.name}  {len(old)} 条 | 新快照: {new_p.name}  {len(new)} 条")
    print(f"两次都抓取的源: {sorted(common_srcs)}")
    if old_srcs - new_srcs:
        print(f"⚠️ 本次未重抓的旧源(不判定下架): {sorted(old_srcs - new_srcs)}")
    if new_srcs - old_srcs:
        print(f"🆕 本次新增的源: {sorted(new_srcs - old_srcs)}")
    print()

    def by_src(rows):
        return collections.Counter(r.get("source") for r in rows)

    print(f"❌ 已消失(疑似已租出/下架): {len(gone)}")
    for s, n in sorted(by_src(gone).items()):
        print(f"     {s}: {n}")
    print(f"✅ 仍在线(留存): {len(kept)}")
    for s, n in sorted(by_src(kept).items()):
        print(f"     {s}: {n}")
    print(f"🆕 新上架(本次新增): {len(added)}")
    for s, n in sorted(by_src(added).items()):
        print(f"     {s}: {n}")

    # 抽样展示几条已消失的（便于人工核验）
    print("\n下架样本(最多 8 条):")
    for r in gone[:8]:
        print(f"  - [{r.get('source')}] {r.get('property_name') or r.get('title')} "
              f"| S${r.get('total_rent') or r.get('price')} "
              f"| {r.get('distance_km')}km | {r.get('url')}")

    # 落盘，供报告/复核
    out = {
        "gone": gone, "added": added,
        "summary": {
            "old": len(old), "new": len(new),
            "gone": len(gone), "kept": len(kept), "added": len(added),
            "gone_by_source": dict(by_src(gone)),
            "added_by_source": dict(by_src(added)),
        },
    }
    dst = config.DATA_DIR / "clean_diff.json"
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n详情已保存: {dst}")


if __name__ == "__main__":
    main()
