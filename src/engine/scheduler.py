"""M3：排程（SPEC.md §4.5）。

每 5 分鐘：重算所有 active 事件的 current_risk -> 標記過期 -> 重算受影響路段的
segment_risk -> 更新 updated_at。
基準風險（KDE）每日重算一次。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.engine import aggregate, baseline, decay


def run_five_minute_cycle():
    n_events = decay.run_decay_cycle()
    tier_counts = aggregate.run_aggregate_cycle()
    print(f"[scheduler] decay 更新 {n_events} 個事件；display_tier 分布 {tier_counts}")


def run_daily_baseline():
    result = baseline.run_baseline_recompute()
    print(f"[scheduler] baseline 重算完成，共 {len(result)} 條路段")


def main():
    scheduler = BlockingScheduler()
    now = datetime.now()
    # 啟動時手動跑一次（見下方），排程的「下一次」從一個間隔之後開始，避免重覆執行
    scheduler.add_job(run_five_minute_cycle, "interval", minutes=config.SCHEDULER_INTERVAL_MINUTES,
                       id="decay_aggregate_cycle",
                       next_run_time=now + timedelta(minutes=config.SCHEDULER_INTERVAL_MINUTES))
    scheduler.add_job(run_daily_baseline, "interval", hours=config.BASELINE_RECOMPUTE_INTERVAL_HOURS,
                       id="baseline_recompute",
                       next_run_time=now + timedelta(hours=config.BASELINE_RECOMPUTE_INTERVAL_HOURS))

    print("[scheduler] 啟動時先跑一次完整計算...")
    run_daily_baseline()
    run_five_minute_cycle()

    print(f"[scheduler] 排程啟動：每 {config.SCHEDULER_INTERVAL_MINUTES} 分鐘重算風險，"
          f"每 {config.BASELINE_RECOMPUTE_INTERVAL_HOURS} 小時重算 baseline")
    scheduler.start()


if __name__ == "__main__":
    main()
