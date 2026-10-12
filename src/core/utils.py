"""通用工具：日志、JSON 读写、断点续爬、去重。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import config


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def save_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_raw(site: str, name: str, obj: Any) -> Path:
    """保存某站点的原始数据，便于复查/离线重跑分析。"""
    path = config.RAW_DIR / f"{site}_{name}.json"
    save_json(obj, path)
    log(f"[{site}] 原始数据已保存: {path.name}")
    return path


def dedup_by_url(records: Iterable[dict]) -> list[dict]:
    """按 url 去重（无 url 的记录用 title+price 兜底）。"""
    seen: set = set()
    out: list[dict] = []
    for r in records:
        key = r.get("url") or f"{r.get('title')}|{r.get('price')}|{r.get('source')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# --------------------------------------------------------------------------
# 断点续爬
# --------------------------------------------------------------------------
def save_progress(state: dict) -> None:
    state["timestamp"] = time.time()
    save_json(state, config.PROGRESS_FILE)


def load_progress() -> dict:
    return load_json(config.PROGRESS_FILE, default={}) or {}
