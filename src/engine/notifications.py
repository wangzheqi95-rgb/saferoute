"""B10：通知佇列。本階段只寫入 notification_queue，不實際發送（App 尚未開發，
無接收裝置）。

觸發時機：新事件建立、或既有事件被升級為 severe 時（呼叫端——`main.py` 的
`create_report()`——負責判斷是哪一種情況，這裡只負責「給定一個事件與嚴重度，
把範圍內該入列的使用者寫進去」）。

對象判定：以 `user_last_ping` 的最後已知位置計算，`NOTIFICATION_RADIUS_M`
（200m）內的使用者才入列。這是目前系統唯一可用的「使用者目前位置」來源；
`user_last_ping` 的紀錄可能是使用者很久以前留下的（B8 的 5 分鐘有效期限只用
在路過判定的連續性比對，不影響這裡），沒有另外依新舊過濾。

免費／付費差異（D2）：severe 一律入列；minor／moderate 只有 Premium 使用者
入列。`user_tier` 查 `app_user` 表，沒有資料的使用者視為 free（預設）。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config


def get_user_tier(conn, user_id: str) -> str:
    cur = conn.cursor()
    cur.execute("SELECT user_tier FROM app_user WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    return row[0] if row else "free"


def enqueue_for_event(conn, event_id, segment_id: str, severity: str, lon: float, lat: float, as_of: datetime) -> int:
    """找出事件位置 NOTIFICATION_RADIUS_M 內、依 user_tier 符合入列條件的使用者，
    寫入 notification_queue（同一使用者對同一事件只入列一次，ON CONFLICT 處理）。
    回傳實際新入列的筆數。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id,
               ST_Distance(
                   geography(ST_SetSRID(ST_MakePoint(lon, lat), 4326)),
                   geography(ST_SetSRID(ST_MakePoint(%s, %s), 4326))
               ) AS distance_m
        FROM user_last_ping
        WHERE ST_DWithin(
            geography(ST_SetSRID(ST_MakePoint(lon, lat), 4326)),
            geography(ST_SetSRID(ST_MakePoint(%s, %s), 4326)),
            %s
        )
        """,
        (lon, lat, lon, lat, config.NOTIFICATION_RADIUS_M),
    )
    nearby = cur.fetchall()

    inserted = 0
    for user_id, distance_m in nearby:
        if severity != "severe" and get_user_tier(conn, user_id) != "premium":
            continue
        cur.execute(
            """
            INSERT INTO notification_queue (id, user_id, event_id, segment_id, distance_m, severity, created_at, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
            ON CONFLICT (user_id, event_id) DO NOTHING
            """,
            (uuid.uuid4(), user_id, event_id, segment_id, distance_m, severity, as_of),
        )
        inserted += cur.rowcount
    return inserted
