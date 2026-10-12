"""把 data/REPORT.md 加上大量 Apple 风格 emoji，转成好看的 PDF。

流程：注入 emoji → markdown→HTML（表格扩展）→ Apple 系统字体 + 美化 CSS →
Playwright 无头 Chromium 渲染（macOS 上输出彩色 Apple Color Emoji）。
输出：data/REPORT.pdf
"""

from __future__ import annotations

import re

import markdown

import config

SRC = config.REPORT_MD
OUT = config.DATA_DIR / "REPORT.pdf"

# ── 标题 emoji（按关键词）──────────────────────────────────────────────
HEADING_EMOJI = [
    ("综合排名", "🏆📊"),
    ("评分口径", "🧮📐"),
    ("NTU 学生公寓", "🎓🏠✨"),
    ("Co-living 运营方", "🏢🛎️"),
    ("ensuite 单间", "🛏️🚿"),
    ("每人一间", "🔑🛏️"),
    ("周边合租房源推荐", "🏠🇸🇬✨"),
    ("需求与预算", "💰📋"),
    ("推荐结论", "🎯⭐️"),
    ("3 人合租推荐", "🏆👨‍👩‍👦🛏️"),
    ("2 人合租推荐", "🏡👫🛏️"),
    ("市场价格基准", "📊💹"),
    ("NTU 片区整租挂牌价", "🏢💵"),
    ("NTU 片区单间挂牌价", "🛏️💵"),
    ("SRX 全岛挂牌价", "🌏🏙️"),
    ("价格区间参考", "💵📈"),
    ("整租单位", "📦"),
    ("注意事项", "⚠️📌"),
]

# ── 列表项 emoji（按关键词，命中第一个）──────────────────────────────
BULLET_EMOJI = [
    ("3 人合租是甜区", "🥇"),
    ("2 人合租", "🥈"),
    ("每人意愿价", "🙋"),
    ("整套总租金", "💸"),
    ("单间分摊", "➗"),
    ("各列前 20", "🔢"),
    ("数据来源", "🗂️"),
    ("拆分为估算", "🧮"),
    ("直线距离", "📏"),
    ("品质分", "⭐️"),
    ("去重", "🧹"),
    ("数据仅供", "🔒"),
]

# ── 配套设施 token → emoji 前缀 ───────────────────────────────────────
AMENITY_EMOJI = {
    "pool": "🏊", "gym": "🏋️", "furnished": "🛋️", "aircon": "❄️",
    "ensuite_bath": "🚿", "cooking": "🍳",
}


def add_emoji(md: str) -> str:
    out_lines = []
    for line in md.splitlines():
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            hashes, text = m.group(1), m.group(2)
            for kw, em in HEADING_EMOJI:
                if kw in text:
                    text = f"{em} {text}"
                    break
            out_lines.append(f"{hashes} {text}")
            continue
        # 列表项
        bm = re.match(r"^(\s*)-\s+(.*)$", line)
        if bm:
            indent, text = bm.group(1), bm.group(2)
            em = "🔹"
            for kw, e in BULLET_EMOJI:
                if kw in text:
                    em = e
                    break
            out_lines.append(f"{indent}- {em} {text}")
            continue
        out_lines.append(line)
    md = "\n".join(out_lines)

    # 类型列
    md = md.replace("| condo |", "| 🏢 condo |")
    md = md.replace("| hdb |", "| 🏘️ hdb |")
    md = md.replace("| 其他 |", "| 🏠 其他 |")
    # 预算标记
    md = md.replace("✓预算内", "✅ 预算内")
    md = md.replace("⚠超", "⚠️ 超").replace("⚠低", "⚠️ 低")
    # 通用对勾/叉
    md = md.replace("✓", "✅").replace("✗", "❌")
    # 配套 emoji（词边界）
    for tok, em in AMENITY_EMOJI.items():
        md = re.sub(rf"\b{tok}\b", f"{em}{tok}", md)
    # 裸 URL → 短链接（表格里那一长串），避免表格过宽
    md = re.sub(r"(?<=\| )(https?://\S+?)(?= \|)",
                lambda x: f"[🔗 链接]({x.group(1)})", md)
    # 引用块开头加个图钉
    md = re.sub(r"^>\s+(?!⚠)", "> 📎 ", md, flags=re.M)
    return md


CSS = """
/* ── 暖色杂志风：米白纸底 · 墨黑正文 · 赤陶点缀 · 衬线标题 ── */
@page { size: A4; margin: 16mm 15mm; }
* { box-sizing: border-box; }
:root {
  --paper:#faf6ef; --ink:#2f2a25; --muted:#9b9183; --terra:#b25a3c;
  --terra-soft:#c98a6e; --rule:#e6dccc; --head:#efe6d4; --zebra:#f4eee2;
}
body {
  font-family: -apple-system, "PingFang SC", "Helvetica Neue", "Apple Color Emoji", sans-serif;
  color: var(--ink); line-height: 1.7; font-size: 12px;
  -webkit-font-smoothing: antialiased; margin: 0; background: var(--paper);
}
.serif {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia,
               "Songti SC", "Noto Serif CJK SC", serif;
}
h1 {
  font-family: "Iowan Old Style", Palatino, Georgia, "Songti SC", serif;
  font-size: 27px; font-weight: 600; letter-spacing: .3px; line-height: 1.3;
  color: var(--ink); margin: 0 0 4px; padding: 0 0 14px;
  border-bottom: 2px solid var(--terra);
}
h2 {
  font-family: "Iowan Old Style", Palatino, Georgia, "Songti SC", serif;
  font-size: 18px; font-weight: 600; color: var(--ink);
  margin: 30px 0 12px; padding: 0 0 7px; border-bottom: 1px solid var(--rule);
}
h3 {
  font-family: "Iowan Old Style", Palatino, Georgia, "Songti SC", serif;
  font-size: 14px; font-weight: 600; font-style: italic; color: var(--terra);
  margin: 20px 0 8px; letter-spacing: .2px;
}
p { margin: 8px 0; }
blockquote {
  margin: 12px 0; padding: 8px 16px; background: #f3ead9;
  border-left: 3px solid var(--terra-soft); color: #6b5e4c;
  font-size: 11px; font-style: italic; border-radius: 0 4px 4px 0;
}
ul { padding-left: 2px; list-style: none; }
li { margin: 6px 0; padding-left: 2px; }
strong { color: var(--terra); font-weight: 700; }
code {
  background: #efe7d8; padding: 1px 6px; border-radius: 4px;
  font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 10.5px; color: #7a4a33;
}
a { color: var(--terra); text-decoration: none; font-weight: 600;
    border-bottom: 1px solid var(--terra-soft); }
/* ── 表格：印刷品式细横线，无竖线无阴影无圆角 ── */
table {
  border-collapse: collapse; width: 100%; margin: 12px 0 18px;
  font-size: 10px; table-layout: auto;
  border-top: 2px solid var(--ink); border-bottom: 2px solid var(--ink);
}
thead th {
  background: var(--head); color: var(--ink); font-weight: 700;
  padding: 7px 8px; text-align: left; white-space: nowrap;
  border-bottom: 1px solid var(--ink); letter-spacing: .2px;
}
tbody td {
  padding: 6px 8px; border-bottom: 1px solid var(--rule);
  vertical-align: middle; white-space: nowrap;
}
/* 仅“配套 / 房源”列（倒数第 2、3 列）允许换行，数字列保持整体不拆 */
tbody td:nth-last-child(2), tbody td:nth-last-child(3) {
  white-space: normal; word-break: break-word;
}
tbody tr:nth-child(even) { background: var(--zebra); }
tbody tr:first-child td { font-weight: 600; }
hr { border: none; border-top: 1px solid var(--rule); margin: 24px 0; }
"""


def main(src=None, out=None):
    import sys
    # 可传 CLI 参数：python md_to_pdf.py <源.md> [输出.pdf]
    if src is None and len(sys.argv) > 1:
        src = config.DATA_DIR / sys.argv[1] if "/" not in sys.argv[1] else None
        src = src or __import__("pathlib").Path(sys.argv[1])
    if out is None and len(sys.argv) > 2:
        out = config.DATA_DIR / sys.argv[2] if "/" not in sys.argv[2] else None
        out = out or __import__("pathlib").Path(sys.argv[2])
    src = src or SRC
    out = out or OUT
    from pathlib import Path
    src, out = Path(src), Path(out)

    md_text = src.read_text(encoding="utf-8")
    md_text = add_emoji(md_text)
    body = markdown.markdown(
        md_text, extensions=["tables", "fenced_code", "sane_lists"])
    html = f"<!doctype html><html lang='zh'><head><meta charset='utf-8'>" \
           f"<style>{CSS}</style></head><body>{body}</body></html>"

    tmp_html = out.with_suffix(".html")
    tmp_html.write_text(html, encoding="utf-8")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(tmp_html.as_uri(), wait_until="networkidle")
        page.pdf(path=str(out), format="A4", print_background=True,
                 margin={"top": "14mm", "bottom": "14mm",
                         "left": "12mm", "right": "12mm"})
        browser.close()
    print(f"✅ PDF 已生成: {out}")
    print(f"   中间 HTML: {tmp_html}")


if __name__ == "__main__":
    main()
