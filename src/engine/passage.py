"""B8：路過驗證與佐證資格（SPEC.md §8）。

兩個獨立職責：
1. `record_ping()`：裝置端 GPS 定位比對（§8.1），判定使用者是否「路過」某路段，
   符合條件就寫入 `passage_log`（只存粗略時段，不存座標、不存精確時間戳，§8.2）。
2. `MapMatchPassageVerifier`：`corroborate` 端點呼叫的資格判定（§8.3/§8.4），
   讀 `passage_log` 看使用者是否在時間窗內路過過這個路段。

`user_last_ping` 是比對連續性/方向用的暫存，每次定位進來就覆寫同一筆、不是歷史
紀錄，不受 §8.2 的路過紀錄保存規範約束；超過 `LAST_PING_VALIDITY_SECONDS`
（B8 決議，5 分鐘）視為過期，當作新的起始點，避免跟久未開啟 App 前的舊位置比對。
"""
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config

TAIPEI = timezone(timedelta(hours=8))


def _day_period(dt_taipei: datetime) -> str:
    hour = dt_taipei.hour
    if hour < 6:
        return "dawn"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _nearest_segment(conn, lon: float, lat: float):
    """回傳 (segment_id, distance_m)，同 main.py 的 snap_to_segment；這裡獨立一份
    避免 main.py <-> passage.py 互相 import。"""
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
    return cur.fetchone()


def _segment_bearing_deg(conn, segment_id: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT degrees(ST_Azimuth(ST_StartPoint(geometry), ST_EndPoint(geometry))) FROM road_segment WHERE segment_id = %s",
        (segment_id,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _bearing_deg(conn, lon1, lat1, lon2, lat2):
    cur = conn.cursor()
    cur.execute(
        "SELECT degrees(ST_Azimuth(ST_SetSRID(ST_MakePoint(%s,%s),4326), ST_SetSRID(ST_MakePoint(%s,%s),4326)))",
        (lon1, lat1, lon2, lat2),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _bearing_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def _distance_m(conn, lon1, lat1, lon2, lat2) -> float:
    cur = conn.cursor()
    cur.execute(
        "SELECT ST_Distance(geography(ST_SetSRID(ST_MakePoint(%s,%s),4326)), geography(ST_SetSRID(ST_MakePoint(%s,%s),4326)))",
        (lon1, lat1, lon2, lat2),
    )
    return cur.fetchone()[0]


def _nearby_junction_segments(conn, lon: float, lat: float) -> list:
    """§8.1 第 4 點：定位落在距路口 JUNCTION_ADJACENCY_M 內時，所有連接該路口的
    路段均計為路過。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT node_id,
               ST_Distance(geography(geometry), geography(ST_SetSRID(ST_MakePoint(%s,%s),4326)))
        FROM road_node
        ORDER BY geometry <-> ST_SetSRID(ST_MakePoint(%s,%s),4326)
        LIMIT 1
        """,
        (lon, lat, lon, lat),
    )
    row = cur.fetchone()
    if row is None or row[1] > config.JUNCTION_ADJACENCY_M:
        return []
    node_id = row[0]
    cur.execute("SELECT segment_id FROM segment_topology WHERE from_node = %s OR to_node = %s", (node_id, node_id))
    return [r[0] for r in cur.fetchall()]


def record_ping(conn, user_id: str, lon: float, lat: float, accuracy_m: float,
                 speed_mps: float | None, bearing_deg: float | None, as_of: datetime) -> dict:
    """§8.1 路過判定。依序檢查貼路容忍距離 -> 連續性 -> 方向一致性 -> 路口鄰接，
    符合條件就寫入 passage_log。回傳 dict 供 API 回應：
    {"matched_segment_id", "confirmed_segments", "reason"}（reason 只在未確認路過時有值）。
    `bearing_deg` 目前未使用——方向一致性改用「上一筆定位 -> 這一筆」的移動方向
    反推，比依賴裝置回報的 bearing 更不受裝置實作差異影響；保留參數是因為 API
    輸入規格包含這個欄位，之後若要改用裝置回報值可以直接替換。
    """
    result = {"matched_segment_id": None, "confirmed_segments": [], "reason": None}

    if accuracy_m > config.MAP_MATCH_TOLERANCE_MAX_M:
        result["reason"] = f"GPS 精度 {accuracy_m:.0f}m 超過可信上限（{config.MAP_MATCH_TOLERANCE_MAX_M}m），不判定"
        return result

    tolerance_m = max(config.MAP_MATCH_TOLERANCE_MIN_M, min(accuracy_m, config.MAP_MATCH_TOLERANCE_MAX_M))

    nearest = _nearest_segment(conn, lon, lat)
    if nearest is None:
        result["reason"] = "路網無資料"
        return result
    segment_id, dist = nearest
    if dist > tolerance_m:
        result["reason"] = f"距最近路段 {dist:.0f}m，超過貼路容忍距離 {tolerance_m:.0f}m"
        return result

    result["matched_segment_id"] = segment_id

    cur = conn.cursor()
    cur.execute(
        "SELECT segment_id, lon, lat, cumulative_travel_m, received_at FROM user_last_ping WHERE user_id = %s",
        (user_id,),
    )
    last = cur.fetchone()

    if last is not None:
        _, _, _, _, last_received_at = last
        if (as_of - last_received_at).total_seconds() > config.LAST_PING_VALIDITY_SECONDS:
            last = None  # 過期，視為新的起始點

    confirmed = False
    cumulative_travel_m = 0.0

    if last is not None:
        last_segment_id, last_lon, last_lat, last_cumulative, _ = last
        step_m = _distance_m(conn, last_lon, last_lat, lon, lat)

        if last_segment_id == segment_id:
            cumulative_travel_m = last_cumulative + step_m
            continuity_ok = True  # 連續兩次定位落在同一路段
        else:
            cumulative_travel_m = step_m
            continuity_ok = cumulative_travel_m >= config.MAP_MATCH_MIN_TRAVEL_M

        if continuity_ok:
            if speed_mps is not None and speed_mps < config.MAP_MATCH_STATIONARY_SPEED_MPS:
                direction_ok = True  # 判定為靜止，跳過方向檢查
            else:
                seg_bearing = _segment_bearing_deg(conn, segment_id)
                move_bearing = _bearing_deg(conn, last_lon, last_lat, lon, lat) if step_m > 0 else None
                if seg_bearing is None or move_bearing is None:
                    direction_ok = True
                else:
                    diff = _bearing_diff(seg_bearing, move_bearing)
                    # 路段沒有固定的「正向」，沿同一條線走反方向一樣算數，所以
                    # 角度接近 0 度或接近 180 度都接受。
                    direction_ok = diff <= config.MAP_MATCH_MAX_BEARING_DIFF_DEG or \
                        diff >= (180 - config.MAP_MATCH_MAX_BEARING_DIFF_DEG)
            confirmed = direction_ok

    cur.execute(
        """
        INSERT INTO user_last_ping (user_id, segment_id, lon, lat, cumulative_travel_m, received_at)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET
            segment_id = EXCLUDED.segment_id, lon = EXCLUDED.lon, lat = EXCLUDED.lat,
            cumulative_travel_m = EXCLUDED.cumulative_travel_m, received_at = EXCLUDED.received_at
        """,
        (user_id, segment_id, lon, lat, cumulative_travel_m, as_of),
    )

    if not confirmed:
        result["reason"] = "尚未滿足連續性/方向判定，記錄為起始點"
        return result

    confirmed_segments = {segment_id} | set(_nearby_junction_segments(conn, lon, lat))

    as_of_taipei = as_of.astimezone(TAIPEI)
    occurred_date = as_of_taipei.date()
    day_period = _day_period(as_of_taipei)

    for seg in confirmed_segments:
        cur.execute(
            """
            INSERT INTO passage_log (user_id, segment_id, occurred_date, day_period)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            (user_id, seg, occurred_date, day_period),
        )

    result["confirmed_segments"] = sorted(confirmed_segments)
    return result


class PassageVerifier:
    def check(self, conn, user_id: str, segment_id: str, category: str, action: str, as_of: datetime) -> tuple[bool, str]:
        """回傳 (是否符合資格, 原因說明文字)。action: 'confirm' 或 'dismiss'——
        兩者的時間窗不同（§8.3 依 category、§8.4 固定 24 小時），判定時一定要知道
        是哪個動作。"""
        raise NotImplementedError


class StubPassageVerifier(PassageVerifier):
    """B8 實作前的預設行為：一律判定不符資格，保留供測試/對照用。"""

    def check(self, conn, user_id: str, segment_id: str, category: str, action: str, as_of: datetime) -> tuple[bool, str]:
        return False, "路過驗證（B8）尚未實作，目前無法判定是否符合佐證/解除資格"


class MapMatchPassageVerifier(PassageVerifier):
    """B8 實作：讀 passage_log 判定使用者是否在資格時間窗內路過該路段。

    passage_log 只存「日期＋粗略時段」，沒有精確時間戳，無法精準比對 24 小時／
    7 天窗口。統一用該時段的「最晚可能時間」（config.DAY_PERIOD_LATEST_HOUR）
    換算成時間點再比對，讓時段近似造成的誤差方向固定偏寬鬆——寧可多給資格，
    不要把仍有資格的使用者誤判為不符資格（B8 決議）。
    """

    def check(self, conn, user_id: str, segment_id: str, category: str, action: str, as_of: datetime) -> tuple[bool, str]:
        if action == "dismiss":
            window_hours = config.DISMISS_WINDOW_HOURS
        else:
            window_hours = config.CORROBORATE_WINDOW_HOURS[category]
        cutoff = as_of - timedelta(hours=window_hours)

        cur = conn.cursor()
        cur.execute(
            "SELECT occurred_date, day_period FROM passage_log WHERE user_id = %s AND segment_id = %s",
            (user_id, segment_id),
        )
        for occurred_date, day_period in cur.fetchall():
            latest_hour = config.DAY_PERIOD_LATEST_HOUR[day_period]
            latest_dt = datetime.combine(occurred_date, time(0, 0), tzinfo=TAIPEI) + timedelta(hours=latest_hour)
            if latest_dt >= cutoff:
                return True, ""

        action_label = "解除" if action == "dismiss" else "佐證"
        return False, f"未在 {window_hours} 小時內路過此路段，不符合{action_label}資格"
