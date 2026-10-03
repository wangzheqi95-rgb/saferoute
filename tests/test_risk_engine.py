"""風險引擎核心行為的冒煙測試（SPEC.md §4）。

不連接資料庫——只測試 decay.py／aggregate.py 裡不需要 DB 的純函式，用固定的
輸入事件驗證輸出的風險值與邏輯是否符合公式本身，不依賴 config.py 裡的實際
校準數值（全部從 config 讀，數值變動不應讓這些測試失敗）。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.engine import aggregate, decay


def test_segment_risk_from_fixed_event():
    """給定一個固定的 severe personal_safety 事件（Δt=0，剛發生），驗證：
    current_risk 等於 base_severity（衰減係數在 Δt=0 時為 1）、
    realtime_risk 在只有單一事件貢獻時等於 current_risk、
    total_risk 在 baseline_risk=0 時等於 realtime_risk、
    display_tier 與 config 的門檻切分邏輯一致。
    """
    now = datetime.now(timezone.utc)
    category, severity = "personal_safety", "severe"

    current_risk = decay.compute_current_risk(category, severity, last_at=now, as_of=now)
    assert current_risk == config.SEVERITY_BASE_SCORE[severity]

    realtime_risk = aggregate.combine_risks([current_risk])
    assert realtime_risk == current_risk

    baseline_risk = 0.0
    total_risk = 1 - (1 - realtime_risk) * (1 - baseline_risk * config.BASELINE_RISK_MULTIPLIER)
    assert total_risk == realtime_risk

    tier = aggregate.display_tier(total_risk)
    if total_risk >= config.DISPLAY_TIER_MEDIUM_MAX:
        assert tier == "high"
    elif total_risk >= config.DISPLAY_TIER_LOW_MAX:
        assert tier == "medium"
    else:
        assert tier == "low"


def test_decay_reduces_risk_over_time():
    """驗證指數衰減公式本身的行為（不檢查實際半衰期數值）：
    時間經過後風險嚴格遞減，且經過一個半衰期後風險約為初始值的一半。
    """
    now = datetime.now(timezone.utc)
    category, severity = "personal_safety", "moderate"
    half_life_hours = decay.get_half_life_hours(category, severity)

    risk_at_zero = decay.compute_current_risk(category, severity, last_at=now, as_of=now)
    risk_after_half_life = decay.compute_current_risk(
        category, severity, last_at=now, as_of=now + timedelta(hours=half_life_hours)
    )
    risk_after_two_half_lives = decay.compute_current_risk(
        category, severity, last_at=now, as_of=now + timedelta(hours=half_life_hours * 2)
    )

    assert risk_after_half_life < risk_at_zero
    assert risk_after_two_half_lives < risk_after_half_life
    assert abs(risk_after_half_life - risk_at_zero / 2) < 1e-9


def test_tier_cap_requires_corroboration():
    """驗證 §4.3 顯示門檻規則：獨立回報/佐證數未達 config 門檻時，
    display_tier 封頂；達到門檻後不封頂。不寫死門檻數值，直接從 config 讀。
    """
    threshold = config.TIER_CAP_MIN_REPORT_COUNT
    cap_tier = config.TIER_CAP_SINGLE_REPORT

    below_threshold = [threshold - 1] if threshold > 0 else [0]
    at_threshold = [threshold]

    assert aggregate.apply_tier_cap("high", below_threshold) == cap_tier
    assert aggregate.apply_tier_cap("high", at_threshold) == "high"
    # 路段完全沒有 active 事件觸及時不受此限制
    assert aggregate.apply_tier_cap("high", []) == "high"
