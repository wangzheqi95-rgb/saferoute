"""M2：合成資料生成器（SPEC.md §3）。

產生 12 個月、符合官方統計特性的模擬事件流，寫入 report / event 表。
這是模擬資料：每筆 report.source = 'synthetic'，所有 log 訊息也標記 [synthetic]。

空間抽樣機率 = road_type 權重（依 category）× 活動密度係數 × 路段長度權重。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import psycopg2.extras
from psycopg2.extras import execute_values

psycopg2.extras.register_uuid()
from scipy.spatial import cKDTree
from shapely import wkb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection

SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3}
CATEGORIES = list(config.CATEGORY_DISTRIBUTION.keys())


def log(msg):
    print(f"[synthetic] {msg}")


# ---------------------------------------------------------------------------
# 資料載入與空間權重計算
# ---------------------------------------------------------------------------
def load_segments(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT segment_id, name, road_type, length_m, ST_AsBinary(geometry) FROM road_segment"
    )
    rows = cur.fetchall()
    segment_id, name, road_type, length_m, geom = [], [], [], [], []
    for sid, nm, rt, ln, g in rows:
        segment_id.append(sid)
        name.append(nm)
        road_type.append(rt)
        length_m.append(ln)
        geom.append(wkb.loads(bytes(g)))
    return {
        "segment_id": np.array(segment_id, dtype=object),
        "name": np.array(name, dtype=object),
        "road_type": np.array(road_type, dtype=object),
        "length_m": np.array(length_m, dtype=float),
        "geometry": geom,
    }


def project_centroids_3826(conn, segment_ids):
    """用 DB 端 ST_Transform 取得投影後中心點座標（比在 Python 端重投影簡單可靠）。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT segment_id, ST_X(c), ST_Y(c) FROM (
            SELECT segment_id,
                   ST_Centroid(ST_Transform(geometry, 3826)) AS c
            FROM road_segment
        ) t
        """
    )
    coord_map = {sid: (x, y) for sid, x, y in cur.fetchall()}
    xs = np.array([coord_map[sid][0] for sid in segment_ids])
    ys = np.array([coord_map[sid][1] for sid in segment_ids])
    return np.column_stack([xs, ys])


def compute_activity_density_coef(coords_3826):
    tree = cKDTree(coords_3826)
    raw_counts = tree.query_ball_point(
        coords_3826, r=config.ACTIVITY_DENSITY_RADIUS_M, return_length=True, workers=-1
    ).astype(float)

    p_lo = np.percentile(raw_counts, config.ACTIVITY_DENSITY_LOWER_PERCENTILE)
    p_hi = np.percentile(raw_counts, config.ACTIVITY_DENSITY_UPPER_PERCENTILE)
    coef = (raw_counts - p_lo) / (p_hi - p_lo)
    coef = np.clip(coef, config.ACTIVITY_DENSITY_FLOOR, 1.0)
    return coef, tree, raw_counts


def spatial_weight_table_for(category):
    if category == "personal_safety":
        return config.PERSONAL_SAFETY_SPATIAL_WEIGHT
    if category == "traffic_accident":
        return config.TRAFFIC_ACCIDENT_SPATIAL_WEIGHT
    return None  # -> 均一環境類權重


def build_category_probabilities(segments, base_weight):
    probs = {}
    for category in CATEGORIES:
        table = spatial_weight_table_for(category)
        if table is None:
            road_type_weight = np.full(len(segments["road_type"]), config.ENVIRONMENTAL_SPATIAL_WEIGHT)
        else:
            road_type_weight = np.array([table[rt] for rt in segments["road_type"]])
        w = road_type_weight * base_weight
        probs[category] = w / w.sum()
    return probs


# ---------------------------------------------------------------------------
# 時段抽樣
# ---------------------------------------------------------------------------
def _hour_prob_array(peak_hours, peak_weight, offpeak_weight):
    weights = np.array([
        peak_weight if h in peak_hours else offpeak_weight for h in range(24)
    ], dtype=float)
    return weights / weights.sum()


HOUR_PROBS = {
    "personal_safety": _hour_prob_array(
        config.PERSONAL_SAFETY_NIGHT_HOURS,
        config.PERSONAL_SAFETY_NIGHT_WEIGHT,
        config.PERSONAL_SAFETY_DAY_WEIGHT,
    ),
    "traffic_accident": _hour_prob_array(
        config.TRAFFIC_ACCIDENT_PEAK_HOURS,
        config.TRAFFIC_ACCIDENT_PEAK_WEIGHT,
        config.TRAFFIC_ACCIDENT_OFFPEAK_WEIGHT,
    ),
    "_uniform": np.full(24, 1 / 24),
}


def sample_hour(category, rng):
    probs = HOUR_PROBS.get(category, HOUR_PROBS["_uniform"])
    return int(rng.choice(24, p=probs))


def sample_severity(category, rng):
    dist = config.SEVERITY_DISTRIBUTION[category]
    keys = list(dist.keys())
    p = list(dist.values())
    return rng.choice(keys, p=p)


def sample_tags(category, rng):
    if category != "personal_safety":
        return None
    dist = config.PERSONAL_SAFETY_TAG_DISTRIBUTION
    tag = rng.choice(list(dist.keys()), p=list(dist.values()))
    return [tag]


# ---------------------------------------------------------------------------
# 隨機 ID / 座標小工具（全部走 seeded rng，確保可重現）
# ---------------------------------------------------------------------------
def new_uuid(rng):
    return uuid.UUID(bytes=rng.bytes(16), version=4)


def new_user_id(rng):
    return "syn_" + rng.bytes(8).hex()


def new_display_code(rng):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    idx = rng.integers(0, len(alphabet), size=4)
    return "".join(alphabet[i] for i in idx)


def point_on_segment(geom, rng):
    frac = rng.random()
    return geom.interpolate(frac, normalized=True)


# ---------------------------------------------------------------------------
# 主生成流程
# ---------------------------------------------------------------------------
def generate(rng, segments, cat_probs, neighbor_tree, coords_3826):
    end_date = datetime.fromisoformat(config.SYNTHETIC_DATA_END_DATE)
    start_date = end_date - timedelta(days=config.SYNTHETIC_DATA_DAYS)
    total_days = (end_date - start_date).days

    target_total = int(rng.integers(
        config.SYNTHETIC_TOTAL_REPORTS_RANGE[0],
        config.SYNTHETIC_TOTAL_REPORTS_RANGE[1] + 1,
    ))
    log(f"目標 report 總數: {target_total}（範圍 {config.SYNTHETIC_TOTAL_REPORTS_RANGE}）")

    n_segments = len(segments["segment_id"])
    cumsum = {c: np.cumsum(cat_probs[c]) for c in CATEGORIES}

    def pick_segment(category):
        r = rng.random()
        idx = int(np.searchsorted(cumsum[category], r))
        return min(idx, n_segments - 1)

    reports = []
    events = []
    n_clustered_events = 0

    while len(reports) < target_total:
        category = rng.choice(CATEGORIES, p=list(config.CATEGORY_DISTRIBUTION.values()))
        idx = pick_segment(category)

        day_offset = int(rng.integers(0, total_days))
        hour = sample_hour(category, rng)
        minute = int(rng.integers(0, 60))
        second = int(rng.integers(0, 60))
        created_at = start_date + timedelta(days=day_offset, hours=hour, minutes=minute, seconds=second)

        event_id = new_uuid(rng)
        event_reports = []

        def make_report(seg_idx, ts):
            geom = segments["geometry"][seg_idx]
            severity = sample_severity(category, rng)
            tags = sample_tags(category, rng)
            loc = point_on_segment(geom, rng)
            rec = {
                "report_id": new_uuid(rng),
                "user_id": new_user_id(rng),
                "display_code": new_display_code(rng),
                "category": category,
                "tags": tags,
                "severity": severity,
                "raw_text": None,
                "ai_summary": None,
                "location_wkt": loc.wkt,
                "segment_id": segments["segment_id"][seg_idx],
                "location_confidence": float(rng.uniform(*config.SYNTHETIC_LOCATION_CONFIDENCE_RANGE)),
                "created_at": ts,
                "event_id": event_id,
                "source": "synthetic",
            }
            event_reports.append(rec)
            return rec

        primary = make_report(idx, created_at)
        reports.append(primary)

        is_cluster = rng.random() < config.CLUSTER_EVENT_RATIO
        if is_cluster:
            n_clustered_events += 1
            extra_n = int(rng.integers(
                config.CLUSTER_EXTRA_REPORTS_MIN, config.CLUSTER_EXTRA_REPORTS_MAX + 1
            ))
            neighbor_idxs = neighbor_tree.query_ball_point(
                coords_3826[idx], r=config.CLUSTER_RADIUS_M
            )
            if not neighbor_idxs:
                neighbor_idxs = [idx]
            for _ in range(extra_n):
                nb_idx = int(rng.choice(neighbor_idxs))
                offset_min = rng.uniform(0, config.CLUSTER_TIME_WINDOW_MINUTES)
                extra_ts = created_at + timedelta(minutes=float(offset_min))
                extra = make_report(nb_idx, extra_ts)
                reports.append(extra)

        severities = [r["severity"] for r in event_reports]
        max_severity = max(severities, key=lambda s: SEVERITY_RANK[s])
        events.append({
            "event_id": event_id,
            "category": category,
            "severity": max_severity,
            "segment_id": primary["segment_id"],
            "report_count": len(event_reports),
            "first_at": min(r["created_at"] for r in event_reports),
            "last_at": max(r["created_at"] for r in event_reports),
        })

    log(f"實際產生: {len(reports)} 筆 report, {len(events)} 個 event")
    log(f"群聚事件數: {n_clustered_events} / {len(events)} = {n_clustered_events/len(events)*100:.2f}%")
    return reports, events


def write_to_db(conn, reports, events):
    with conn.cursor() as cur:
        log("清空舊資料（report / event）...")
        cur.execute("TRUNCATE report, event CASCADE")

        event_rows = [
            (
                e["event_id"], e["category"], e["severity"], e["segment_id"],
                "active", e["report_count"], 0, e["first_at"], e["last_at"], 0.0,
            )
            for e in events
        ]
        log(f"寫入 event: {len(event_rows)} 筆")
        execute_values(
            cur,
            """INSERT INTO event
               (event_id, category, severity, segment_id, status,
                report_count, dismiss_count, first_at, last_at, current_risk)
               VALUES %s""",
            event_rows,
        )

        report_rows = [
            (
                r["report_id"], r["user_id"], r["display_code"], r["category"],
                r["tags"], r["severity"], r["raw_text"], r["ai_summary"],
                r["location_wkt"], r["segment_id"], r["location_confidence"],
                r["created_at"], r["event_id"], r["source"],
            )
            for r in reports
        ]
        log(f"寫入 report: {len(report_rows)} 筆")
        execute_values(
            cur,
            """INSERT INTO report
               (report_id, user_id, display_code, category, tags, severity,
                raw_text, ai_summary, location, segment_id, location_confidence,
                created_at, event_id, source)
               VALUES %s""",
            report_rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_GeomFromText(%s),4326),%s,%s,%s,%s,%s)",
        )
    conn.commit()
    log("寫入完成。")


def main():
    rng = np.random.default_rng(config.SYNTHETIC_DATA_SEED)
    conn = get_connection()
    try:
        log(f"載入路段（seed={config.SYNTHETIC_DATA_SEED}）...")
        segments = load_segments(conn)
        coords_3826 = project_centroids_3826(conn, segments["segment_id"])

        log("計算活動密度係數...")
        density_coef, tree, raw_density = compute_activity_density_coef(coords_3826)
        length_weight = segments["length_m"] ** config.ROAD_LENGTH_SAMPLING_EXPONENT
        base_weight = density_coef * length_weight

        cat_probs = build_category_probabilities(segments, base_weight)

        reports, events = generate(rng, segments, cat_probs, tree, coords_3826)
        write_to_db(conn, reports, events)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
