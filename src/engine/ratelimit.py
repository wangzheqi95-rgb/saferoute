"""B7：防濫用頻率上限（SPEC.md §9）。

次數統計直接查 report/corroboration 表的時間窗，不另外計數；只有「有沒有被
警告過、有沒有被暫停」這個狀態機用 user_abuse_flag 表記錄。

規則：超過上限時，第一次先警告（仍允許這次請求通過）；如果在同一個窗口內
再次超過（代表已經被警告過還繼續），才真正暫停 ABUSE_SUSPENSION_HOURS 小時。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config


class RateLimitExceeded(Exception):
    def __init__(self, reason: str, suspended_until: datetime = None):
        self.reason = reason
        self.suspended_until = suspended_until
        super().__init__(reason)


def _get_flag(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT warned_at, suspended_until FROM user_abuse_flag WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    return row if row else (None, None)


def _upsert_flag(conn, user_id, warned_at=None, suspended_until=None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO user_abuse_flag (user_id, warned_at, suspended_until)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            warned_at = COALESCE(EXCLUDED.warned_at, user_abuse_flag.warned_at),
            suspended_until = EXCLUDED.suspended_until
        """,
        (user_id, warned_at, suspended_until),
    )


def check_and_record(conn, user_id: str, action: str, as_of: datetime = None):
    """action: 'report' 或 'corroborate'。超過上限時丟 RateLimitExceeded；
    第一次超過會記警告但仍正常放行（呼叫端不需要特別處理，只有真的暫停才擋）。
    """
    as_of = as_of or datetime.now(timezone.utc)
    warned_at, suspended_until = _get_flag(conn, user_id)

    if suspended_until and suspended_until > as_of:
        raise RateLimitExceeded(
            f"帳號因頻率超限已暫停，暫停至 {suspended_until.isoformat()}", suspended_until
        )

    cur = conn.cursor()
    if action == "report":
        cur.execute(
            "SELECT COUNT(*) FROM report WHERE user_id = %s AND created_at >= %s - interval '1 hour'",
            (user_id, as_of),
        )
        hourly = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM report WHERE user_id = %s AND created_at >= %s - interval '1 day'",
            (user_id, as_of),
        )
        daily = cur.fetchone()[0]
        over_limit = hourly >= config.REPORT_RATE_LIMIT_HOURLY or daily >= config.REPORT_RATE_LIMIT_DAILY
    elif action == "corroborate":
        cur.execute(
            "SELECT COUNT(*) FROM corroboration WHERE user_id = %s AND created_at >= %s - interval '1 hour'",
            (user_id, as_of),
        )
        hourly = cur.fetchone()[0]
        over_limit = hourly >= config.CORROBORATE_RATE_LIMIT_HOURLY
    else:
        raise ValueError(f"unknown action: {action}")

    if not over_limit:
        return {"warned": False}

    # 已經在近一小時內被警告過、這次又超過 -> 暫停
    already_warned_recently = warned_at is not None and (as_of - warned_at) < timedelta(hours=1)
    if already_warned_recently:
        suspended_until = as_of + timedelta(hours=config.ABUSE_SUSPENSION_HOURS)
        _upsert_flag(conn, user_id, suspended_until=suspended_until)
        raise RateLimitExceeded(
            f"持續超過頻率上限，帳號已暫停 {config.ABUSE_SUSPENSION_HOURS} 小時", suspended_until
        )

    _upsert_flag(conn, user_id, warned_at=as_of)
    return {"warned": True}
