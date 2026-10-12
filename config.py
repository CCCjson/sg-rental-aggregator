"""全局配置：NTU 坐标、搜索半径、各站点、UA 池、价格分桶等。"""

from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
GEOCODE_CACHE = DATA_DIR / "geocode_cache.json"
PROGRESS_FILE = DATA_DIR / "progress.json"
LISTINGS_CSV = DATA_DIR / "listings.csv"
SUMMARY_XLSX = DATA_DIR / "rent_summary.xlsx"
REPORT_MD = DATA_DIR / "REPORT.md"

for _d in (DATA_DIR, RAW_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# NTU 位置与搜索范围
# ---------------------------------------------------------------------------
# 来自用户提供的 99.co URL: query_coords=1.3483099,103.6831347
NTU_LAT = 1.3483099
NTU_LNG = 103.6831347
NTU_NAME = "Nanyang Technological University, Singapore"

# 用户确认：3 km 半径内算"离 NTU 近"
RADIUS_KM = 3.0

# ---------------------------------------------------------------------------
# 单间拆分权重（主人房 : 普通房 = 1.3 : 1）
# 该权重在 3 房(1主+2普)时还原为 4:3:3（master 40%/common 30%/30%，用户已确认）；
# 在 2 房(1主+1普)时为 master 56.5%/common 43.5%，避免普通房反而比主人房贵。
# ---------------------------------------------------------------------------
MASTER_WEIGHT = 1.3
COMMON_WEIGHT = 1.0

# ---------------------------------------------------------------------------
# 合租预算（一群人整租，每人一间独立卧室）
# 每人意愿价：主人房 1800-2200，普通房 1300-1800
# ---------------------------------------------------------------------------
MASTER_BUDGET = (1800, 2200)
COMMON_BUDGET = (1300, 1800)
# 由每人预算推导的「整套总租金」预算区间：
#   2 人(2房=1主+1普): 1800+1300 ~ 2200+1800 = 3100 ~ 4000
#   3 人(3房=1主+2普): 1800+2*1300 ~ 2200+2*1800 = 4400 ~ 5800
WHOLE_BUDGET = {
    2: (MASTER_BUDGET[0] + COMMON_BUDGET[0], MASTER_BUDGET[1] + COMMON_BUDGET[1]),
    3: (MASTER_BUDGET[0] + 2 * COMMON_BUDGET[0], MASTER_BUDGET[1] + 2 * COMMON_BUDGET[1]),
}
BUDGET_TOLERANCE = 0.07      # 允许极值轻微浮动 ±7%

# ---------------------------------------------------------------------------
# 价格分桶边界 (SGD / 月)
# ---------------------------------------------------------------------------
PRICE_BINS = [0, 800, 1000, 1200, 1500, 2000, 3000, float("inf")]
PRICE_BIN_LABELS = [
    "<800", "800-1000", "1000-1200", "1200-1500",
    "1500-2000", "2000-3000", "3000+",
]

# ---------------------------------------------------------------------------
# 反爬 / 请求控制 (参照 SCRAPING_KNOWLEDGE_BASE 2.md)
# ---------------------------------------------------------------------------
TLS_IMPERSONATE = "chrome120"
DELAY_MIN = 0.8
DELAY_MAX = 2.5
MAX_RETRIES = 3
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# ---------------------------------------------------------------------------
# 站点配置
# ---------------------------------------------------------------------------
# OneMap (新加坡官方免费地理编码，无需 key)
ONEMAP_SEARCH = "https://www.onemap.gov.sg/api/common/elastic/search"

SITES = {
    "99co": {
        "base": "https://www.99.co",
        "search_url": (
            "https://www.99.co/singapore/rent"
            "?query_coords=1.3483099%2C103.6831347"
            "&query_type=google&main_category=condo&rental_type=all"
            "&radius_max={radius_m}&page_num={page}"
        ),
    },
    "propertyguru": {
        "base": "https://www.propertyguru.com.sg",
        # 专门的 room rental + near NTU 板块
        "room_rental": "https://www.propertyguru.com.sg/property-for-rent/p/near-ntu",
    },
    "roomies": {
        "base": "https://www.roomies.sg",
        "search": "https://www.roomies.sg/rooms/singapore-west",
    },
    "rentinsg": {
        "base": "https://rentinsingapore.com.sg",
        "search": "https://rentinsingapore.com.sg/",
    },
    "srx": {
        "base": "https://www.srx.com.sg",
        "search": "https://www.srx.com.sg/rent",
    },
    "edgeprop": {
        "base": "https://www.edgeprop.sg",
        # CF 托管挑战，需 nodriver；Jurong West 等 NTU 邻近板块
        "rent_slug": "https://www.edgeprop.sg/property-for-rent/{town}",
    },
    "carousell": {
        "base": "https://www.carousell.sg",
        "search": "https://www.carousell.sg/search/{query}/",
    },
    "ohmyhome": {
        "base": "https://ohmyhome.com",
        "search": "https://ohmyhome.com/en-sg/property-for-rent-singapore/",
    },
}
