"""HTTP 客户端：curl_cffi TLS 指纹伪装 + UA 轮换 + 随机延迟 + 指数退避。

复用 SCRAPING_KNOWLEDGE_BASE 2.md 第 3 节的经验。
"""

from __future__ import annotations

import random
import time
from typing import Optional

from curl_cffi import requests as curl_requests

import config
from src.core.utils import log


def get_random_ua() -> str:
    return random.choice(config.USER_AGENTS)


def smart_delay() -> None:
    """请求之间的随机延迟，降低被限流概率。"""
    time.sleep(random.uniform(config.DELAY_MIN, config.DELAY_MAX))


def _backoff(attempt: int, base: float = 1.0, max_delay: float = 10.0) -> float:
    delay = min(base * (2 ** attempt), max_delay)
    jitter = delay * random.uniform(-0.25, 0.25)
    return delay + jitter


def new_session() -> "curl_requests.Session":
    """创建一个带固定 TLS 指纹的 Session（模拟单一浏览器）。"""
    return curl_requests.Session(impersonate=config.TLS_IMPERSONATE)


def build_headers(referer: str = "", extra: Optional[dict] = None) -> dict:
    headers = {
        "accept": "application/json, text/javascript, text/html, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8",
        "user-agent": get_random_ua(),
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not(A:Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if referer:
        headers["referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def get(
    url: str,
    session: Optional["curl_requests.Session"] = None,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = 30,
    expect_json: bool = False,
):
    """带重试/退避的 GET。返回 response 对象（失败返回 None）。"""
    sess = session or new_session()
    hdrs = headers or build_headers()

    for attempt in range(config.MAX_RETRIES):
        try:
            resp = sess.get(url, headers=hdrs, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log(f"[client] 请求异常 {url}: {exc}")
            time.sleep(_backoff(attempt))
            continue

        if resp.status_code == 200:
            if expect_json:
                try:
                    resp.json()
                except Exception:  # noqa: BLE001
                    log(f"[client] 200 但非 JSON: {url}")
            return resp

        if resp.status_code == 403:
            log(f"[client] 403 Forbidden（可能被 WAF 拦截）: {url}")
            return resp  # 交由上层决定是否换 Playwright

        if resp.status_code in config.RETRYABLE_STATUS:
            wait = _backoff(attempt)
            log(f"[client] {resp.status_code} 重试 {attempt + 1}/{config.MAX_RETRIES}，等待 {wait:.1f}s")
            time.sleep(wait)
            continue

        log(f"[client] 非预期状态 {resp.status_code}: {url}")
        return resp

    return None
