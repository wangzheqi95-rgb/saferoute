"""M3：三維度衰減與過期（SPEC.md §4.1）。

維度①（環境類狀態機）與維度②（指數衰減）統一成同一條公式：
環境類視為 half_life=∞，0.5**(Δt/∞) = 1，current_risk 恆為 base_severity，
不需要另外開分支；環境類另外走 dismiss_count>=3 的狀態機轉換。

維度③（長期基準風險）由 baseline.py 負責讀取這裡產生的 status/last_at。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection


SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3}


def is_environmental(category: str) -> bool:
    return category in config.ENVIRONMENTAL_CATEGORIES


def get_half_life_hours(category: str, severity: str):
    """環境類回傳 None，代表數學上的「半衰期為無限大」（不衰減）。"""
    if is_environmental(category):
        return None
    return config.HALF_LIFE_HOURS[(category, severity)]


def get_full_expiry_hours(category: str, severity: str):
    """環境類回傳 None：不會因時間自動過期，只能靠修復關閉。"""
    if is_environmental(category):
        return None
    return config.FULL_EXPIRY_HOURS[(category, severity)]


def compute_current_risk(category: str, severity: str, last_at: datetime, as_of: datetime) -> float:
    """維度①②統一公式：current_risk = base_severity × 0.5 ^ (Δt / half_life)。
    環境類 half_life=None 視為無限大，結果恆為 base_severity。
    """
    base = config.SEVERITY_BASE_SCORE[severity]
    half_life = get_half_life_hours(category, severity)
    if half_life is None:
        return base
    delta_hours = (as_of - last_at).total_seconds() / 3600.0
    if delta_hours <= 0:
        return base
    return base * (0.5 ** (delta_hours / half_life))


def compute_expires_at(category: str, severity: str, last_at: datetime):
    """環境類回傳 None（不會因時間過期）。"""
    full_expiry_hours = get_full_expiry_hours(category, severity)
    if full_expiry_hours is None:
        return None
    from datetime import timedelta
    return last_at + timedelta(hours=full_expiry_hours)


def compute_status(category: str, severity: str, last_at: datetime, as_of: datetime,
                    current_status: str, dismiss_count: int) -> str:
    """決定事件的新狀態。

    環境類：active -> repaired（dismiss_count >= 門檻）；repaired 後保持 repaired。
    其餘類別：active -> expired（超過完全過期時數）；expired 後保持 expired。
    """
    if is_environmental(category):
        if current_status == "repaired":
            return "repaired"
        if dismiss_count >= config.REPAIR_DISMISS_COUNT_THRESHOLD:
            return "repaired"
        return "active"

    if current_status == "expired":
        return "expired"
    full_expiry_hours = get_full_expiry_hours(category, severity)
    delta_hours = (as_of - last_at).total_seconds() / 3600.0
    if delta_hours > full_expiry_hours:
        return "expired"
    return "active"


def status_and_risk_at(category: str, severity: str, first_at: datetime, last_at: datetime,
                        as_of: datetime, dismiss_count: int = 0):
    """給定任意時間點 as_of，純函式算出「當時」的 (status, current_risk)，不碰 DB。

    用於事後回溯某個歷史時間點的風險快照（例如 M4 α 實測），而不是即時系統的
    「現在」狀態——current_status 一律視為 'active' 重新計算（不採用 DB 現在儲存的
    status，因為那反映的是相對於真實 now() 的衰減結果，不是相對於 as_of 的）。
    若事件在 as_of 當下還沒發生（first_at > as_of），回傳 (None, 0.0)。
    """
    if first_at > as_of:
        return None, 0.0
    status = compute_status(category, severity, last_at, as_of, "active", dismiss_count)
    current_risk = compute_current_risk(category, severity, last_at, as_of)
    return status, current_risk


# ---------------------------------------------------------------------------
# DB 端整批更新
# ---------------------------------------------------------------------------
def run_decay_cycle(as_of: datetime = None, conn=None):
    """重算所有非 expired/repaired 事件的 current_risk 與 status，並補上 expires_at。
    回傳實際更新的事件數。
    """
    as_of = as_of or datetime.now(timezone.utc)
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT event_id, category, severity, last_at, status, dismiss_count, expires_at
            FROM event
            WHERE status IN ('active')
            """
        )
        rows = cur.fetchall()

        updates = []
        for event_id, category, severity, last_at, status, dismiss_count, expires_at in rows:
            new_risk = compute_current_risk(category, severity, last_at, as_of)
            new_status = compute_status(category, severity, last_at, as_of, status, dismiss_count)
            new_expires_at = expires_at if expires_at is not None else compute_expires_at(category, severity, last_at)
            updates.append((new_risk, new_status, new_expires_at, event_id))

        cur.executemany(
            "UPDATE event SET current_risk = %s, status = %s, expires_at = %s WHERE event_id = %s",
            updates,
        )
        if owns_conn:
            conn.commit()
        return len(updates)
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    n = run_decay_cycle()
    print(f"[decay] 更新了 {n} 個 active 事件")
