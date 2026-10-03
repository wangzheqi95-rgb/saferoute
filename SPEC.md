# SafeRoute 風險引擎 開發規格書（SPEC.md）

> 版本：v1.0（2026-08-22）
> 給 Claude Code 的實作依據。範圍＝台北市文山區。本階段**不做 App 前端**，只做風險引擎＋驗證用網頁地圖。
> 開發原則：每個模組完成後可獨立驗證，通過驗收條件才進下一個模組。

---

## 0. 專案概要

**目標**：證明「資料進來 → 風險計算 → 地圖與路線輸出」這條鏈路可即時運作，且真實資料可直接換裝。

**技術棧**
- 語言：Python 3.11+
- 資料庫：PostgreSQL 16 + PostGIS 3
- 後端框架：FastAPI
- 地理處理：osmnx、geopandas、shapely、networkx
- 數值：numpy、scipy、pandas
- 驗證前端：單一 HTML + MapLibre GL JS（不用框架）

**不在本階段範圍**：Flutter App、使用者帳號系統、實際推播、LLM 串接（M6 才接）、聯邦學習、街景分析。

**專案結構**
```
saferoute-engine/
├── SPEC.md                  # 本文件
├── .env.example             # DB 連線等設定範本
├── requirements.txt
├── src/
│   ├── db/
│   │   ├── schema.sql       # 建表 DDL
│   │   └── connection.py
│   ├── ingest/
│   │   ├── osm_network.py   # M1 路網匯入
│   │   └── adapters/        # M5 資料轉接層
│   │       ├── base.py
│   │       ├── synthetic.py
│   │       └── aggregate.py
│   ├── generator/
│   │   └── synthetic_events.py  # M2 合成資料
│   ├── engine/
│   │   ├── decay.py         # M3 衰減
│   │   ├── baseline.py      # M3 KDE 基準風險
│   │   ├── aggregate.py     # M3 風險合成
│   │   └── scheduler.py     # M3 5 分鐘重算
│   ├── routing/
│   │   └── safe_route.py    # M4 路線計算
│   └── api/
│       └── main.py          # FastAPI
├── web/
│   └── index.html           # 驗證用地圖
└── tests/
```

---

## 1. 資料庫 Schema

> 座標系統統一 **EPSG:4326**（WGS84）儲存；距離計算轉 **EPSG:3826**（TWD97 台灣二度分帶）。

### 1.1 `road_segment` 路段

| 欄位 | 型別 | 說明 |
|---|---|---|
| segment_id | TEXT PK | OSM way id（同 way 分段時加後綴 `_1`、`_2`） |
| name | TEXT | 路名，可 null |
| geometry | GEOMETRY(LineString, 4326) | 幾何，建 GIST 索引 |
| length_m | DOUBLE PRECISION | 長度（公尺） |
| road_type | TEXT | primary/secondary/residential/alley/footway |
| streetlight_count | INTEGER | 預設 null |
| baseline_risk | DOUBLE PRECISION | 0–1，預設 0 |
| updated_at | TIMESTAMPTZ | |

索引：`GIST(geometry)`

### 1.2 `road_node` 路口（路線計算用）

| 欄位 | 型別 | 說明 |
|---|---|---|
| node_id | TEXT PK | OSM node id |
| geometry | GEOMETRY(Point, 4326) | |

### 1.3 `segment_topology` 路段連接關係

| 欄位 | 型別 | 說明 |
|---|---|---|
| segment_id | TEXT FK | |
| from_node / to_node | TEXT FK | |
| is_oneway | BOOLEAN | 步行預設 false |

### 1.4 `report` 原始回報

| 欄位 | 型別 | 說明 |
|---|---|---|
| report_id | UUID PK | |
| user_id | TEXT | **90 天後由清理任務設為 null** |
| display_code | TEXT | 每筆隨機 4 碼，前端顯示用 |
| category | TEXT | 見 §1.7 列舉 |
| tags | TEXT[] | 人身安全快速標籤，可空 |
| severity | TEXT | minor / moderate / severe |
| raw_text | TEXT | 用戶原文，**僅後端** |
| ai_summary | TEXT | 前端顯示版（M6 前留空） |
| location | GEOMETRY(Point, 4326) | |
| segment_id | TEXT FK | 吸附結果 |
| location_confidence | DOUBLE PRECISION | 0–1 |
| text_confidence | DOUBLE PRECISION | 0–1，M6 前預設 1.0 |
| created_at | TIMESTAMPTZ | |
| event_id | UUID FK | 合併後歸屬 |
| source | TEXT | user / synthetic / official |

### 1.5 `event` 事件（顯示與計算單位）

| 欄位 | 型別 | 說明 |
|---|---|---|
| event_id | UUID PK | |
| category / severity | TEXT | 由所屬 report 加權決定 |
| segment_id | TEXT FK | 主要路段 |
| affected_segments | TEXT[] | 擴散影響路段 |
| status | TEXT | active / resolved / expired / repaired |
| report_count | INTEGER | 回報人數（`COUNT DISTINCT user_id`），單筆即顯示（無佐證門檻），一律標示於前端 |
| confirm_count | INTEGER | 佐證（confirm）人數（B7 決議，`COUNT DISTINCT user_id`）；**不計入** report_count，分開儲存與顯示；與 report_count 相加後用於 display_tier 上限判定，見 §4.3 |
| dismiss_count | INTEGER | 解除數，累積達一定數量後生效（門檻依調校結果設定，集中於 `config.py`） |
| first_at / last_at | TIMESTAMPTZ | last_at 有新回報即更新（重置衰減時鐘） |
| expires_at | TIMESTAMPTZ | 依 §3.1；環境類為 null |
| current_risk | DOUBLE PRECISION | 0–1 即時風險 |
| repaired_at | TIMESTAMPTZ | 環境類專用 |

### 1.6 `corroboration` 佐證

| 欄位 | 型別 | 說明 |
|---|---|---|
| id | UUID PK | |
| event_id | UUID FK | |
| user_id | TEXT | UNIQUE(event_id, user_id) — 一人一事件限一次 |
| action | TEXT | confirm / dismiss |
| created_at | TIMESTAMPTZ | |

### 1.7 `segment_risk` 引擎輸出

| 欄位 | 型別 | 說明 |
|---|---|---|
| segment_id | TEXT PK FK | |
| realtime_risk | DOUBLE PRECISION | 0–1 |
| baseline_risk | DOUBLE PRECISION | 0–1 |
| total_risk | DOUBLE PRECISION | 0–1 |
| display_tier | TEXT | low / medium / high |
| updated_at | TIMESTAMPTZ | |

**category 列舉**：`personal_safety`（人身安全）／`streetlight`（路燈故障）／`obstruction`（積水或障礙）／`traffic_accident`（交通事故）／`construction`（道路施工）／`crowd`（人群聚集）／`other`

**personal_safety 的 tags 列舉**：`following`（尾隨跟蹤）／`harassment`（搭訕糾纏）／`filming`（疑似偷拍）／`exposure`（暴露行為）／`other`

---

## 2. M1：路網匯入

**輸入**：OSM（osmnx，`network_type="walk"`），範圍 = 台北市文山區行政區界。

**處理**
1. 下載路網，取得 nodes / edges
2. edges → `road_segment`（長路段可依路口切分，segment_id 加後綴）
3. nodes → `road_node`，連接關係 → `segment_topology`
4. `road_type` 判斷優先序（依 name／highway／width／service 子標籤）：
   1. `name` 含「弄」或「巷」→ `alley`
   2. `highway=service` → `service_access`（`service=driveway` 或 `parking_aisle`）或 `service_alley`（`service=alley`／無子標籤／其他子標籤）
   3. `highway=unclassified` → `residential`
   4. `highway=residential/living_street` → `residential`
   5. `highway=steps` → `steps`
   6. `highway=path` → `path`
   7. `highway=footway/pedestrian` → `footway`
   8. `highway=primary/secondary/tertiary` → 同名
   9. 其他（含 trunk/track/*_link 等未列類型）→ `residential`
   10. 輔助規則：已歸 `residential` 但 `width` < 4 公尺 → 改判 `alley`

   `road_type` 列舉值：`primary`／`secondary`／`tertiary`／`residential`／`alley`／`service_alley`／`service_access`／`footway`／`path`／`steps`（門檻值集中於 `config.py`）
5. 長度用 EPSG:3826 計算

**驗收**：文山區路段數 > 3,000；隨機抽 10 條路段在 QGIS 或網頁地圖上位置正確；`segment_topology` 無孤立節點（連通分量最大者涵蓋 > 95% 路段）。

---

## 3. M2：合成資料生成器

**目標**：產生 12 個月、符合官方統計特性的事件流。**這是模擬資料，所有輸出檔名與 log 必須標記 `synthetic`。**

### 3.1 事件類型分布（依警政統計調整，初版比例）

| category | 佔比 | severity 分布 |
|---|---|---|
| streetlight | 30% | minor 70% / moderate 30% |
| obstruction | 15% | minor 60% / moderate 40% |
| personal_safety | 25% | minor 45% / moderate 40% / severe 15% |
| traffic_accident | 12% | moderate 70% / severe 30% |
| construction | 10% | minor 80% / moderate 20% |
| crowd | 8% | minor 100% |

> personal_safety 的 tags 分布：following 35%／harassment 30%／filming 15%／exposure 10%／other 10%（參照跟騷法案件樣態，盯梢尾隨為大宗）

### 3.2 時空分布規則

- **時段**：人身安全類事件在夜間時段生成機率較高、日間較低；交通事故類在通勤尖峰時段機率較高、其餘時段較低；環境類全時段均勻生成（時段範圍與權重倍數集中於 `config.py`，依調校結果設定，可調）
- **空間（第一層：road_type 權重）**：以路段 `road_type` 為單位加權事件生成機率（權重表集中於 `config.py` `PERSONAL_SAFETY_SPATIAL_WEIGHT`／`TRAFFIC_ACCIDENT_SPATIAL_WEIGHT`，依調校結果設定）：人身安全類事件偏好人煙較少、照明較差的路段類型（如階梯、山徑、巷弄），交通事故類事件偏好車流量較大的主要道路，兩者的權重方向相反；環境類與其餘類別不分 road_type，空間權重一律相同（`ENVIRONMENTAL_SPATIAL_WEIGHT`）。

- **空間（第二層：活動密度係數）**：文山區西北市區與東南山區的路網密度落差極大，若只靠 road_type 權重，事件仍會依路網長度／段數平均灑到人煙稀少的山區。加入活動密度係數作為人口活動的代理指標：
  1. 以每條路段中心點為圓心，計算一定半徑範圍內（`ACTIVITY_DENSITY_RADIUS_M`，TWD97 計算）的路段數量，作為原始密度值
  2. 用百分位數做 min-max 正規化到 0–1（`ACTIVITY_DENSITY_LOWER_PERCENTILE`／`UPPER_PERCENTILE`），避免離群值把多數路段壓縮到接近 0
  3. 設一個非零下限（`ACTIVITY_DENSITY_FLOOR`），讓山區/孤立路段仍有非零機率，而非完全排除

- **空間（第三層：路段長度加權）**：路段抽樣機率額外乘上 `length_m` 的次方（指數集中於 `config.py` `ROAD_LENGTH_SAMPLING_EXPONENT`），避免長路段如山徑因長度優勢過度中選

  **最終空間抽樣機率** ∝ `road_type 權重 × 活動密度係數 × length_m ** 指數`，全區正規化後供每筆事件抽樣路段。以上第一／二／三層的實際權重數值、百分位數、下限與指數皆依調校結果設定，集中於 `config.py`，不在此列出。

- **群聚**：部分事件為「同事件多回報」，在既有事件的合理時空距離內再生多筆 report、共用 event_id（供 M4 合併邏輯測試；距離/時間窗邏輯與 §4.4 的即時合併規則呼應，實際數值集中於 `config.py`）
- **總量**：12 個月份量的合成事件，規模可於 `config.py` 調整

**驗收**：跑一次產生的資料，區級月總量與時段分布須與設定比例誤差 < 10%；能重複執行且以 seed 固定結果。產生後另外輸出自我驗證統計：各 `road_type` 事件數佔比（對照權重表方向）、事件時段分布直方圖、事件密度最高前 10 條路段、同事件多回報群組實際生成比例。

---

## 4. M3：風險引擎

### 4.1 衰減與過期（三維度）

**維度①：狀態機（環境類）**
`streetlight`、`obstruction`、`construction` 不隨時間衰減。狀態：`active` → `repaired`（官方回覆或 dismiss_count ≥ 3）。

**維度②：即時風險（其餘類別）**
指數衰減（半衰期公式，公開的時效處理方法）：
```
current_risk = base_severity × 0.5 ^ (Δt / half_life)
Δt = now - last_at
```
`base_severity` 依嚴重度分級（minor／moderate／severe）遞增設定，實際數值集中於 `config.py`（`SEVERITY_BASE_SCORE`），依調校結果設定。

半衰期（`half_life`）與完全過期時間依 category／severity 分級設定：嚴重度越高，半衰期與完全過期時間越長；personal_safety 依嚴重度細分三級，traffic_accident 與 crowd 各自一級；環境類不衰減、只能靠修復關閉。實際數值集中於 `config.py`（`HALF_LIFE_HOURS`／`FULL_EXPIRY_HOURS`），依調校結果設定。

超過「完全過期」→ status = `expired`，退出即時計算、轉入基準層。
新 report 進來 → 更新 `last_at`，衰減時鐘重置。

**維度③：長期基準風險**
過期事件沉澱為路段體質，以 KDE 計算（見 4.2）：
- personal_safety severe/moderate：計入較長的回溯時間窗
- personal_safety minor、traffic_accident：計入較短的回溯時間窗
- crowd：不計入
- 環境類：`active` 期間持續計入

（實際回溯天數集中於 `config.py` `BASELINE_TIME_WINDOW_DAYS`，依調校結果設定）

### 4.2 基準風險（KDE）

沿路網做核密度估計（KDE，公開的統計方法），不用方格：
1. 取符合時間窗的歷史事件，權重 = `base_severity × location_confidence`
2. 路段取樣點：每條路段沿線以固定間距取樣多點，取代單純用路段中心點——長路段只取中心點會低估密度。每個取樣點各自計算一定半徑內事件的高斯核加權和（bandwidth 與半徑經校準設定，距離用 EPSG:3826），該路段最終值 = 其所有取樣點的平均值
3. 全區正規化到 0–1（用高百分位數當上界避免離群值壓縮，超過者設 1.0）
4. 寫入 `road_segment.baseline_risk`

（取樣間距、半徑、bandwidth、百分位數等實際數值集中於 `config.py`（`KDE_RADIUS_M`／`KDE_BANDWIDTH_M`／`KDE_SAMPLE_INTERVAL_M`／`KDE_UPPER_PERCENTILE`），依調校結果設定）

> 註：ETAS/Hawkes 本階段**不實作**，僅在文件保留為日後選項。

### 4.3 風險合成與擴散

```
realtime_risk(segment) = 1 - Π(1 - current_risk_i)   # 該路段所有 active 事件的機率式合成
total_risk = 1 - (1 - realtime_risk) × (1 - baseline_risk × w)
```
> `w` 為基準風險的影響權重（小於 1，集中於 `config.py` `BASELINE_RISK_MULTIPLIER`，依調校結果設定）：長期體質不應單獨把路段推上高風險，需即時事件加成。

**擴散**：依事件 category（personal_safety 再依 severity）決定一跳／二跳比例（`config.py` `DIFFUSION_RATES`，依調校結果設定），固定設施類不會移動故不擴散，涉及移動的人保留擴散，嚴重度越高、擴散的範圍與強度越大。寫入 `event.affected_segments`。

**display_tier**：`total_risk` 依兩個內部門檻切分為 low／medium／high 三級，門檻集中於 `config.py`（`DISPLAY_TIER_LOW_MAX`／`DISPLAY_TIER_MEDIUM_MAX`），依調校結果設定。

**顯示門檻（產品決策，B7 調整）**：
1. 單筆回報即顯示該事件，不設佐證門檻（不再要求計數達一定數量才顯示）。
2. 事件一律分開標示 `report_count`（回報人數）與 `confirm_count`（佐證人數）；confirm **不計入** report_count。
3. `display_tier` 的上限受佐證數限制：路段上若有 active 事件（直接歸屬或擴散貢獻）觸及，且這些事件裡沒有任何一個 `report_count + confirm_count` 達到門檻，該路段的 `display_tier` 最高只能到 `medium`（門檻與封頂等級集中於 `config.py` `TIER_CAP_SINGLE_REPORT`／`TIER_CAP_MIN_REPORT_COUNT`，依調校結果設定），即使算出來的 total_risk 已達到 high 等級。只要有任一觸及該路段的 active 事件達到這個獨立回報/佐證數量門檻，就不設限，可達 `high`。路段若完全沒有 active 事件觸及（風險純粹來自 baseline），不受此限制。

### 4.4 事件合併（同事件多回報）

新 report 進來時，若同時滿足以下條件，歸入既有 event：
- 同 category
- 空間：同路段或距離在合理範圍內
- 時間：與 `last_at` 差距在合理時間窗內——涉及移動的人（人身安全類、traffic_accident、crowd）用較短窗口，固定設施類（streetlight／obstruction／construction）用較長窗口

（實際距離與時間窗數值集中於 `config.py` `MERGE_DISTANCE_M`／`MERGE_TIME_WINDOW_MINUTES`，依調校結果設定）

合併後：`report_count += 1`（`COUNT DISTINCT user_id`，同一 user_id 對同一 event 的重複回報只計 1 次）；severity 取最高值；`last_at` 更新。

**多候選 tie-break（B7 決議）**：粗篩後若有多個候選事件同時符合條件，優先選距離最近者；距離相同則取 `last_at` 最新者。

**判定不看 raw_text（B7 決議）**：粗篩規則本身即為完整判準，不讀取 report 的 `raw_text` 內容；M6 導入 LLM 判定時才會用文字內容做最終判定或否決，粗篩邏輯本身不需更動。

### 4.5 排程

`scheduler.py` 每 5 分鐘：重算所有 active 事件的 `current_risk` → 標記過期 → 重算受影響路段的 `segment_risk` → 更新 `updated_at`。
基準風險（KDE）每日重算一次即可。

**驗收**：注入一筆 severe 人身安全事件，該路段 tier 立即轉 high；隨時間經過風險依半衰期公式遞減；超過完全過期時間後退出即時層但 baseline 微幅上升。相鄰路段有對應擴散值。

### 4.6 回報吸附與定位信心（B7 決議）

- **吸附路段最大距離**：`POST /reports` 用 PostGIS KNN 吸附到最近路段，若最近路段距離超過門檻（`SNAP_MAX_DISTANCE_M`，100m），拒絕該筆回報（回傳 400 與可讀錯誤訊息），不寫入任何資料。這個限制只管「吸附到哪條路段合理」，不是 §8.5「任何位置皆可回報」的資格限制。
- **`accuracy_m` → `location_confidence`**：裝置回報的 GPS 精度（公尺）線性轉換為信心分數——精度優於下限給滿分，劣於上限給下限，中間線性內插。三個常數（`ACCURACY_CONFIDENCE_MIN_M`／`MAX_M`／`FLOOR`）集中於 `config.py`，依調校結果設定，與 §8.6 的 `MAP_MATCH_TOLERANCE_*`（路過判定用）是各自獨立的門檻，不共用。

---

## 5. M4：安全路線

用 networkx 建圖（`MultiGraph`，因為路網中同一對節點間可能有平行路段），邊權重：
```
weight = length_m × (1 + α × total_risk^p)
```
`p`＝`ROUTE_RISK_EXPONENT`，指數 > 1 讓輕微風險幾乎不影響路線，只對高風險路段強力避開，減少不必要的繞路。
- 最快路線：α = 0（不考慮風險）
- 最安心路線：α 為可調的避險強度係數，數值越大越傾向繞開高風險路段

  **校準方法**：在實際路網上以多組起訖點測試不同 α 值的避險效果（能否有效避開 high tier 路段）與代價（繞路比例），在繞路代價可接受的範圍內取避險效果最佳的值；此法屬於邊際分析，資料集或事件密度改變後需重新校準。`p` 與 `α` 的實際數值集中於 `config.py`（`ROUTE_RISK_EXPONENT`／`ROUTE_ALPHA_SAFE`），依調校結果設定，不在此列出。

輸出兩條路線：距離、預估步行時間、路徑上的 `max_tier` 與平均風險。若兩條路線重疊度過高，回傳「此區域無明顯更安全的替代路線」（重疊門檻集中於 `config.py` `ROUTE_OVERLAP_THRESHOLD`）。

**驗收**：在文山區任選起訖點，最安心路線的平均風險低於最快路線，且繞路距離在合理範圍內（門檻經校準設定）。

---

## 6. M5：Adapter 層

`adapters/base.py` 定義統一介面：
```python
class EventSourceAdapter:
    def fetch(self, since: datetime) -> list[ReportDTO]: ...
```
- `synthetic.py`：讀 M2 產出（本階段唯一啟用）
- `aggregate.py`：**聚合級輸入**（區級／月級官方統計）→ 不產生單筆 event，而是直接調整該區所有路段的 `baseline_risk`（依區內路段類型加權分配）

> 引擎只認 ReportDTO，不認來源。未來拿到事件級真實資料只需新增一個 adapter。

---

## 7. M6：LLM 回報結構化（後續）

輸入：category、tags、severity、自由文字 → 輸出：`ai_summary`＋`text_confidence`。

**顯示規則（寫入 prompt，硬性）**
- 只輸出行為＋路段＋時段
- **禁止**輸出：臉部特徵、車牌、店家名稱、族裔
- 性別、衣著顏色：僅在事件發生 2 小時內可輸出，逾時版本自動移除
- 文字內容與所選 severity 明顯不符 → `text_confidence` 下修至 0.5 以下

---

## 8. 路過驗證與佐證資格（Map Matching）

> B8 實作（`src/engine/passage.py`、`src/engine/passage_cleanup.py`）。

**取代說明**：本節之路過驗證取代先前「佐證／解除需位於事件 200 公尺內」之設計；現場距離不再作為判定條件，一律以近期路過為準（見 §8.3、§8.4 的時間窗規則）。

### 8.0 定位回報端點（B8 補上，原規格遺漏）

```
POST /location_pings → 裝置端回報目前定位，供 §8.1 比對
```

- 輸入：`{location: [lon, lat], accuracy_m, speed_mps?, bearing_deg?}`，需要 `X-User-Id` 標頭。
- 不需要處於「導航中」狀態才能呼叫；每次呼叫都是一次獨立的路過判定機會。
- 不設頻率上限：定位回報本身不影響任何事件計數或顯示內容，只會累積路過紀錄，與 §9 的回報/佐證濫用風險不同。
- 回傳 `{matched_segment_id, confirmed_segments, reason}`：`matched_segment_id` 是貼路結果；`confirmed_segments` 是這次判定為「路過」而寫入 `passage_log` 的路段（含 §8.1 第 4 點路口鄰接展開的路段）；未判定為路過時為空陣列，`reason` 說明原因。

### 8.1 路過判定規則

判定使用者是否「路過」某路段（供 §8.3 佐證資格使用），依序檢查：

1. **貼路容忍距離**：依裝置回報的 GPS 精度動態調整，下限 25m、上限 50m；GPS 精度超過上限時不判定（精度太差，不採信）。
2. **連續性**：需連續兩次定位落在同一路段，或沿該路段移動累計 ≥ 15m，才算真的路過（單一瞬時定位點不算數，避免 GPS 飄移誤判）。比對用的「上一筆定位」（`user_last_ping`：每人一筆，每次定位進來就覆寫，不是歷史紀錄，不受 §8.2 保存規範約束）若距今超過 `LAST_PING_VALIDITY_SECONDS`（B8 決議，300 秒）視為過期、不納入比對，當作新的起始點——避免使用者久未開啟 App 時跟過舊的位置做比對。
3. **方向一致性**：移動方向與路段走向夾角需 < 45 度；速度低於靜止閾值時（判定為原地不動，沒有方向可言）跳過此項檢查。
4. **路口鄰接**：定位落在距路口 20m 內時，所有連接該路口的路段均計為路過（路口附近難以精確判斷走的是哪一條岔路，寧可寬鬆計入）。

### 8.2 路過紀錄保存（資料最小化）

- 僅儲存 `segment_id` 與粗略時段，**不儲存座標、不儲存精確時間戳**：`passage_log` 表（`user_id, segment_id, occurred_date, day_period`），`day_period` 為 `dawn`(00–06)／`morning`(06–12)／`afternoon`(12–18)／`evening`(18–24) 四個 6 小時區間（`config.py` `DAY_PERIODS`）。表內另有一個 `created_at` 僅供清理任務判斷是否滿 7 天用，不對外暴露，不是使用者行蹤時間戳。
- 保存 7 天後由清理任務刪除，比照 SPEC.md §12 第 3 點「90 天斷鏈」的精神，但週期更短，因為路過紀錄比回報內容更即時敏感。
- **§8.3／§8.4 資格判定的時段近似（B8 決議）**：`passage_log` 沒有精確時間戳，無法精準比對 24 小時／7 天窗口。一律用該時段的「最晚可能時間」（`config.py` `DAY_PERIOD_LATEST_HOUR`，例如 `afternoon` 取當天 18:00）換算成時間點再比對，讓時段近似造成的誤差方向固定偏寬鬆——寧可多給資格，不要把仍有資格的使用者誤判為不符資格。

**到期處理（累加後刪除）**：

1. 路過紀錄到期（滿 7 天）前，先把該筆紀錄累加進使用者的「常走路段計數」（`user_segment_frequency`：`user_id`、`segment_id`、`count`、`last_updated`），累加完成後才刪除原始路過紀錄。
2. 常走路段計數本身**不含任何時間戳以外的個資**——只保留累計次數（`last_updated` 僅用於下一點的週期衰減判斷，不是行程時間戳）。
3. 計數衰減的對象是「超過 `FREQUENCY_DECAY_IDLE_DAYS`（預設 7 天，`config.py`，與 `PASSAGE_LOG_RETENTION_DAYS` 是各自獨立的參數、不得互相替代，目前數值相同純屬巧合）未更新」的紀錄，衰減時乘上一個小於 1 的衰減係數（`FREQUENCY_DECAY_WEEKLY`，依調校結果設定），讓過時的移動習慣隨時間自然淡出，不需要額外的刪除邏輯。
4. 衰減後計數低於門檻者不保留（直接刪除該筆 `user_segment_frequency` 紀錄；門檻集中於 `config.py` `FREQUENCY_MIN_COUNT`，依調校結果設定），避免單次或極少次的行程被長期記錄下來。**刪除範圍的設計（刻意決策）**：刪除只作用於「本次被第 3 點選中並真的執行過衰減」的紀錄子集合，不是對整張表無差別掃描低於門檻的紀錄。因為衰減資格本身就要求 `last_updated` 已超過 `FREQUENCY_DECAY_IDLE_DAYS`，剛累加、`last_updated` 是當下時間的新紀錄天然不滿足這個條件、不會被選中衰減，自然也就不會被第 4 點誤刪——不需要另外設計一個「新紀錄保護期」參數。日後如果要修改刪除範圍的實作，必須保留「刪除對象 ⊆ 本次衰減對象」這個性質，否則剛累加的新紀錄會被誤殺。
5. 個人化功能（Premium 區域監看、早晨摘要的路段優先序）一律讀取 `user_segment_frequency` 的累計計數，**不讀取路過紀錄本身**——路過紀錄只是計算計數的中繼資料，生命週期不超過 7 天。

此功能可用 `PASSAGE_TO_FREQUENCY_ENABLED` 整體開關（關閉時只刪除到期的 `passage_log`、不累加進 `user_segment_frequency`——關掉的是「餵給個人化功能」這個下游用途，不影響 passage_log 本身的 7 天保存期限）；資料表為 `user_segment_frequency`（`user_id, segment_id, count, last_updated`），到期累加與每週衰減由 `src/engine/passage_cleanup.py` 執行（比照 `decay.py`／`aggregate.py`，`python -m src.engine.passage_cleanup` 手動或排程執行）。

### 8.3 佐證資格（corroborate：confirm）

使用者要對某事件按「confirm」，必須在合理的時間窗內路過該事件所在路段，才算有效佐證；時間窗依事件類型分級——涉及移動的人（personal_safety／traffic_accident／crowd）用較短窗口，固定設施類（streetlight／obstruction／construction）用較長窗口（實際時數集中於 `config.py` `CORROBORATE_WINDOW_HOURS`，依調校結果設定）。

### 8.4 解除資格（dismiss）

所有事件類型一律需在同一個較短的時間窗內走過該路段，才能按「dismiss」（時數集中於 `config.py` `DISMISS_WINDOW_HOURS`，依調校結果設定）。

### 8.5 回報（report）不受路過驗證限制

任何使用者、任何位置皆可提交回報（`POST /reports`），不需要滿足 §8.1 的路過判定。但非現場回報（未通過路過驗證）的 `report.location_confidence` 給予較低的預設值（見 `config.py` `REMOTE_REPORT_LOCATION_CONFIDENCE`），反映其可信度較低。

### 8.6 config.py 參數

| 參數 | 說明 |
|---|---|
| `MAP_MATCH_TOLERANCE_MIN_M` | 貼路容忍距離下限（25m） |
| `MAP_MATCH_TOLERANCE_MAX_M` | 貼路容忍距離上限（50m），超過不判定 |
| `MAP_MATCH_MIN_TRAVEL_M` | 連續性判定的累計移動距離（15m） |
| `MAP_MATCH_MAX_BEARING_DIFF_DEG` | 方向一致性夾角上限（45 度） |
| `MAP_MATCH_STATIONARY_SPEED_MPS` | 判定「靜止、跳過方向檢查」的速度閾值——**本次使用者提供的參數清單未包含此值，暫填一個遠低於一般步行速度的值，實作前需要重新確認** |
| `JUNCTION_ADJACENCY_M` | 路口鄰接距離（20m） |
| `PASSAGE_LOG_RETENTION_DAYS` | 路過紀錄保存天數（7 天） |
| `CORROBORATE_WINDOW_HOURS` | 佐證資格時間窗，依 category 查表（見 §8.3），依調校結果設定 |
| `DISMISS_WINDOW_HOURS` | 解除資格時間窗（不分類型），依調校結果設定 |
| `REMOTE_REPORT_LOCATION_CONFIDENCE` | 非現場回報的 `location_confidence` 預設值，依調校結果設定 |
| `PASSAGE_TO_FREQUENCY_ENABLED` | 路過紀錄→常走路段計數功能的總開關 |
| `FREQUENCY_DECAY_WEEKLY` | 常走路段計數的每週衰減係數，依調校結果設定 |
| `FREQUENCY_MIN_COUNT` | 衰減後低於此計數即刪除該筆紀錄，門檻依調校結果設定 |
| `FREQUENCY_DECAY_IDLE_DAYS` | 多久未更新才開始衰減（預設 7 天）；與 `PASSAGE_LOG_RETENTION_DAYS` 是獨立參數，不得互相替代 |
| `DAY_PERIODS` | passage_log 的粗略時段列舉（dawn/morning/afternoon/evening） |
| `DAY_PERIOD_LATEST_HOUR` | 每個時段的最晚可能時間，資格判定換算用（B8 決議） |
| `LAST_PING_VALIDITY_SECONDS` | 上一筆定位的有效期限，超過視為過期（B8 決議，300 秒） |

---

## 9. 防濫用頻率上限

> B7 實作（`src/engine/ratelimit.py`），B9 確認規則並補上查詢端點。

### 9.1 頻率上限

| 動作 | 上限 |
|---|---|
| 回報（report） | 每帳號每小時 5 則、每日 20 則 |
| 佐證與解除（corroborate：confirm／dismiss） | 每帳號每小時 10 次 |

### 9.2 超限處理

- 超過上限：警告。
- 持續超過：暫停該帳號的回報與佐證權限 24 小時。
- 暫停期間仍可瀏覽地圖、使用 SOS（暫停只限制「寫入」類動作，不影響安全相關的核心功能；SOS 尚未實作，但設計上不得被此機制阻擋）。
- 暫停狀態與解除時間記錄於 `user_abuse_flag`（`warned_at`、`suspended_until`），可透過 `GET /abuse_status`（B9 新增，需 `X-User-Id`）查詢目前是否暫停、暫停到何時。

### 9.3 config.py 參數

| 參數 | 說明 |
|---|---|
| `REPORT_RATE_LIMIT_HOURLY` | 回報每小時上限（5） |
| `REPORT_RATE_LIMIT_DAILY` | 回報每日上限（20） |
| `CORROBORATE_RATE_LIMIT_HOURLY` | 佐證／解除每小時上限（10） |
| `ABUSE_SUSPENSION_HOURS` | 持續超限後的暫停時數（24） |

---

## 10. M7：驗證用網頁地圖

`web/index.html`：MapLibre GL JS + OSM 圖磚（免費源），暗色主題。
- 路段依 `display_tier` 上色（low 灰／medium 橘／高 red）
- 點擊路段顯示風險數值與該路段 active 事件列表
- 上方輸入起訖點 → 呼叫 API 畫出兩條路線
- 時間軸滑桿：可拉動查看不同時間點的風險（驗證衰減是否正確）

**FastAPI 端點**
```
GET  /segments?bbox=...          → 路段幾何 + tier
GET  /segments/{id}/events       → 該路段事件
POST /reports                    → 新增回報（B7，含吸附、合併、頻率上限）
GET  /route?from=&to=&mode=safe|fast
POST /events/{id}/corroborate    → confirm / dismiss（B7，含路過驗證、頻率上限）
POST /location_pings             → 定位回報，供路過判定（B8，見 §8.0）
GET  /abuse_status                → 查詢目前帳號的防濫用暫停狀態（B9，見 §9.2）
```

---

## 11. 開發順序與驗收關卡

| 順序 | 模組 | 完成定義 |
|---|---|---|
| 1 | 環境＋schema.sql | 建表成功、PostGIS 可用 |
| 2 | M1 路網 | 文山區路段入庫、網頁能畫出來 |
| 3 | M2 合成資料 | 12 個月事件入庫、分布符合設定 |
| 4 | M3 衰減＋KDE＋合成 | 通過 §4.5 驗收 |
| 5 | M7 網頁地圖 | 能看到風險分布與時間軸 |
| 6 | M4 路線 | 通過 §5 驗收 |
| 7 | M5 adapter 重構 | 合成資料改由 adapter 進入，行為不變 |
| 8 | M6 LLM | 回報文字能轉出合格 ai_summary |

---

## 12. 硬性規則（不可違反）

1. **合成資料必須標記**：所有 log、輸出檔、API 回應中的模擬資料須帶 `"source": "synthetic"`。
2. **原文不出前端**：`raw_text` 僅存後端；任何 API 回應只回 `ai_summary`。
3. **90 天斷鏈**：清理任務每日執行，將 `created_at` 超過 90 天的 report 的 `user_id` 設為 null。
4. **不具名商家**：事件的位置描述只用路段名或公共設施，不得引用店名。
5. **不使用 Google 地圖服務**：圖磚、地理編碼、街景一律不接 Google API。
6. 所有可調參數（α、half_life、bandwidth、閾值）集中在 `config.py`，不散落在程式碼中。

---

## 13. 效能擴展路線（僅記錄，不實作）

本節只記錄已知的效能瓶頸與可能的擴展方向，供日後有實際負載數據時評估是否要做；
目前文山區規模（12,315 路段）下都還沒構成問題，不是現在要解決的事。

### 已知瓶頸點

| 瓶頸 | 現況 | 觸發條件 |
|---|---|---|
| 全區重算（`aggregate.run_aggregate_cycle`） | 排程每 5 分鐘跑一次，重算全部 12,315 條路段的 `segment_risk` | active 事件數或路段數大幅增加（例如擴大到其他行政區）時，單次重算時間拉長，可能追不上 5 分鐘週期 |
| KDE 計算（`baseline.py`） | 每個路段沿線以固定間距取樣多點，對每個取樣點做固定半徑的高斯核搜尋（參數見 §4.2），目前每日一次 | 歷史事件量（決定 KDE 輸入點數）或路網規模同時增長時，取樣點數 × 事件數的計算量會相乘成長 |
| 路線查詢（`safe_route.py`） | 每次請求即時用 networkx Dijkstra 在全區 MultiGraph 上算最短路徑，圖本身每次請求重建 | 路網規模變大、或併發請求數上升時，重複建圖與逐次 Dijkstra 的成本會上升 |

### 可能的擴展方向（僅記錄）

- **增量更新**：B7 已經把「單一路段 + 擴散範圍」的即時重算（`run_incremental_aggregate`）跟全區排程重算分開，這個方向可以再延伸——例如排程本身也只重算「自上次執行以來有變動」的路段，而不是每次都全部重來。
- **空間分區**：路網規模變大後，可以用行政區或網格把路段分區，重算、KDE、路線查詢都只在相關分區內進行，分區之間用邊界路段做銜接。
- **快取層**：`safe_route.py` 的圖目前每次請求都重建；可以考慮快取圖結構本身（只有風險值變動時才更新邊權重，拓樸不變不用重建），或對高頻起訖點配對做路線結果快取。

**原則**：這些都是等實際負載出現、真的量測到瓶頸之後再評估要不要做的方向，現階段不實作，避免過度工程化。

---

## 14. 通知佇列（B10）

> B10 實作（`src/engine/notifications.py`）。本階段只寫入 `notification_queue`，**不實際發送**——App 尚未開發，沒有接收裝置。

### 14.1 觸發時機

- 新事件建立時（任何 severity，實際是否入列依 14.3 的免費/付費規則判定）。
- 既有事件被升級為 `severe`（merge 後 severity 從非 severe 變成 severe）時；非 severe 之間的升級（例如 minor → moderate）不觸發。

### 14.2 對象判定

以 `user_last_ping`（§8 路過判定用的「最後一筆定位」暫存）的最後已知位置為準，計算事件位置 `NOTIFICATION_RADIUS_M`（200m）內的使用者。這個位置可能是使用者很久以前留下的——`user_last_ping` 本身沒有過期的概念對這裡適用（§8 的 `LAST_PING_VALIDITY_SECONDS` 只用在路過判定的連續性比對），沒有另外依新舊過濾。

### 14.3 免費與付費差異（D2）

- `severity = severe`：一律入列。
- `severity ∈ {minor, moderate}`：只有 `user_tier = premium` 的使用者入列。
- `user_tier` 存於新增的 `app_user` 表（`user_id, user_tier, created_at`，`user_tier` 預設 `free`）。系統目前沒有使用者註冊流程，`app_user` 只需要記錄「誰是 Premium」，沒有資料的使用者一律視為 `free`。

### 14.4 `notification_queue` 表

| 欄位 | 型別 | 說明 |
|---|---|---|
| id | UUID PK | |
| user_id | TEXT | |
| event_id | UUID FK | |
| segment_id | TEXT FK | 事件所在路段 |
| distance_m | DOUBLE PRECISION | 使用者最後位置與事件位置的距離 |
| severity | TEXT | minor / moderate / severe |
| created_at | TIMESTAMPTZ | |
| status | TEXT | pending / sent / skipped（本階段一律 pending，`sent`／`skipped` 保留給之後真正接上發送機制時使用） |

`UNIQUE(user_id, event_id)`：同一使用者對同一事件只入列一次（即使事件之後又升級一次 severity，也不會重複入列）。

### 14.5 config.py 參數

| 參數 | 說明 |
|---|---|
| `NOTIFICATION_RADIUS_M` | 通知對象的距離範圍（200m） |

---

## 15. 週期性工作的排程現況（僅記錄，本次不實作排程）

本節記錄截至目前為止，各週期性工作實際的執行方式。**結論：本專案目前沒有任何排程機制在運作**，所有週期性工作都是開發/測試過程中手動逐次呼叫。

### 15.1 現況

| 工作 | 程式進入點 | 目前實際執行方式 |
|---|---|---|
| decay + aggregate（§4.5，每 5 分鐘） | `src/engine/scheduler.py`（已寫好 APScheduler `BlockingScheduler` 邏輯，`run_five_minute_cycle()`） | **從未啟動**。開發過程中每次需要重算都是手動個別呼叫 `python -m src.engine.decay` 和 `python -m src.engine.aggregate`，不是透過 `scheduler.py` |
| baseline 重算（§4.2，每日） | `src/engine/scheduler.py`（`run_daily_baseline()`） | 同上，`scheduler.py` 從未啟動；baseline 只在需要時手動呼叫 `python -m src.engine.baseline` |
| passage_cleanup（§8.2，到期累加＋每週衰減） | `src/engine/passage_cleanup.py` | 完全手動（`python -m src.engine.passage_cleanup`），而且**沒有被整合進 `scheduler.py`**，是獨立於上面兩者之外的另一個腳本 |
| FastAPI 伺服器（`uvicorn`） | `src/api/main.py` | 開發/測試期間手動在前景或背景啟動，不是以系統服務（launchd／systemd／supervisor 等）常駐 |

已確認這台機器上沒有 crontab（`crontab -l` 無結果）、沒有對應的 launchd agent（`~/Library/LaunchAgents` 無 saferoute 相關項目）、也沒有任何 `src.engine.*` 的常駐行程在跑——目前唯一在跑的只有手動啟動的 `uvicorn` API 伺服器。

### 15.2 正式上線建議排程（僅記錄，不實作）

| 工作 | 建議頻率 | 建議方式 |
|---|---|---|
| decay + aggregate | 每 `SCHEDULER_INTERVAL_MINUTES`（5）分鐘 | `scheduler.py` 的邏輯已經寫好，正式上線可直接以常駐服務執行 `python -m src.engine.scheduler`（用 systemd/launchd/supervisor 等 process manager 包起來，確保掛掉會自動重啟） |
| baseline 重算 | 每 `BASELINE_RECOMPUTE_INTERVAL_HOURS`（24）小時 | 同上，已經是 `scheduler.py` 常駐程序裡的第二個 job，不需要獨立排程 |
| passage_cleanup | 建議每日一次 | 目前沒有整合進 `scheduler.py`，需要另外用 cron 或把它加進 `scheduler.py` 的 job 清單；頻率沒有強制規定，但因為到期判斷（`PASSAGE_LOG_RETENTION_DAYS`）與衰減判斷（`FREQUENCY_DECAY_IDLE_DAYS`）都是以「天」為單位，每日一次足以及時處理到期資料，不需要跟 5 分鐘那組一樣頻繁 |
| FastAPI 伺服器 | 常駐 | 用 process manager 常駐執行，掛掉自動重啟；不屬於週期性工作，但同樣目前只是手動啟動，一併記錄 |
