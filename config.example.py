"""SafeRoute 風險引擎 - 集中設定檔範本（SPEC.md §10 第 6 點）。
所有可調參數（半衰期、α、bandwidth、分類閾值等）都放這裡，不散落在程式碼中。

這是範本檔：風險引擎的實際校準數值（衰減半衰期、風險權重係數、佐證門檻）
已改為中性佔位值，標註「佔位值」的參數**不是**實際使用的數值——真正的
config.py 依調校結果設定，不納入版本控制。使用方式：複製一份成 config.py，
依自己的資料與需求重新校準這些佔位值。
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 資料庫連線
# ---------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "saferoute")
DB_USER = os.getenv("DB_USER", "test")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# ---------------------------------------------------------------------------
# 座標系統
# ---------------------------------------------------------------------------
CRS_WGS84 = "EPSG:4326"   # 儲存用
CRS_TWD97 = "EPSG:3826"   # 距離計算用（台灣二度分帶）

# ---------------------------------------------------------------------------
# M1：OSM 路網匯入（SPEC.md §2）
# ---------------------------------------------------------------------------
OSM_PLACE_NAME = "文山區, 台北市, 台灣"
OSM_NETWORK_TYPE = "walk"

# road_type 判斷規則（優先序見 src/ingest/osm_network.py）：
# 1. name 含關鍵字 -> alley
# 2. highway=service -> service_access（service=driveway/parking_aisle）
#                        或 service_alley（service=alley 或無子標籤／其他子標籤）
# 3. highway=unclassified -> residential
# 4. highway=residential/living_street -> residential
# 5. highway=steps -> steps
# 6. highway=path -> path
# 7. highway=footway/pedestrian -> footway
# 8. highway=primary/secondary/tertiary -> 同名
# 9. 其他 -> residential
# 10. 輔助：已歸 residential 但 width < 閾值 -> 改判 alley
ROAD_TYPE_ALLEY_NAME_KEYWORDS = ["弄", "巷"]
ROAD_TYPE_ALLEY_WIDTH_THRESHOLD_M = 4.0
ROAD_TYPE_SERVICE_ACCESS_SUBTYPES = ["driveway", "parking_aisle"]

# ---------------------------------------------------------------------------
# M2：空間權重（SPEC.md §3.2 — 依 road_type 細分，取代舊版 alley/footway 二元 2x）
# ---------------------------------------------------------------------------
# 人身安全事件空間權重
PERSONAL_SAFETY_SPATIAL_WEIGHT = {
    "steps": 2.0,
    "path": 2.0,
    "service_alley": 1.6,
    "alley": 1.5,
    "footway": 1.3,
    "residential": 1.0,
    "service_access": 0.6,
    "tertiary": 0.8,
    "secondary": 0.6,
    "primary": 0.5,
}

# 交通事故空間權重
TRAFFIC_ACCIDENT_SPATIAL_WEIGHT = {
    "primary": 3.0,
    "secondary": 3.0,
    "tertiary": 2.0,
    "residential": 1.0,
    "alley": 0.5,
    "service_alley": 0.5,
    "service_access": 0.2,
    "footway": 0.2,
    "path": 0.2,
    "steps": 0.2,
}

# 環境類（streetlight／obstruction／construction）：不分 road_type，一律 1.0
# crowd／other 未特別指定 road_type 偏好，也套用這個均一值。
ENVIRONMENTAL_SPATIAL_WEIGHT = 1.0

# ---------------------------------------------------------------------------
# M2：合成資料生成器（SPEC.md §3）
# ---------------------------------------------------------------------------
SYNTHETIC_DATA_SEED = 42
# 固定的資料集「結束日」，確保同一 seed 每次重跑產生完全相同的結果
# （若改用 now() 會讓輸出隨執行日期漂移，破壞可重現性）。
SYNTHETIC_DATA_END_DATE = "2026-09-14"
SYNTHETIC_DATA_DAYS = 365  # 12 個月回溯天數
SYNTHETIC_TOTAL_REPORTS_RANGE = (3000, 5000)

CATEGORY_DISTRIBUTION = {
    "streetlight": 0.30,
    "obstruction": 0.15,
    "personal_safety": 0.25,
    "traffic_accident": 0.12,
    "construction": 0.10,
    "crowd": 0.08,
}

SEVERITY_DISTRIBUTION = {
    "streetlight": {"minor": 0.70, "moderate": 0.30},
    "obstruction": {"minor": 0.60, "moderate": 0.40},
    "personal_safety": {"minor": 0.45, "moderate": 0.40, "severe": 0.15},
    "traffic_accident": {"moderate": 0.70, "severe": 0.30},
    "construction": {"minor": 0.80, "moderate": 0.20},
    "crowd": {"minor": 1.00},
}

PERSONAL_SAFETY_TAG_DISTRIBUTION = {
    "following": 0.35,
    "harassment": 0.30,
    "filming": 0.15,
    "exposure": 0.10,
    "other": 0.10,
}

# 時段權重：人身安全類集中 20:00-02:00
PERSONAL_SAFETY_NIGHT_HOURS = [20, 21, 22, 23, 0, 1]
PERSONAL_SAFETY_NIGHT_WEIGHT = 3.0
PERSONAL_SAFETY_DAY_WEIGHT = 1.0

# 時段權重：交通事故尖峰 07-09、17-19
# SPEC 只訂了尖峰時段範圍，沒訂權重倍數；此處沿用人身安全同量級的 3x，可調。
TRAFFIC_ACCIDENT_PEAK_HOURS = [7, 8, 9, 17, 18, 19]
TRAFFIC_ACCIDENT_PEAK_WEIGHT = 3.0
TRAFFIC_ACCIDENT_OFFPEAK_WEIGHT = 1.0

# 群聚：同事件多回報（供 M4 合併邏輯測試）
CLUSTER_EVENT_RATIO = 0.10
CLUSTER_EXTRA_REPORTS_MIN = 1
CLUSTER_EXTRA_REPORTS_MAX = 3
CLUSTER_RADIUS_M = 200
CLUSTER_TIME_WINDOW_MINUTES = 30

# 空間第二層權重：活動密度代理指標（用路段周邊 ACTIVITY_DENSITY_RADIUS_M 內的
# 路段數，正規化後代表人潮/活動程度，避免山區因路網稀疏而被稀釋掉的事件
# 反而被平均分配到）。用百分位數而非極值做正規化，避免離群值把大多數路段都壓縮到接近 0。
ACTIVITY_DENSITY_RADIUS_M = 300
ACTIVITY_DENSITY_LOWER_PERCENTILE = 5
ACTIVITY_DENSITY_UPPER_PERCENTILE = 95
ACTIVITY_DENSITY_FLOOR = 0.05  # 山區/孤立路段的最低係數，避免機率完全歸零

# 路段長度加權：抽樣機率 ∝ length_m ** 此指數（0.5 = sqrt，避免超長路段過度中選）
ROAD_LENGTH_SAMPLING_EXPONENT = 0.5

# 每筆 report 的 location_confidence 隨機範圍（SPEC 未訂數值，先給合理區間）
SYNTHETIC_LOCATION_CONFIDENCE_RANGE = (0.7, 1.0)

# ---------------------------------------------------------------------------
# M3：風險引擎（SPEC.md §4）
# ---------------------------------------------------------------------------
# 維度②：base_severity（§4.1）。佔位值：相對大小關係依調校結果設定。
SEVERITY_BASE_SCORE = {"minor": 0.25, "moderate": 0.5, "severe": 0.9}

# 環境類（不衰減）：streetlight／obstruction／construction
ENVIRONMENTAL_CATEGORIES = ["streetlight", "obstruction", "construction"]
# 環境類的狀態機轉換門檻（官方回覆管道尚未建立，此階段只有 dismiss_count 這條路徑）
REPAIR_DISMISS_COUNT_THRESHOLD = 3

# 半衰期（小時）。環境類不在此表中——decay.py 對環境類套用「half_life=∞」
# 的等價寫法（0.5**(Δt/∞)=1，current_risk 恆為 base_severity），與其餘類別共用同一公式。
# 佔位值：實際數值依各類別事件的真實衰退速度校準，不是這裡的示意值。
HALF_LIFE_HOURS = {
    ("personal_safety", "severe"): 8,
    ("personal_safety", "moderate"): 8,
    ("personal_safety", "minor"): 8,
    ("traffic_accident", "moderate"): 8,
    ("traffic_accident", "severe"): 8,
    ("crowd", "minor"): 8,
}

# 完全過期（小時）。超過此值 → status=expired，退出即時層。環境類不適用（None＝不會自動過期）。
# 佔位值：實際數值依調校結果設定。
FULL_EXPIRY_HOURS = {
    ("personal_safety", "severe"): 48,
    ("personal_safety", "moderate"): 48,
    ("personal_safety", "minor"): 48,
    ("traffic_accident", "moderate"): 48,
    ("traffic_accident", "severe"): 48,
    ("crowd", "minor"): 48,
}

# 維度③：長期基準風險的歷史事件時間窗（天）。crowd 不計入（None）；
# 環境類不用天數窗，改用「status=active」持續計入（見 baseline.py）。
# 佔位值：實際天數依調校結果設定。
BASELINE_TIME_WINDOW_DAYS = {
    ("personal_safety", "severe"): 180,
    ("personal_safety", "moderate"): 180,
    ("personal_safety", "minor"): 90,
    ("traffic_accident", "moderate"): 90,
    ("traffic_accident", "severe"): 90,
}

# KDE 參數（§4.2 + 使用者補充規格一：沿路段每 KDE_SAMPLE_INTERVAL_M 公尺取樣一點，
# 取樣點集合各自算高斯核加權和後取平均，取代單純用路段中心點）。
# 佔位值：半徑/頻寬依調校結果設定。
KDE_RADIUS_M = 300
KDE_BANDWIDTH_M = 150
KDE_SAMPLE_INTERVAL_M = 25
KDE_UPPER_PERCENTILE = 95

# 風險合成（§4.3）。佔位值：實際係數依調校結果設定。
BASELINE_RISK_MULTIPLIER = 0.5  # total_risk = 1-(1-realtime)*(1-baseline*此值)

# 擴散（§4.3）：依 category（personal_safety 再依 severity）決定一跳／二跳比例。
# 固定設施類（streetlight/obstruction/construction）不會移動，不擴散；
# 涉及移動的人（personal_safety/traffic_accident/crowd）保留擴散。
# 值為 (一跳比例, 二跳比例)；personal_safety 依 severity 細分，其餘類別不分 severity。
# 佔位值：實際比例依調校結果設定。
DIFFUSION_RATES = {
    "personal_safety": {
        "severe": (0.3, 0.1),
        "moderate": (0.3, 0.1),
        "minor": (0.3, 0.1),
    },
    "traffic_accident": (0.1, 0.0),
    "crowd": (0.1, 0.0),
    "streetlight": (0.0, 0.0),
    "obstruction": (0.0, 0.0),
    "construction": (0.0, 0.0),
}

# display_tier 門檻（§4.3）。佔位值：實際門檻依調校結果設定。
DISPLAY_TIER_LOW_MAX = 0.4      # < 此值 -> low
DISPLAY_TIER_MEDIUM_MAX = 0.7   # [LOW_MAX, 此值) -> medium；>= 此值 -> high

# 顯示門檻（產品決策，§4.3；B7 調整為 report_count + confirm_count）：單筆回報即
# 顯示，不設佐證門檻，但 display_tier 上限受佐證數限制——路段上觸及的 active 事件
# 裡沒有任何一個 (report_count + confirm_count) >= 門檻時，display_tier 最高只能
# 到這個值（即使 total_risk >= 0.75）。report_count 與 confirm_count 分開儲存，
# 只有在算這個門檻時才相加。佔位值：實際門檻依調校結果設定。
TIER_CAP_SINGLE_REPORT = "medium"
TIER_CAP_MIN_REPORT_COUNT = 3

# 事件合併粗篩（§4.4，B7 實作）。佔位值：實際距離/時間窗依調校結果設定。
MERGE_DISTANCE_M = 150
MERGE_TIME_WINDOW_MINUTES = {
    "personal_safety": 20,
    "traffic_accident": 20,
    "crowd": 20,
    "streetlight": 240,
    "obstruction": 240,
    "construction": 240,
}

# 排程（§4.5）
SCHEDULER_INTERVAL_MINUTES = 5
BASELINE_RECOMPUTE_INTERVAL_HOURS = 24

# ---------------------------------------------------------------------------
# M4：安全路線（SPEC.md §5）
# ---------------------------------------------------------------------------
ROUTE_ALPHA_FAST = 0.0
# 佔位值：實際數值是用真實路網與事件資料反覆測試校準出來的「定案值」，
# 不是這裡的示意值；校準方法見 SPEC.md §5，校準紀錄不納入版本控制。
ROUTE_ALPHA_SAFE = 3.0
WALKING_SPEED_MPS = 1.3
ROUTE_OVERLAP_THRESHOLD = 0.90
# 權重 = length_m × (1 + α × total_risk^ROUTE_RISK_EXPONENT)。指數 > 1 讓輕微風險
# 幾乎不影響路線，只對高風險路段強力避開，減少不必要的繞路。佔位值。
ROUTE_RISK_EXPONENT = 2

# ---------------------------------------------------------------------------
# 路過驗證與佐證資格（SPEC.md §8，B8 實作）
# ---------------------------------------------------------------------------
MAP_MATCH_TOLERANCE_MIN_M = 25
MAP_MATCH_TOLERANCE_MAX_M = 50
MAP_MATCH_MIN_TRAVEL_M = 15
MAP_MATCH_MAX_BEARING_DIFF_DEG = 45
# 判定「靜止、跳過方向檢查」的速度閾值。使用者提供的參數清單未包含此值，
# 暫填 0.3（WALKING_SPEED_MPS=1.3 的零頭），B8 實作前需要重新確認。
MAP_MATCH_STATIONARY_SPEED_MPS = 0.3
JUNCTION_ADJACENCY_M = 20
PASSAGE_LOG_RETENTION_DAYS = 7

# passage_log 只存粗略時段（§8.2 資料最小化），不存精確時間戳。資格判定
# （§8.3/§8.4 的 24 小時／7 天窗口）一律用該時段的「最晚可能時間」換算，
# 讓時段近似造成的誤差方向固定偏寬鬆（B8 決議：寧可多給資格，不要誤判為失格）。
# 24 代表次日 00:00。
DAY_PERIODS = ["dawn", "morning", "afternoon", "evening"]
DAY_PERIOD_LATEST_HOUR = {"dawn": 6, "morning": 12, "afternoon": 18, "evening": 24}

# 連續性／方向判定用的「上一筆定位」暫存（user_last_ping，會被覆寫，非歷史紀錄）
# 超過此秒數視為過期，不用於比對，當作新的起始點（B8 決議，避免久未開啟 App 時
# 跟過舊的位置做比對）。
LAST_PING_VALIDITY_SECONDS = 300

# 佐證資格（confirm）時間窗：依 category 查表。佔位值：實際門檻依調校結果設定。
CORROBORATE_WINDOW_HOURS = {
    "personal_safety": 24,
    "traffic_accident": 24,
    "crowd": 24,
    "streetlight": 24 * 7,
    "obstruction": 24 * 7,
    "construction": 24 * 7,
}
# 解除資格（dismiss）時間窗：所有類型一律 24 小時。佔位值。
DISMISS_WINDOW_HOURS = 24

# 非現場回報（未通過路過驗證）的 location_confidence 預設值
REMOTE_REPORT_LOCATION_CONFIDENCE = 0.4

# 路過紀錄到期處理：累加至 user_segment_frequency 後刪除原始紀錄（§8.2）
PASSAGE_TO_FREQUENCY_ENABLED = True
FREQUENCY_DECAY_WEEKLY = 0.95   # 常走路段計數每週衰減係數
FREQUENCY_MIN_COUNT = 3         # 衰減後低於此計數即刪除該筆紀錄
# user_segment_frequency 多久未更新才開始衰減（判斷依據是 last_updated）。
# 跟 PASSAGE_LOG_RETENTION_DAYS（passage_log 的保存期限，判斷依據是
# created_at）用途不同、是兩個獨立的設計決策——目前數值剛好都是 7 天純屬巧合，
# 兩者不得互相替代，日後各自調整不應互相牽動。
FREQUENCY_DECAY_IDLE_DAYS = 7

# ---------------------------------------------------------------------------
# M7：驗證用網頁地圖與 API（SPEC.md §9）
# ---------------------------------------------------------------------------
OSM_POI_TAGS = {
    "amenity": ["school", "university", "hospital", "clinic", "police"],
    "railway": ["station"],
    "leisure": ["park"],
    "shop": ["convenience"],
}
# 判定使用者座標是否落在文山區路網服務範圍內：座標到最近 road_node 的距離
# 超過此門檻視為範圍外，退回預設起點。150m（非 300m）：文山區路網密度足以
# 支撐較嚴格的門檻，避免區界外數條街被誤判為範圍內。
SERVICE_AREA_SNAP_TOLERANCE_M = 150
DEFAULT_START_POI_NAME_HINT = "國立政治大學"  # 用於從 poi 表查詢預設起點座標
TIMELINE_START = "2026-09-07T00:00:00+08:00"
TIMELINE_END = "2026-09-14T23:00:00+08:00"
BASELINE_SNAPSHOT_CACHE_ENABLED = True

# ---------------------------------------------------------------------------
# M7：視覺樣式（依 Figma 設計稿 fileKey=yyQPERYt36339bp5Qj8EwV node-id=69-74 校準，
# 2026-09-24。這些是給前端 web/index.html 對照用的權威色碼，前端的 JS 常數必須與
# 這裡保持一致——目前專案沒有前端 build 流程，無法讓 index.html 直接 import 這個
# 檔案，所以是人工同步，非自動讀取。）
# ---------------------------------------------------------------------------
# 事件標記（圓形底 + 白色圖示，直徑 28px，圖示 16px）
EVENT_MARKER_COLOR_HIGH = "#E5383B"    # 非環境類事件 severity=severe
EVENT_MARKER_COLOR_AMBER = "#FFA000"   # 非環境類事件 severity=moderate/minor；環境類不論等級一律此色
EVENT_MARKER_DIAMETER_PX = 28
EVENT_MARKER_ICON_PX = 16

# 事件類別 -> Lucide 圖示名稱。Figma 原型用另一套圖示庫（hacker/barrier(2)/lightbulb/
# car-crash/water-drops/team/more-horizontal），這裡依語意挑選 Lucide 最接近者。
EVENT_CATEGORY_ICON = {
    "personal_safety": "shield-alert",
    "construction": "construction",
    "streetlight": "lightbulb-off",
    "traffic_accident": "car-front",
    "obstruction": "droplets",
    "crowd": "users",
    "other": "ellipsis",
}
# zoom < 此值不顯示個別事件標記，改以路段風險線呈現
EVENT_MARKER_MIN_ZOOM = 14

# 同一路段多個 active 事件合併為一個標記時，顯示風險等級最高的事件類別；
# 同等級時依此順序決定代表類別（索引越小優先權越高）。crowd 未被使用者明確歸類到
# personal_safety/traffic_accident/環境類任一組，這裡把它排在 traffic_accident 之後、
# 環境類之前，是實作上的假設。
EVENT_MARKER_CATEGORY_PRIORITY = [
    "personal_safety",
    "traffic_accident",
    "crowd",
    "streetlight",
    "obstruction",
    "construction",
]

# 路線圖層
ROUTE_MODE_BACKGROUND_OPACITY = 0.3   # 顯示路線時，背景風險線透明度降至此值
ROUTE_TIER_COLOR_LOW = "#00E676"      # 路線分段上色（依所經路段 display_tier）
ROUTE_TIER_COLOR_MEDIUM = "#FFA000"
ROUTE_TIER_COLOR_HIGH = "#E5383B"

# ---------------------------------------------------------------------------
# 防濫用頻率上限（SPEC.md §9，B7 實作）
# ---------------------------------------------------------------------------
REPORT_RATE_LIMIT_HOURLY = 5
REPORT_RATE_LIMIT_DAILY = 20
CORROBORATE_RATE_LIMIT_HOURLY = 10
ABUSE_SUSPENSION_HOURS = 24

# ---------------------------------------------------------------------------
# B7：回報吸附與定位信心（SPEC.md §9）
# ---------------------------------------------------------------------------
# GPS 精度（公尺）-> location_confidence：<=MIN 給滿分，>=MAX 給下限，中間線性內插
ACCURACY_CONFIDENCE_MIN_M = 10
ACCURACY_CONFIDENCE_MAX_M = 50
ACCURACY_CONFIDENCE_FLOOR = 0.3

# 吸附路段的最大距離；超過則拒絕該筆回報（不像 §8.5 的「任何位置皆可回報」沒有
# 距離限制——這裡限制的是「吸附到哪條路段」的合理範圍，不是回報本身的資格）
SNAP_MAX_DISTANCE_M = 100

# ---------------------------------------------------------------------------
# B10：通知佇列（本階段只寫入、不實際發送）
# ---------------------------------------------------------------------------
NOTIFICATION_RADIUS_M = 200  # 以 user_last_ping 最後位置為準的入列範圍
