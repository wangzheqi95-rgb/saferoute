"""M7+B7：驗證用網頁地圖與事件/回報 API（SPEC.md §9）。

GET  /segments?bbox=&as_of=          路段幾何 + tier
GET  /segments/{id}/events?as_of=    該路段事件
GET  /route?from=&to=&mode=safe|fast&as_of=   路線（單次呼叫回傳快/安心兩條 + 比較指標）
GET  /search?q=                      本地搜尋（road_segment.name + poi.name）
POST /reports                        新增回報（B7）
POST /events/{id}/corroborate        佐證／解除（B7）

使用者身分：測試階段用 X-User-Id 標頭，取得邏輯收斂在 get_user_id() 一個函式，
正式版換 Firebase 驗證只需要改這裡。

所有合成資料一律標記 "source": "synthetic"（SPEC.md §10 第 1 點）。
"""
import json
import secrets
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import UUID

import numpy as np
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from scipy.spatial import cKDTree

psycopg2.extras.register_uuid()

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection
from src.engine import aggregate, baseline, decay, merge, notifications, passage, ratelimit
from src.engine.decay import SEVERITY_RANK
from src.routing import safe_route

app = FastAPI(title="SafeRoute 風險引擎驗證 API")

TAIPEI = timezone(timedelta(hours=8))
WEB_DIR = Path(__file__).resolve().parents[2] / "web"

_baseline_cache: dict[str, dict] = {}
_node_tree_cache: dict = {}


def parse_as_of(as_of_str: str | None) -> datetime:
    if not as_of_str:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(as_of_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt


def get_user_id(request: Request) -> str:
    """測試階段：讀 X-User-Id 標頭，驗證存在即可，不做認證。正式版換 Firebase
    驗證時，只需要改這個函式（例如改成解析 Authorization: Bearer <id token>），
    呼叫端（下面所有端點）都不用動。
    """
    user_id = request.headers.get("X-User-Id")
    if not user_id:
        raise HTTPException(status_code=401, detail="缺少 X-User-Id 標頭")
    return user_id


def get_cached_baseline(conn, as_of: datetime) -> dict:
    """baseline 以「as_of 所在的台北時間日期」為 key 快取，同一天內不重算。"""
    date_key = as_of.astimezone(TAIPEI).date().isoformat()
    if not config.BASELINE_SNAPSHOT_CACHE_ENABLED or date_key not in _baseline_cache:
        _baseline_cache[date_key] = baseline.compute_snapshot(conn, as_of)
    return _baseline_cache[date_key]


def get_segment_risk_snapshot(conn, as_of: datetime):
    baseline_map = get_cached_baseline(conn, as_of)
    return aggregate.compute_snapshot(conn, as_of, baseline_map=baseline_map)


def get_node_tree(conn):
    """快取 road_node 的 KDTree（EPSG:3826），供起訖點吸附與服務範圍判定使用。"""
    if "tree" in _node_tree_cache:
        return _node_tree_cache["tree"], _node_tree_cache["node_ids"]

    cur = conn.cursor()
    cur.execute("SELECT node_id, ST_X(ST_Transform(geometry,3826)), ST_Y(ST_Transform(geometry,3826)) FROM road_node")
    rows = cur.fetchall()
    node_ids = [r[0] for r in rows]
    coords = np.array([[r[1], r[2]] for r in rows])
    tree = cKDTree(coords)
    _node_tree_cache["tree"] = tree
    _node_tree_cache["node_ids"] = node_ids
    _node_tree_cache["coords"] = coords
    return tree, node_ids


def snap_to_nearest_node(conn, lon: float, lat: float):
    """回傳 (node_id, distance_m)。座標先轉 3826 再查 KDTree。"""
    cur = conn.cursor()
    cur.execute("SELECT ST_X(t), ST_Y(t) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s,%s),4326),3826) AS t) s", (lon, lat))
    x, y = cur.fetchone()
    tree, node_ids = get_node_tree(conn)
    dist, idx = tree.query([x, y])
    return node_ids[idx], float(dist)


def snap_to_segment(conn, lon: float, lat: float):
    """回傳 (segment_id, distance_m)：吸附到最近的路段（不是路口），用 PostGIS
    的 KNN 運算子 <-> 走 GIST 索引，不用另外建 KDTree。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT segment_id,
               ST_Distance(geography(geometry), geography(ST_SetSRID(ST_MakePoint(%s,%s),4326)))
        FROM road_segment
        ORDER BY geometry <-> ST_SetSRID(ST_MakePoint(%s,%s),4326)
        LIMIT 1
        """,
        (lon, lat, lon, lat),
    )
    segment_id, dist = cur.fetchone()
    return segment_id, float(dist)


def accuracy_to_location_confidence(accuracy_m: float) -> float:
    """GPS 精度（公尺）轉 location_confidence（B7 決議）：<= ACCURACY_CONFIDENCE_MIN_M
    給滿分 1.0，>= ACCURACY_CONFIDENCE_MAX_M 給下限 ACCURACY_CONFIDENCE_FLOOR，中間
    線性內插。
    """
    lo, hi = config.ACCURACY_CONFIDENCE_MIN_M, config.ACCURACY_CONFIDENCE_MAX_M
    floor = config.ACCURACY_CONFIDENCE_FLOOR
    if accuracy_m <= lo:
        return 1.0
    if accuracy_m >= hi:
        return floor
    return 1.0 - (accuracy_m - lo) / (hi - lo) * (1.0 - floor)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/segments")
def get_segments(bbox: str | None = Query(None), as_of: str | None = Query(None)):
    as_of_dt = parse_as_of(as_of)
    conn = get_connection()
    try:
        snapshot, _, active_event_count = get_segment_risk_snapshot(conn, as_of_dt)
        tier_counts = {"low": 0, "medium": 0, "high": 0}
        for v in snapshot.values():
            tier_counts[v["display_tier"]] += 1

        cur = conn.cursor()
        if bbox:
            minlon, minlat, maxlon, maxlat = map(float, bbox.split(","))
            cur.execute(
                """
                SELECT segment_id, name, road_type, ST_AsGeoJSON(geometry)
                FROM road_segment
                WHERE geometry && ST_MakeEnvelope(%s,%s,%s,%s,4326)
                """,
                (minlon, minlat, maxlon, maxlat),
            )
        else:
            cur.execute("SELECT segment_id, name, road_type, ST_AsGeoJSON(geometry) FROM road_segment")

        features = []
        for segment_id, name, road_type, geom_json in cur.fetchall():
            risk = snapshot.get(segment_id, {"realtime_risk": 0.0, "baseline_risk": 0.0, "total_risk": 0.0, "display_tier": "low"})
            features.append({
                "type": "Feature",
                "geometry": json.loads(geom_json),
                "properties": {
                    "segment_id": segment_id,
                    "name": name,
                    "road_type": road_type,
                    "realtime_risk": risk["realtime_risk"],
                    "baseline_risk": risk["baseline_risk"],
                    "total_risk": risk["total_risk"],
                    "display_tier": risk["display_tier"],
                },
            })

        return JSONResponse({
            "type": "FeatureCollection",
            "as_of": as_of_dt.isoformat(),
            "source": "synthetic",
            "active_event_count": active_event_count,
            "tier_counts": tier_counts,
            "features": features,
        })
    finally:
        conn.close()


@app.get("/segments/{segment_id}/events")
def get_segment_events(segment_id: str, as_of: str | None = Query(None)):
    as_of_dt = parse_as_of(as_of)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT segment_id FROM road_segment WHERE segment_id = %s", (segment_id,)
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="segment not found")

        cur.execute(
            """
            SELECT e.event_id, e.category, e.severity, e.first_at, e.last_at,
                   e.report_count, e.confirm_count,
                   (SELECT source FROM report WHERE report.event_id = e.event_id LIMIT 1) AS source
            FROM event e WHERE e.segment_id = %s
            """,
            (segment_id,),
        )
        rows = cur.fetchall()

        events = []
        for event_id, category, severity, first_at, last_at, report_count, confirm_count, source in rows:
            status, current_risk = decay.status_and_risk_at(
                category, severity, first_at, last_at, as_of_dt
            )
            if status != "active":
                continue
            events.append({
                "event_id": str(event_id),
                "category": category,
                "severity": severity,
                "report_count": report_count,
                "confirm_count": confirm_count,
                "current_risk": current_risk,
                "source": source or "synthetic",
            })

        return JSONResponse({"segment_id": segment_id, "as_of": as_of_dt.isoformat(), "events": events})
    finally:
        conn.close()


def _route_payload(route: dict) -> dict:
    return {
        "length_m": route["length_m"],
        "walk_time_min": route["walk_time_min"],
        "avg_risk": route["avg_risk"],
        "max_risk": route["max_risk"],
        "max_tier": route["max_tier"],
        "high_length_m": route["high_length_m"],
        "medium_plus_length_m": route["medium_plus_length_m"],
        "segments": route["segments"],
        "tiers": route["tiers"],
        "node_path": route["node_path"],
    }


@app.get("/route")
def get_route(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    mode: str = Query("safe", pattern="^(safe|fast)$"),
    as_of: str | None = Query(None),
):
    as_of_dt = parse_as_of(as_of)
    conn = get_connection()
    try:
        from_lon, from_lat = map(float, from_.split(","))
        to_lon, to_lat = map(float, to.split(","))

        source_node, source_dist = snap_to_nearest_node(conn, from_lon, from_lat)
        target_node, target_dist = snap_to_nearest_node(conn, to_lon, to_lat)

        if source_dist > config.SERVICE_AREA_SNAP_TOLERANCE_M:
            raise HTTPException(status_code=400, detail="起點不在服務範圍內")
        if target_dist > config.SERVICE_AREA_SNAP_TOLERANCE_M:
            raise HTTPException(status_code=400, detail="終點不在服務範圍內")

        snapshot, _, _ = get_segment_risk_snapshot(conn, as_of_dt)
        segment_risk_map = {
            seg_id: {"total_risk": v["total_risk"], "display_tier": v["display_tier"]}
            for seg_id, v in snapshot.items()
        }
        G = safe_route.build_graph(conn, segment_risk_map=segment_risk_map)

        result = safe_route.compare_routes(G, source_node, target_node, config.ROUTE_ALPHA_SAFE)

        return JSONResponse({
            "as_of": as_of_dt.isoformat(),
            "mode_requested": mode,
            "source": "synthetic",
            "fast": _route_payload(result["fast"]),
            "safe": _route_payload(result["safe"]),
            "overlap_pct": result["overlap_pct"],
            "detour_pct": result["detour_pct"],
            "risk_reduction_pct": result["risk_reduction_pct"],
            "note": result["note"],
        })
    finally:
        conn.close()


@app.get("/search")
def search(q: str = Query(..., min_length=1)):
    conn = get_connection()
    try:
        cur = conn.cursor()
        pattern = f"%{q}%"
        cur.execute(
            """
            SELECT segment_id AS id, name, road_type AS category, 'segment' AS kind,
                   ST_X(ST_Centroid(geometry)), ST_Y(ST_Centroid(geometry))
            FROM road_segment WHERE name ILIKE %s
            LIMIT 10
            """,
            (pattern,),
        )
        segment_rows = cur.fetchall()

        cur.execute(
            """
            SELECT poi_id AS id, name, category, 'poi' AS kind,
                   ST_X(geometry), ST_Y(geometry)
            FROM poi WHERE name ILIKE %s
            LIMIT 10
            """,
            (pattern,),
        )
        poi_rows = cur.fetchall()

        results = []
        for id_, name, category, kind, lon, lat in list(segment_rows) + list(poi_rows):
            results.append({"id": id_, "name": name, "category": category, "kind": kind, "lon": lon, "lat": lat})

        return JSONResponse({"query": q, "results": results})
    finally:
        conn.close()


VALID_CATEGORIES = [
    "personal_safety", "streetlight", "obstruction",
    "traffic_accident", "construction", "crowd", "other",
]
VALID_SEVERITIES = ["minor", "moderate", "severe"]
VALID_TAGS = ["following", "harassment", "filming", "exposure", "other"]

_merge_decider = merge.RuleBasedMergeDecider()
_passage_verifier = passage.MapMatchPassageVerifier()


class ReportIn(BaseModel):
    category: str
    tags: list[str] | None = None
    severity: str
    raw_text: str | None = None
    location: list[float]  # [lon, lat]
    accuracy_m: float


class CorroborateIn(BaseModel):
    action: str  # confirm | dismiss


class LocationPingIn(BaseModel):
    """B8：裝置端定位回報（SPEC.md §8.1 比對用）。"""
    location: list[float]  # [lon, lat]
    accuracy_m: float
    speed_mps: float | None = None
    bearing_deg: float | None = None


def _random_display_code() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(4))


@app.post("/reports")
def create_report(body: ReportIn, request: Request):
    """B7：新增回報。吸附路段 → 事件合併判定（粗篩 find_candidate_events + 規則層
    RuleBasedMergeDecider）→ 寫入 report → 更新或建立 event → 立即重算受影響路段。
    """
    user_id = get_user_id(request)

    if body.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"invalid category: {body.category}")
    if body.severity not in VALID_SEVERITIES:
        raise HTTPException(status_code=400, detail=f"invalid severity: {body.severity}")
    for t in (body.tags or []):
        if t not in VALID_TAGS:
            raise HTTPException(status_code=400, detail=f"invalid tag: {t}")
    if len(body.location) != 2:
        raise HTTPException(status_code=400, detail="location must be [lon, lat]")
    lon, lat = body.location

    conn = get_connection()
    try:
        try:
            ratelimit.check_and_record(conn, user_id, "report")
        except ratelimit.RateLimitExceeded as e:
            conn.commit()  # 保留警告/暫停狀態的寫入
            raise HTTPException(status_code=429, detail=e.reason)

        segment_id, snap_dist = snap_to_segment(conn, lon, lat)
        if snap_dist > config.SNAP_MAX_DISTANCE_M:
            raise HTTPException(
                status_code=400,
                detail=f"回報位置距離最近路段 {snap_dist:.0f} 公尺，超過可接受範圍（{config.SNAP_MAX_DISTANCE_M} 公尺），請確認定位或靠近道路後再回報",
            )
        location_confidence = accuracy_to_location_confidence(body.accuracy_m)
        now = datetime.now(timezone.utc)
        location_wkt = f"POINT({lon} {lat})"

        candidates = merge.find_candidate_events(conn, body.category, segment_id, location_wkt, now)
        event_id = _merge_decider.decide(candidates, {"category": body.category, "raw_text": body.raw_text})
        merged = event_id is not None

        cur = conn.cursor()
        notify_severity = None  # B10：新事件一律檢查、既有事件只有「升級為 severe」才檢查
        if merged:
            cur.execute("SELECT severity FROM event WHERE event_id = %s", (event_id,))
            existing_severity = cur.fetchone()[0]
            new_severity = merge.max_severity(existing_severity, body.severity)
            cur.execute(
                "UPDATE event SET severity = %s, last_at = %s WHERE event_id = %s",
                (new_severity, now, event_id),
            )
            if new_severity == "severe" and existing_severity != "severe":
                notify_severity = "severe"
        else:
            event_id = uuid.uuid4()
            cur.execute(
                """INSERT INTO event (event_id, category, severity, segment_id, status,
                   report_count, dismiss_count, first_at, last_at, current_risk)
                   VALUES (%s,%s,%s,%s,'active',0,0,%s,%s,0.0)""",
                (event_id, body.category, body.severity, segment_id, now, now),
            )
            notify_severity = body.severity

        report_id = uuid.uuid4()
        cur.execute(
            """INSERT INTO report
               (report_id, user_id, display_code, category, tags, severity, raw_text,
                ai_summary, location, segment_id, location_confidence, created_at,
                event_id, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,
                       ST_SetSRID(ST_GeomFromText(%s),4326),%s,%s,%s,%s,'user')""",
            (report_id, user_id, _random_display_code(), body.category, body.tags, body.severity,
             body.raw_text, location_wkt, segment_id, location_confidence, now, event_id),
        )

        # report_count = COUNT(DISTINCT user_id)：同一 user_id 對同一 event 的重複回報只計 1 次
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM report WHERE event_id = %s", (event_id,))
        report_count = cur.fetchone()[0]

        cur.execute("SELECT category, severity, last_at FROM event WHERE event_id = %s", (event_id,))
        cat, sev, last_at = cur.fetchone()
        current_risk = decay.compute_current_risk(cat, sev, last_at, now)
        expires_at = decay.compute_expires_at(cat, sev, last_at)  # 新 report 進來，衰減時鐘重置
        cur.execute(
            "UPDATE event SET report_count = %s, current_risk = %s, expires_at = %s WHERE event_id = %s",
            (report_count, current_risk, expires_at, event_id),
        )

        tiers = aggregate.run_incremental_aggregate(conn, segment_id, triggering_event_id=event_id)

        if notify_severity is not None:
            notifications.enqueue_for_event(conn, event_id, segment_id, notify_severity, lon, lat, now)

        conn.commit()

        return JSONResponse({
            "report_id": str(report_id),
            "event_id": str(event_id),
            "merged": merged,
            "segment_id": segment_id,
            "display_tier": tiers.get(segment_id, "low"),
        })
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/location_pings")
def submit_location_ping(body: LocationPingIn, request: Request):
    """B8：裝置端定位回報（SPEC.md §8.1，SPEC 原規格遺漏此端點，B8 補上）。
    不需要持續導航中才能呼叫；每次呼叫都是一次獨立的路過判定機會，判定邏輯見
    `src.engine.passage.record_ping()`。不做頻率上限（跟 report/corroborate 的
    濫用風險不同：定位回報本身不影響任何事件計數或顯示內容，只會累積路過紀錄）。
    """
    user_id = get_user_id(request)
    if len(body.location) != 2:
        raise HTTPException(status_code=400, detail="location must be [lon, lat]")
    lon, lat = body.location

    conn = get_connection()
    try:
        now = datetime.now(timezone.utc)
        result = passage.record_ping(conn, user_id, lon, lat, body.accuracy_m, body.speed_mps, body.bearing_deg, now)
        conn.commit()
        return JSONResponse(result)
    finally:
        conn.close()


@app.post("/events/{event_id}/corroborate")
def corroborate_event(event_id: UUID, body: CorroborateIn, request: Request):
    """B7：佐證／解除。檢查一人一事件限一次（DB 唯一約束）→ 檢查路過驗證資格
    （B8 未實作，一律回絕，見 src/engine/passage.py）→ 寫入 corroboration →
    更新 confirm_count（confirm）或 dismiss_count（dismiss，不影響 report_count）→
    立即重算受影響路段。
    """
    user_id = get_user_id(request)
    if body.action not in ("confirm", "dismiss"):
        raise HTTPException(status_code=400, detail="action must be confirm or dismiss")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, severity, segment_id, status, first_at, last_at, dismiss_count FROM event WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="event not found")
        category, severity, segment_id, status, first_at, last_at, dismiss_count = row

        try:
            ratelimit.check_and_record(conn, user_id, "corroborate")
        except ratelimit.RateLimitExceeded as e:
            conn.commit()
            raise HTTPException(status_code=429, detail=e.reason)

        now = datetime.now(timezone.utc)

        eligible, reason = _passage_verifier.check(conn, user_id, segment_id, category, body.action, now)
        if not eligible:
            conn.commit()  # 保留上面頻率限制可能寫入的狀態
            return JSONResponse(status_code=403, content={"eligible": False, "reason": reason})

        try:
            cur.execute(
                "INSERT INTO corroboration (id, event_id, user_id, action, created_at) VALUES (%s,%s,%s,%s,%s)",
                (uuid.uuid4(), event_id, user_id, body.action, now),
            )
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            raise HTTPException(status_code=409, detail="一人一事件限一次，這個事件你已經回應過")

        if body.action == "dismiss":
            cur.execute(
                "SELECT COUNT(DISTINCT user_id) FROM corroboration WHERE event_id = %s AND action = 'dismiss'",
                (event_id,),
            )
            dismiss_count = cur.fetchone()[0]
            new_status = decay.compute_status(category, severity, last_at, now, status, dismiss_count)
            cur.execute(
                "UPDATE event SET dismiss_count = %s, status = %s WHERE event_id = %s",
                (dismiss_count, new_status, event_id),
            )
            status = new_status
        else:  # confirm（B7 決議）：不計入 report_count，另外存在 confirm_count，
            # 兩者分開儲存與顯示；tier 解封頂判定改看 report_count + confirm_count
            cur.execute(
                "SELECT COUNT(DISTINCT user_id) FROM corroboration WHERE event_id = %s AND action = 'confirm'",
                (event_id,),
            )
            confirm_count = cur.fetchone()[0]
            cur.execute("UPDATE event SET confirm_count = %s WHERE event_id = %s", (confirm_count, event_id))

        tiers = aggregate.run_incremental_aggregate(conn, segment_id, triggering_event_id=str(event_id))
        conn.commit()

        cur.execute(
            "SELECT report_count, confirm_count, dismiss_count, status FROM event WHERE event_id = %s",
            (event_id,),
        )
        report_count, confirm_count, dismiss_count, status = cur.fetchone()

        return JSONResponse({
            "event_id": str(event_id),
            "action": body.action,
            "report_count": report_count,
            "confirm_count": confirm_count,
            "dismiss_count": dismiss_count,
            "status": status,
            "segment_id": segment_id,
            "display_tier": tiers.get(segment_id, "low"),
        })
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/event_markers")
def event_markers(as_of: str | None = Query(None)):
    """地圖事件標記（圓形底+白色圖示，SPEC.md §9 視覺修正）。不在原始四端點清單內，
    是為了實作「地圖上的事件標記」這個新要求另外加的，見 M7 報告說明。
    顏色規則：環境類（streetlight/obstruction/construction）不分等級一律 EVENT_MARKER_COLOR_AMBER；
    其餘類別 severity=severe 用 EVENT_MARKER_COLOR_HIGH，否則用 EVENT_MARKER_COLOR_AMBER。

    同一路段（segment_id）多個 active 事件合併為一個標記：取「最嚴重」的事件（依
    color 優先 HIGH > AMBER，同色再依 severity）決定代表的圖示/顏色，`count` 記錄
    該路段實際事件數。「最嚴重」以外的合併規則（例如同色時選哪個類別當代表）是
    實作上的簡化，非規格明訂。
    """
    as_of_dt = parse_as_of(as_of)
    conn = get_connection()
    try:
        raw = aggregate.get_active_events_at(conn, as_of_dt)
        for m in raw:
            if decay.is_environmental(m["category"]):
                m["color"] = config.EVENT_MARKER_COLOR_AMBER
            else:
                m["color"] = (
                    config.EVENT_MARKER_COLOR_HIGH if m["severity"] == "severe"
                    else config.EVENT_MARKER_COLOR_AMBER
                )
            m["icon"] = config.EVENT_CATEGORY_ICON.get(m["category"], "ellipsis")

        by_segment = {}
        for m in raw:
            by_segment.setdefault(m["segment_id"], []).append(m)

        def rank(m):
            color_rank = 1 if m["color"] == config.EVENT_MARKER_COLOR_HIGH else 0
            severity_rank = SEVERITY_RANK.get(m["severity"], 0)
            priority_list = config.EVENT_MARKER_CATEGORY_PRIORITY
            idx = priority_list.index(m["category"]) if m["category"] in priority_list else len(priority_list)
            category_rank = len(priority_list) - idx  # 數字越大越優先
            return (color_rank, severity_rank, category_rank)

        markers = []
        for segment_id, group in by_segment.items():
            best = max(group, key=rank)
            markers.append({
                "event_id": best["event_id"],
                "category": best["category"],
                "severity": best["severity"],
                "segment_id": segment_id,
                "lon": best["lon"],
                "lat": best["lat"],
                "color": best["color"],
                "icon": best["icon"],
                "count": len(group),
            })

        return JSONResponse({"as_of": as_of_dt.isoformat(), "source": "synthetic", "markers": markers})
    finally:
        conn.close()


@app.get("/default_start")
def default_start():
    """服務範圍外時的預設起點（國立政治大學），座標從 poi 表查，不寫死。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, ST_X(geometry), ST_Y(geometry) FROM poi WHERE name = %s LIMIT 1",
            (config.DEFAULT_START_POI_NAME_HINT,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="default start POI not found")
        name, lon, lat = row
        return JSONResponse({"name": name, "lon": lon, "lat": lat})
    finally:
        conn.close()


@app.get("/service_area_check")
def service_area_check(lon: float = Query(...), lat: float = Query(...)):
    conn = get_connection()
    try:
        _, dist = snap_to_nearest_node(conn, lon, lat)
        within = dist <= config.SERVICE_AREA_SNAP_TOLERANCE_M
        return JSONResponse({"within_service_area": within, "nearest_node_distance_m": dist})
    finally:
        conn.close()


@app.get("/abuse_status")
def get_abuse_status(request: Request):
    """B9：查詢目前使用者的防濫用暫停狀態（SPEC.md §9）。暫停只限制 report／
    corroborate 這兩個寫入端點，本端點本身與其他 GET 端點、SOS 一律不受影響。
    """
    user_id = get_user_id(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT warned_at, suspended_until FROM user_abuse_flag WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        warned_at, suspended_until = row if row else (None, None)
        now = datetime.now(timezone.utc)
        suspended = suspended_until is not None and suspended_until > now
        return JSONResponse({
            "user_id": user_id,
            "suspended": suspended,
            "suspended_until": suspended_until.isoformat() if suspended_until else None,
            "warned_at": warned_at.isoformat() if warned_at else None,
        })
    finally:
        conn.close()
