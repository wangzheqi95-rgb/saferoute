"""M3：長期基準風險 KDE（SPEC.md §4.2，含使用者補充規格一：路段取樣點）。

沿路網做核密度估計：
1. 取符合時間窗的歷史事件（維度③規則），權重 = base_severity × location_confidence
2. 每條路段每 KDE_SAMPLE_INTERVAL_M 公尺取樣一點，各點計算 300m 內事件的
   高斯核加權和（bandwidth=150m），取該路段所有取樣點的平均值
3. 全區以 95 百分位正規化到 0-1，超過者設 1.0
4. 寫回 road_segment.baseline_risk

事件的「位置」與「location_confidence」取該事件最早的一筆 report
（SPEC 未明訂 event 本身的座標，此為實作假設，見 M3 報告 open questions）。
"""
from datetime import datetime, timezone

import numpy as np
from psycopg2.extras import execute_values
from scipy.spatial import cKDTree
from shapely import wkb

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection
from src.engine import decay

BASE_SCORE = config.SEVERITY_BASE_SCORE


def fetch_baseline_input_events_at(conn, as_of: datetime):
    """撈出「as_of 當下」符合維度③規則的歷史事件，回傳 (xs, ys, weights)
    三個 numpy array（EPSG:3826）。用 decay.status_and_risk_at() 判斷 as_of 當下
    每個事件的狀態，不依賴 DB 現在儲存的 status（那是相對真實 now() 的結果），
    因此可以回溯任意時間點（供 M7 時間軸使用）。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.event_id, e.category, e.severity, e.first_at, e.last_at,
               ST_X(ST_Transform(r.location, 3826)), ST_Y(ST_Transform(r.location, 3826)),
               r.location_confidence
        FROM event e
        JOIN LATERAL (
            SELECT location, location_confidence FROM report
            WHERE report.event_id = e.event_id
            ORDER BY created_at ASC LIMIT 1
        ) r ON true
        """
    )
    all_rows = cur.fetchall()

    xs, ys, weights = [], [], []
    for event_id, category, severity, first_at, last_at, x, y, loc_conf in all_rows:
        if category == "crowd":
            continue  # 維度③：crowd 不計入

        status, _ = decay.status_and_risk_at(category, severity, first_at, last_at, as_of)
        if status is None:
            continue  # as_of 當下這個事件還沒發生

        if decay.is_environmental(category):
            if status != "active":
                continue
        else:
            if status != "expired":
                continue
            days_window = config.BASELINE_TIME_WINDOW_DAYS.get((category, severity))
            if days_window is None:
                continue
            age_days = (as_of - last_at).total_seconds() / 86400.0
            if age_days > days_window:
                continue

        xs.append(x)
        ys.append(y)
        weights.append(BASE_SCORE[severity] * (loc_conf if loc_conf is not None else 1.0))

    return np.array(xs), np.array(ys), np.array(weights)


def sample_points_along_segment(geom_3826, interval_m):
    length = geom_3826.length
    n_points = max(1, round(length / interval_m))
    fractions = [(i + 0.5) / n_points for i in range(n_points)]
    return [geom_3826.interpolate(f, normalized=True) for f in fractions]


def fetch_segments_with_samples(conn):
    """回傳 segment_id 清單，以及每條路段的取樣點（EPSG:3826 座標）。"""
    cur = conn.cursor()
    cur.execute("SELECT segment_id, ST_AsBinary(ST_Transform(geometry, 3826)) FROM road_segment")
    rows = cur.fetchall()

    segment_ids = []
    sample_owner = []  # 每個取樣點屬於哪個 segment（用 index）
    sample_xs = []
    sample_ys = []

    for idx, (seg_id, geom_bin) in enumerate(rows):
        geom = wkb.loads(bytes(geom_bin))
        segment_ids.append(seg_id)
        for pt in sample_points_along_segment(geom, config.KDE_SAMPLE_INTERVAL_M):
            sample_owner.append(idx)
            sample_xs.append(pt.x)
            sample_ys.append(pt.y)

    return (
        segment_ids,
        np.array(sample_owner),
        np.column_stack([np.array(sample_xs), np.array(sample_ys)]),
    )


def compute_kde_values(sample_coords, event_xs, event_ys, event_weights):
    """對每個取樣點計算 300m 內事件的高斯核加權和。"""
    n_samples = len(sample_coords)
    if len(event_xs) == 0:
        return np.zeros(n_samples)

    event_coords = np.column_stack([event_xs, event_ys])
    event_tree = cKDTree(event_coords)

    neighbor_lists = event_tree.query_ball_point(sample_coords, r=config.KDE_RADIUS_M, workers=-1)

    values = np.zeros(n_samples)
    bandwidth = config.KDE_BANDWIDTH_M
    for i, neighbors in enumerate(neighbor_lists):
        if not neighbors:
            continue
        neighbors = np.array(neighbors)
        d = np.linalg.norm(event_coords[neighbors] - sample_coords[i], axis=1)
        kernel = np.exp(-0.5 * (d / bandwidth) ** 2)
        values[i] = np.sum(event_weights[neighbors] * kernel)
    return values


def run_baseline_recompute(conn=None):
    """回傳 (segment_id -> baseline_risk) 的 dict，並寫回 road_segment.baseline_risk。"""
    owns_conn = conn is None
    conn = conn or get_connection()
    try:
        as_of = datetime.now(timezone.utc)
        baseline_map = compute_snapshot(conn, as_of)

        rows = list(zip(baseline_map.values(), baseline_map.keys()))
        with conn.cursor() as cur:
            execute_values(
                cur,
                "UPDATE road_segment AS rs SET baseline_risk = data.b FROM (VALUES %s) AS data(b, segment_id) WHERE rs.segment_id = data.segment_id",
                rows,
                template="(%s, %s)",
            )
        if owns_conn:
            conn.commit()

        return baseline_map
    finally:
        if owns_conn:
            conn.close()


def compute_snapshot(conn, as_of: datetime):
    """回溯 as_of 當下的 baseline_risk，不寫入 DB（供 M7 時間軸等回溯情境使用）。
    回傳 segment_id -> baseline_risk 的 dict。
    """
    event_xs, event_ys, event_weights = fetch_baseline_input_events_at(conn, as_of)
    segment_ids, sample_owner, sample_coords = fetch_segments_with_samples(conn)

    sample_values = compute_kde_values(sample_coords, event_xs, event_ys, event_weights)

    n_segments = len(segment_ids)
    raw = np.zeros(n_segments)
    counts = np.zeros(n_segments)
    np.add.at(raw, sample_owner, sample_values)
    np.add.at(counts, sample_owner, 1)
    raw = raw / np.maximum(counts, 1)

    p95 = np.percentile(raw, config.KDE_UPPER_PERCENTILE) if n_segments else 0
    if p95 > 0:
        baseline = np.clip(raw / p95, 0, 1.0)
    else:
        baseline = np.zeros(n_segments)

    return dict(zip(segment_ids, baseline.tolist()))


if __name__ == "__main__":
    result = run_baseline_recompute()
    print(f"[baseline] 重算了 {len(result)} 條路段的 baseline_risk")
