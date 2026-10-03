"""B8：路過紀錄到期處理（SPEC.md §8.2）。

獨立腳本，比照 decay.py／aggregate.py 的執行模式（手動或外部排程跑
`python -m src.engine.passage_cleanup`；專案目前沒有真正的 cron，衰減/聚合
也都是手動執行）。兩個步驟各自冪等，可重複呼叫：

1. 到期累加：`passage_log` 滿 `PASSAGE_LOG_RETENTION_DAYS`（7 天，依內部的
   `created_at` 判斷，不是使用者可見的欄位）的紀錄，先累加進
   `user_segment_frequency`（count += 1），累加完成後才刪除原始紀錄。
   `PASSAGE_TO_FREQUENCY_ENABLED=False` 時只刪除、不累加——關掉的是「餵給
   個人化功能」這個下游用途，不是 passage_log 本身的保存期限。
2. 每週衰減：`last_updated` 超過 `FREQUENCY_DECAY_IDLE_DAYS`（預設 7 天，與
   `PASSAGE_LOG_RETENTION_DAYS` 是各自獨立的參數，不得互相替代）未更新的
   `user_segment_frequency` 紀錄，count 乘上 `FREQUENCY_DECAY_WEEKLY`；衰減後
   低於 `FREQUENCY_MIN_COUNT` 才刪除——只刪「這次真的被衰減到」的紀錄，不會把
   剛累加、count 還是 1 的新紀錄也刪掉（那些紀錄的 last_updated 是這次執行的
   時間，還沒到 `FREQUENCY_DECAY_IDLE_DAYS`，不會被選中衰減）。這是刻意設計：
   刪除範圍天然被限制在「本次被衰減到」的子集合內，不需要額外的保護期參數；
   之後若修改刪除範圍的邏輯，需要保留這個性質。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection


def accumulate_expired_passage_logs(conn, as_of: datetime = None) -> int:
    as_of = as_of or datetime.now(timezone.utc)
    cutoff = as_of - timedelta(days=config.PASSAGE_LOG_RETENTION_DAYS)
    cur = conn.cursor()

    cur.execute("SELECT user_id, segment_id FROM passage_log WHERE created_at < %s", (cutoff,))
    expired = cur.fetchall()

    if config.PASSAGE_TO_FREQUENCY_ENABLED:
        for user_id, segment_id in expired:
            cur.execute(
                """
                INSERT INTO user_segment_frequency (user_id, segment_id, count, last_updated)
                VALUES (%s, %s, 1, %s)
                ON CONFLICT (user_id, segment_id) DO UPDATE SET
                    count = user_segment_frequency.count + 1,
                    last_updated = EXCLUDED.last_updated
                """,
                (user_id, segment_id, as_of),
            )

    cur.execute("DELETE FROM passage_log WHERE created_at < %s", (cutoff,))
    return len(expired)


def decay_frequencies(conn, as_of: datetime = None) -> tuple[int, int]:
    as_of = as_of or datetime.now(timezone.utc)
    decay_cutoff = as_of - timedelta(days=config.FREQUENCY_DECAY_IDLE_DAYS)
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE user_segment_frequency
        SET count = count * %s, last_updated = %s
        WHERE last_updated < %s
        RETURNING user_id, segment_id, count
        """,
        (config.FREQUENCY_DECAY_WEEKLY, as_of, decay_cutoff),
    )
    decayed_rows = cur.fetchall()

    to_prune = [(u, s) for u, s, c in decayed_rows if c < config.FREQUENCY_MIN_COUNT]
    for user_id, segment_id in to_prune:
        cur.execute(
            "DELETE FROM user_segment_frequency WHERE user_id = %s AND segment_id = %s",
            (user_id, segment_id),
        )

    return len(decayed_rows), len(to_prune)


if __name__ == "__main__":
    conn = get_connection()
    try:
        expired_count = accumulate_expired_passage_logs(conn)
        decayed_count, pruned_count = decay_frequencies(conn)
        conn.commit()
        print(
            f"[passage_cleanup] 到期累加 {expired_count} 筆 passage_log；"
            f"衰減 {decayed_count} 筆、刪除 {pruned_count} 筆 user_segment_frequency"
        )
    finally:
        conn.close()
