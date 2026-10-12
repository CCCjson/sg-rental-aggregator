# NTU 周边租房信息爬虫 (rent_info)

爬取新加坡 **NTU（南洋理工大学）周边 3 公里**内的租房信息，产出汇总 Excel + Markdown 报告。
满足四项需求：①到 NTU 直线距离　②价格（区分主人房/普通房，整租按 4:3:3 拆分）　③价格区间表　④配套设施 + 性价比意见。

## 环境

专用 conda 环境 `rent_info`：

```bash
conda create -n rent_info python=3.11 -y
conda activate rent_info
pip install -r requirements.txt
playwright install chromium
```

## 运行

```bash
conda activate rent_info

python run.py                      # 全部站点，产出到 data/
python run.py --sites 99co,propertyguru
python run.py --pages 25           # 每站最大页数
python run.py --headless           # 99.co 用无头（默认 headful 以过 Cloudflare）
python run.py --from-raw           # 跳过抓取，用 data/raw 已存数据重跑分析
```

首次运行 99.co 会弹出浏览器窗口通过 Cloudflare Turnstile（通常自动通过；必要时手动点选）。
通过后 `cf_clearance` 存入 `data/browser_99co/`，后续运行自动复用。

## 数据源

| 站点 | 方式 | 说明 |
|---|---|---|
| **99.co** | Playwright 过 CF + 页面内 fetch 调内部 API `/api/v10/web/search/listings` | 主力，condo+hdb，含房间(beds=-1)，带经纬度 |
| **PropertyGuru** | curl_cffi 取 Next.js 页 → 解析 `__NEXT_DATA__` | "near-ntu" 板块，单间/合租最丰富，地址经 OneMap 地理编码 |
| **roomies.sg** | curl_cffi + BeautifulSoup | 合租/单间，best-effort |
| **rentinsingapore** | curl_cffi + BeautifulSoup | 房间，best-effort，按半径过滤 |
| **SRX** | curl_cffi + BeautifulSoup | 价格基准，best-effort（结果多为 JS 渲染，产出有限） |

地理编码用**新加坡官方 OneMap API**（免费、无需 key），结果缓存于 `data/geocode_cache.json`。

## 产出文件（data/）

- `rent_summary.xlsx` — 5 个 sheet：明细 / 价格区间 / 4-3-3拆分 / 配套设施 / 性价比榜
- `REPORT.md` — 中文分析报告（摘要、方法、价格区间、性价比推荐、注意事项）
- `listings.csv` — 统一明细
- `raw/` — 各站原始数据（便于 `--from-raw` 离线复跑）

## 方法要点

- **直线距离**：haversine（`src/core/geo.py`），参照点为 NTU `(1.3483099, 103.6831347)`。
- **4:3:3 拆分**：整租总租金按 主人房 40% : 普通房合计 60%（平摊到每间普通房）估算单间价。
- **性价比评分**：同类（房型×卧室数）内对价格(权重0.6)与距离(0.4)做 z-score，映射 0–100。

## 技术沉淀

复用 `../SCRAPING_KNOWLEDGE_BASE 2.md`：curl_cffi TLS 指纹伪装、UA 轮换、随机延迟、
指数退避、Playwright 持久化上下文过 Cloudflare、cookie 复用。

> 本数据仅供个人租房研究，采集已温和限速；请勿用于商业再分发。
