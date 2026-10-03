-- SafeRoute 風險引擎資料庫 Schema
-- 座標系統：儲存用 EPSG:4326 (WGS84)；距離計算用 EPSG:3826 (TWD97 台灣二度分帶)
-- 對應 SPEC.md §1

CREATE EXTENSION IF NOT EXISTS postgis;

-- =====================================================
-- 1.1 road_segment 路段
-- =====================================================
CREATE TABLE road_segment (
    segment_id          TEXT PRIMARY KEY,          -- OSM way id，同 way 分段時加後綴 _1, _2
    name                TEXT,
    geometry            GEOMETRY(LineString, 4326) NOT NULL,
    length_m            DOUBLE PRECISION NOT NULL,
    road_type           TEXT NOT NULL CHECK (road_type IN (
                            'primary', 'secondary', 'tertiary', 'residential',
                            'alley', 'service_alley', 'service_access',
                            'footway', 'path', 'steps'
                        )),
    streetlight_count   INTEGER,                   -- 預設 null
    baseline_risk       DOUBLE PRECISION NOT NULL DEFAULT 0
                            CHECK (baseline_risk BETWEEN 0 AND 1),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_road_segment_geometry ON road_segment USING GIST (geometry);

-- =====================================================
-- 1.2 road_node 路口（路線計算用）
-- =====================================================
CREATE TABLE road_node (
    node_id     TEXT PRIMARY KEY,                  -- OSM node id
    geometry    GEOMETRY(Point, 4326) NOT NULL
);

CREATE INDEX idx_road_node_geometry ON road_node USING GIST (geometry);

-- =====================================================
-- 1.3 segment_topology 路段連接關係
-- =====================================================
CREATE TABLE segment_topology (
    segment_id  TEXT PRIMARY KEY REFERENCES road_segment(segment_id),
    from_node   TEXT NOT NULL REFERENCES road_node(node_id),
    to_node     TEXT NOT NULL REFERENCES road_node(node_id),
    is_oneway   BOOLEAN NOT NULL DEFAULT false      -- 步行預設 false
);

CREATE INDEX idx_segment_topology_from_node ON segment_topology (from_node);
CREATE INDEX idx_segment_topology_to_node ON segment_topology (to_node);

-- =====================================================
-- 1.5 event 事件（先建表，report 會 FK 到這裡）
-- =====================================================
CREATE TABLE event (
    event_id            UUID PRIMARY KEY,
    category            TEXT NOT NULL CHECK (category IN (
                            'personal_safety', 'streetlight', 'obstruction',
                            'traffic_accident', 'construction', 'crowd', 'other'
                        )),
    severity            TEXT NOT NULL CHECK (severity IN ('minor', 'moderate', 'severe')),
    segment_id          TEXT REFERENCES road_segment(segment_id),
    affected_segments   TEXT[],
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                            'active', 'resolved', 'expired', 'repaired'
                        )),
    report_count        INTEGER NOT NULL DEFAULT 1,
    confirm_count       INTEGER NOT NULL DEFAULT 0,    -- B7：confirm 不計入 report_count，分開儲存
    dismiss_count       INTEGER NOT NULL DEFAULT 0,
    first_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ,                -- 環境類為 null
    current_risk        DOUBLE PRECISION NOT NULL DEFAULT 0
                            CHECK (current_risk BETWEEN 0 AND 1),
    repaired_at         TIMESTAMPTZ                 -- 環境類專用
);

CREATE INDEX idx_event_segment_id ON event (segment_id);
CREATE INDEX idx_event_status ON event (status);

-- =====================================================
-- 1.4 report 原始回報
-- =====================================================
CREATE TABLE report (
    report_id           UUID PRIMARY KEY,
    user_id             TEXT,                       -- 90 天後由清理任務設為 null
    display_code        TEXT NOT NULL,              -- 每筆隨機 4 碼，前端顯示用
    category            TEXT NOT NULL CHECK (category IN (
                            'personal_safety', 'streetlight', 'obstruction',
                            'traffic_accident', 'construction', 'crowd', 'other'
                        )),
    tags                TEXT[] CHECK (
                            tags IS NULL OR tags <@ ARRAY[
                                'following', 'harassment', 'filming', 'exposure', 'other'
                            ]
                        ),
    severity            TEXT NOT NULL CHECK (severity IN ('minor', 'moderate', 'severe')),
    raw_text            TEXT,                       -- 用戶原文，僅後端
    ai_summary          TEXT,                       -- 前端顯示版（M6 前留空）
    location            GEOMETRY(Point, 4326) NOT NULL,
    segment_id          TEXT REFERENCES road_segment(segment_id),  -- 吸附結果
    location_confidence DOUBLE PRECISION CHECK (location_confidence BETWEEN 0 AND 1),
    text_confidence     DOUBLE PRECISION NOT NULL DEFAULT 1.0
                            CHECK (text_confidence BETWEEN 0 AND 1),  -- M6 前預設 1.0
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_id            UUID REFERENCES event(event_id),  -- 合併後歸屬
    source              TEXT NOT NULL CHECK (source IN ('user', 'synthetic', 'official'))
);

CREATE INDEX idx_report_location ON report USING GIST (location);
CREATE INDEX idx_report_segment_id ON report (segment_id);
CREATE INDEX idx_report_event_id ON report (event_id);
CREATE INDEX idx_report_created_at ON report (created_at);

-- =====================================================
-- 1.6 corroboration 佐證
-- =====================================================
CREATE TABLE corroboration (
    id          UUID PRIMARY KEY,
    event_id    UUID NOT NULL REFERENCES event(event_id),
    user_id     TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('confirm', 'dismiss')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, user_id)                      -- 一人一事件限一次
);

-- =====================================================
-- 1.7 segment_risk 引擎輸出
-- =====================================================
CREATE TABLE segment_risk (
    segment_id      TEXT PRIMARY KEY REFERENCES road_segment(segment_id),
    realtime_risk   DOUBLE PRECISION NOT NULL DEFAULT 0
                        CHECK (realtime_risk BETWEEN 0 AND 1),
    baseline_risk   DOUBLE PRECISION NOT NULL DEFAULT 0
                        CHECK (baseline_risk BETWEEN 0 AND 1),
    total_risk      DOUBLE PRECISION NOT NULL DEFAULT 0
                        CHECK (total_risk BETWEEN 0 AND 1),
    display_tier    TEXT NOT NULL DEFAULT 'low' CHECK (display_tier IN ('low', 'medium', 'high')),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================
-- M7：poi 地標（供 /search 搜尋索引使用，SPEC.md §9）
-- 學校、捷運站、公園、超商等 OSM amenity/POI，非 §1 原始規格表，M7 新增。
-- =====================================================
CREATE TABLE poi (
    poi_id      TEXT PRIMARY KEY,          -- OSM node/way id
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,             -- school/university/hospital/station/park/convenience 等
    geometry    GEOMETRY(Point, 4326) NOT NULL
);

CREATE INDEX idx_poi_geometry ON poi USING GIST (geometry);

-- =====================================================
-- B7：防濫用頻率上限狀態（SPEC.md §9，非 §1 原始規格表，B7 新增）
-- 警告/暫停狀態機：report/corroborate 各自超過頻率上限時先記一次警告
-- （warned_at），若在同一週期內持續超過才真正暫停（suspended_until）。
-- 實際的次數統計直接查 report/corroboration 表的時間窗，不在這裡重複累計。
-- =====================================================
CREATE TABLE user_abuse_flag (
    user_id         TEXT PRIMARY KEY,
    warned_at       TIMESTAMPTZ,
    suspended_until TIMESTAMPTZ
);

-- =====================================================
-- B8：路過驗證（SPEC.md §8，非 §1 原始規格表，B8 新增）
-- =====================================================

-- 路過紀錄：僅存粗略時段，不存座標、不存精確時間戳（§8.2 資料最小化）。
-- created_at 只供清理任務判斷是否滿 7 天用，不是使用者行蹤時間戳，不對外暴露。
CREATE TABLE passage_log (
    user_id        TEXT NOT NULL,
    segment_id     TEXT NOT NULL REFERENCES road_segment(segment_id),
    occurred_date  DATE NOT NULL,
    day_period     TEXT NOT NULL CHECK (day_period IN ('dawn', 'morning', 'afternoon', 'evening')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, segment_id, occurred_date, day_period)
);

CREATE INDEX idx_passage_log_created_at ON passage_log (created_at);

-- 常走路段計數：路過紀錄到期後累加於此，個人化功能只讀這張表，不讀 passage_log。
-- count 用 DOUBLE PRECISION 是因為每週衰減（×FREQUENCY_DECAY_WEEKLY）會產生小數。
CREATE TABLE user_segment_frequency (
    user_id      TEXT NOT NULL,
    segment_id   TEXT NOT NULL REFERENCES road_segment(segment_id),
    count        DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, segment_id)
);

-- 「上一筆定位」暫存，供連續性／方向判定比對用。每次定位進來就覆寫同一筆，
-- 不是歷史紀錄，不受 §8.2 路過紀錄保存規範約束。
CREATE TABLE user_last_ping (
    user_id              TEXT PRIMARY KEY,
    segment_id           TEXT REFERENCES road_segment(segment_id),
    lon                  DOUBLE PRECISION NOT NULL,
    lat                  DOUBLE PRECISION NOT NULL,
    cumulative_travel_m  DOUBLE PRECISION NOT NULL DEFAULT 0,
    received_at          TIMESTAMPTZ NOT NULL
);

-- =====================================================
-- B10：使用者方案與通知佇列（非 §1 原始規格表，B10 新增）
-- =====================================================

-- 目前系統沒有使用者註冊流程，user_id 都是 X-User-Id 標頭的任意字串。這張表
-- 只用來記錄「誰是 Premium」，沒有資料的使用者一律視為 free（預設），不需要
-- 預先幫每個使用者建一筆紀錄。
CREATE TABLE app_user (
    user_id     TEXT PRIMARY KEY,
    user_tier   TEXT NOT NULL DEFAULT 'free' CHECK (user_tier IN ('free', 'premium')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 通知佇列：本階段只寫入、不實際發送（App 尚未開發，無接收裝置）。
CREATE TABLE notification_queue (
    id          UUID PRIMARY KEY,
    user_id     TEXT NOT NULL,
    event_id    UUID NOT NULL REFERENCES event(event_id),
    segment_id  TEXT NOT NULL REFERENCES road_segment(segment_id),
    distance_m  DOUBLE PRECISION NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('minor', 'moderate', 'severe')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped')),
    UNIQUE (user_id, event_id)  -- 同一使用者對同一事件只入列一次
);

CREATE INDEX idx_notification_queue_user_id ON notification_queue (user_id);
CREATE INDEX idx_notification_queue_status ON notification_queue (status);
