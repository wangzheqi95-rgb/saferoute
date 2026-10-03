"""M3：風險合成與擴散（SPEC.md §4.3）。

realtime_risk(segment) = 1 - Π(1 - current_risk_i)   # 該路段所有貢獻（含擴散）
total_risk = 1 - (1 - realtime_risk) × (1 - baseline_risk × 0.6)

擴散依 category（personal_safety 再依 severity）決定，見 config.DIFFUSION_RATES：
固定設施類（streetlight/obstruction/construction）不擴散；personal_safety/
traffic_accident/crowd 涉及移動的人，保留擴散。
"""
import sys
from collections import defaultdict
from pathlib import Path

from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection
from src.engine import decay


def build_adjacency(conn):
    """回傳 segment_id -> set(一跳鄰居 segment_id) 的鄰接表（共用 road_node 視為相鄰）。"""
    cur = conn.cursor()
    cur.execute("SELECT segment_id, from_node, to_node FROM segment_topology")
    rows = cur.fetchall()

    node_to_segments = defaultdict(set)
    for seg_id, from_node, to_node in rows:
        node_to_segments[from_node].add(seg_id)
        node_to_segments[to_node].add(seg_id)

    adjacency = defaultdict(set)
    for seg_id, from_node, to_node in rows:
        for node in (from_node, to_node):
            for other in node_to_segments[node]:
                if other != seg_id:
                    adjacency[seg_id].add(other)

    return adjacency


def get_two_hop(adjacency, segment_id):
    one_hop = adjacency.get(segment_id, set())
    two_hop = set()
    for neighbor in one_hop:
        two_hop |= adjacency.get(neighbor, set())
    two_hop -= one_hop
    two_hop.discard(segment_id)
    return two_hop


def diffusion_rates(category: str, severity: str):
    """回傳 (一跳比例, 二跳比例)。"""
    entry = config.DIFFUSION_RATES[category]
    return entry[severity] if isinstance(entry, dict) else entry


def diffusion_multiplier(category: str, severity: str, hop: int) -> float:
    one, two = diffusion_rates(category, severity)
    return one if hop == 1 else two


def combine_risks(risks) -> float:
    """機率式合成：1 - Π(1 - r_i)。"""
    prob_survive = 1.0
    for r in risks:
        prob_survive *= (1 - r)
    return 1 - prob_survive


def display_tier(total_risk: float) -> str:
    if total_risk < config.DISPLAY_TIER_LOW_MAX:
        return "low"
    if total_risk < config.DISPLAY_TIER_MEDIUM_MAX:
        return "medium"
    return "high"


TIER_RANK = {"low": 0, "medium": 1, "high": 2}


def apply_tier_cap(tier: str, touching_report_counts: list) -> str:
    """顯示門檻（產品決策，§4.3，B7 調整）：路段上觸及的 active 事件裡沒有任何一個
    (report_count + confirm_count) >= 門檻時，tier 最高只能到 TIER_CAP_SINGLE_REPORT。
    路段完全沒有 active 事件觸及（touching_report_counts 為空，風險純粹來自
    baseline）時不受此限制。呼叫端已把 report_count+confirm_count 加總後才放進
    touching_report_counts，這裡不重複處理兩者的分別。
    """
    if not touching_report_counts:
        return tier
    if any(rc >= config.TIER_CAP_MIN_REPORT_COUNT for rc in touching_report_counts):
        return tier
    cap = config.TIER_CAP_SINGLE_REPORT
    return cap if TIER_RANK[cap] < TIER_RANK[tier] else tier


def compute_contributions(conn, adjacency):
    """共用核心：抓全部 active 事件，算出每個路段收到的風險貢獻清單
    （含擴散）、每個路段被哪些 report_count 觸及，以及每個事件各自的
    擴散影響路段清單。排程（全區）與即時（單路段+擴散範圍）都呼叫這個函式，
    差別只在後續「寫入哪些 segment_id」的範圍，不是兩套計算邏輯。

    回傳 (contributions, touching_report_counts, event_affected_map)：
    - contributions: segment_id -> [current_risk 貢獻值, ...]
    - touching_report_counts: segment_id -> [report_count, ...]
    - event_affected_map: event_id -> 排序後的擴散影響路段 list
    """
    cur = conn.cursor()
    cur.execute(
        """SELECT event_id, segment_id, category, severity, current_risk,
                  report_count + confirm_count AS corroboration_count
           FROM event WHERE status = 'active'"""
    )
    active_events = cur.fetchall()

    contributions = defaultdict(list)
    touching_report_counts = defaultdict(list)
    event_affected_map = {}

    for event_id, segment_id, category, severity, current_risk, corroboration_count in active_events:
        contributions[segment_id].append(current_risk)
        touching_report_counts[segment_id].append(corroboration_count)

        mult_1, mult_2 = diffusion_rates(category, severity)
        affected = set()

        if mult_1 > 0:
            one_hop = adjacency.get(segment_id, set())
            for seg in one_hop:
                contributions[seg].append(current_risk * mult_1)
                touching_report_counts[seg].append(corroboration_count)
            affected |= one_hop

        if mult_2 > 0:
            two_hop = get_two_hop(adjacency, segment_id)
            for seg in two_hop:
                contributions[seg].append(current_risk * mult_2)
                touching_report_counts[seg].append(corroboration_count)
            affected |= two_hop

        event_affected_map[event_id] = sorted(affected)

    return contributions, touching_report_counts, event_affected_map


def write_segment_risk(conn, segment_ids, baseline_map, contributions, touching_report_counts):
    """只重算、寫入指定 segment_ids 的 segment_risk（UPSERT）。回傳 tier 分布統計 dict。"""
    tier_counts = {"low": 0, "medium": 0, "high": 0}
    segment_risk_rows = []
    for segment_id in segment_ids:
        baseline_risk = baseline_map.get(segment_id, 0.0)
        realtime_risk = combine_risks(contributions.get(segment_id, []))
        total_risk = 1 - (1 - realtime_risk) * (1 - baseline_risk * config.BASELINE_RISK_MULTIPLIER)
        tier = display_tier(total_risk)
        tier = apply_tier_cap(tier, touching_report_counts.get(segment_id, []))
        tier_counts[tier] += 1
        segment_risk_rows.append((segment_id, realtime_risk, baseline_risk, total_risk, tier))

    if segment_risk_rows:
        with conn.cursor() as write_cur:
            execute_values(
                write_cur,
                """
                INSERT INTO segment_risk (segment_id, realtime_risk, baseline_risk, total_risk, display_tier, updated_at)
                VALUES %s
                ON CONFLICT (segment_id) DO UPDATE SET
                    realtime_risk = EXCLUDED.realtime_risk,
                    baseline_risk = EXCLUDED.baseline_risk,
                    total_risk = EXCLUDED.total_risk,
                    display_tier = EXCLUDED.display_tier,
                    updated_at = EXCLUDED.updated_at
                """,
                segment_risk_rows,
                template="(%s, %s, %s, %s, %s, now())",
            )

    return tier_counts, {row[0]: row[4] for row in segment_risk_rows}


def write_event_affected_segments(conn, event_ids, event_affected_map):
    """只更新指定 event_ids 的 affected_segments 欄位。"""
    updates = [(event_affected_map[eid], eid) for eid in event_ids if eid in event_affected_map]
    if not updates:
        return
    with conn.cursor() as write_cur:
        execute_values(
            write_cur,
            """
            UPDATE event AS e SET affected_segments = data.segs
            FROM (VALUES %s) AS data(segs, event_id)
            WHERE e.event_id = data.event_id
            """,
            updates,
            template="(%s::text[], %s::uuid)",
        )


def run_aggregate_cycle(conn=None):
    """排程用（§4.5）：重算全區所有路段，並更新所有事件的 affected_segments。
    回傳 tier 分布統計 dict，供驗證/log 使用。
    """
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        adjacency = build_adjacency(conn)
        contributions, touching_report_counts, event_affected_map = compute_contributions(conn, adjacency)

        cur = conn.cursor()
        cur.execute("SELECT segment_id, baseline_risk FROM road_segment")
        baseline_map = dict(cur.fetchall())

        tier_counts, _ = write_segment_risk(conn, baseline_map.keys(), baseline_map, contributions, touching_report_counts)
        write_event_affected_segments(conn, event_affected_map.keys(), event_affected_map)

        if owns_conn:
            conn.commit()

        return tier_counts
    finally:
        if owns_conn:
            conn.close()


def run_incremental_aggregate(conn, segment_id, triggering_event_id=None):
    """即時用（B7）：只重算 segment_id 本身 + 一跳 + 二跳鄰居，不碰其餘路段，
    也不重跑 baseline（KDE 那塊留給每日排程）。跟 run_aggregate_cycle() 共用
    compute_contributions()／write_segment_risk()／write_event_affected_segments()
    這三個核心函式，只有「要寫入哪些 segment_id」的範圍不同，不是兩套邏輯。

    回傳 {segment_id: display_tier} 只涵蓋這次重算到的路段。
    """
    adjacency = build_adjacency(conn)
    targets = {segment_id} | adjacency.get(segment_id, set()) | get_two_hop(adjacency, segment_id)

    contributions, touching_report_counts, event_affected_map = compute_contributions(conn, adjacency)

    cur = conn.cursor()
    cur.execute("SELECT segment_id, baseline_risk FROM road_segment WHERE segment_id = ANY(%s)", (list(targets),))
    baseline_map = dict(cur.fetchall())

    _, tiers_by_segment = write_segment_risk(conn, targets, baseline_map, contributions, touching_report_counts)

    if triggering_event_id is not None:
        write_event_affected_segments(conn, [triggering_event_id], event_affected_map)

    return tiers_by_segment


def compute_snapshot(conn, as_of, baseline_map=None):
    """回溯 as_of 當下的 segment_risk，不寫入 DB（供 M4 α 實測、M7 時間軸等回溯情境使用）。

    realtime_risk 用 decay.status_and_risk_at() 在 as_of 當下重新算，不採用 DB 目前
    儲存的 status/current_risk（那是相對真實 now() 的結果）。baseline_map 未提供時
    沿用 road_segment 目前的值（M4 用法：相對 as_of 只差幾天，影響可忽略）；
    M7 呼叫時會傳入 `baseline.compute_snapshot()` 算出的 as_of 當下 baseline。

    回傳 (snapshot_dict, active_personal_safety_count, active_event_count)：
    snapshot_dict: segment_id -> {"realtime_risk", "baseline_risk", "total_risk", "display_tier"}
    """
    adjacency = build_adjacency(conn)

    cur = conn.cursor()
    cur.execute(
        """SELECT event_id, category, severity, first_at, last_at,
                  report_count + confirm_count AS corroboration_count
           FROM event"""
    )
    all_events = cur.fetchall()

    contributions = defaultdict(list)
    touching_report_counts = defaultdict(list)
    active_personal_safety_count = 0
    active_event_count = 0

    cur.execute("SELECT event_id, segment_id FROM event")
    event_segment_map = dict(cur.fetchall())

    for event_id, category, severity, first_at, last_at, corroboration_count in all_events:
        status, current_risk = decay.status_and_risk_at(category, severity, first_at, last_at, as_of)
        if status != "active":
            continue
        active_event_count += 1
        if category == "personal_safety":
            active_personal_safety_count += 1

        segment_id = event_segment_map[event_id]
        contributions[segment_id].append(current_risk)
        touching_report_counts[segment_id].append(corroboration_count)

        mult_1, mult_2 = diffusion_rates(category, severity)

        if mult_1 > 0:
            one_hop = adjacency.get(segment_id, set())
            for seg in one_hop:
                contributions[seg].append(current_risk * mult_1)
                touching_report_counts[seg].append(corroboration_count)

        if mult_2 > 0:
            two_hop = get_two_hop(adjacency, segment_id)
            for seg in two_hop:
                contributions[seg].append(current_risk * mult_2)
                touching_report_counts[seg].append(corroboration_count)

    if baseline_map is None:
        cur.execute("SELECT segment_id, baseline_risk FROM road_segment")
        baseline_map = dict(cur.fetchall())

    snapshot = {}
    for segment_id, baseline_risk in baseline_map.items():
        realtime_risk = combine_risks(contributions.get(segment_id, []))
        total_risk = 1 - (1 - realtime_risk) * (1 - baseline_risk * config.BASELINE_RISK_MULTIPLIER)
        tier = display_tier(total_risk)
        tier = apply_tier_cap(tier, touching_report_counts.get(segment_id, []))
        snapshot[segment_id] = {
            "realtime_risk": realtime_risk,
            "baseline_risk": baseline_risk,
            "total_risk": total_risk,
            "display_tier": tier,
        }

    return snapshot, active_personal_safety_count, active_event_count


def get_active_events_at(conn, as_of):
    """回傳 as_of 當下所有 active 事件，附上其所在路段的中心點座標
    （供 M7 地圖事件標記使用）。每筆：
    {event_id, category, severity, segment_id, lon, lat}
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.event_id, e.category, e.severity, e.first_at, e.last_at, e.segment_id,
               ST_X(ST_Centroid(rs.geometry)), ST_Y(ST_Centroid(rs.geometry))
        FROM event e JOIN road_segment rs ON rs.segment_id = e.segment_id
        """
    )
    rows = cur.fetchall()

    markers = []
    for event_id, category, severity, first_at, last_at, segment_id, lon, lat in rows:
        status, _ = decay.status_and_risk_at(category, severity, first_at, last_at, as_of)
        if status != "active":
            continue
        markers.append({
            "event_id": str(event_id),
            "category": category,
            "severity": severity,
            "segment_id": segment_id,
            "lon": lon,
            "lat": lat,
        })
    return markers


if __name__ == "__main__":
    counts = run_aggregate_cycle()
    total = sum(counts.values())
    print(f"[aggregate] display_tier 分布：{counts}（共 {total} 條路段）")
