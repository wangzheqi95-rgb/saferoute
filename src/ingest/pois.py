"""M7：文山區 OSM 地標下載（供 /search 搜尋索引使用，SPEC.md §9）。

用 osmnx 下載學校、捷運站、公園、超商等 amenity/POI，寫入 poi 表。
不使用任何外部地理編碼服務——地標資料本身也來自 OSM。
"""
import sys
from pathlib import Path

import osmnx as ox
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection


def log(msg):
    print(f"[pois] {msg}")


def fetch_pois():
    log(f"下載 OSM 地標：{config.OSM_PLACE_NAME}，tags={config.OSM_POI_TAGS}")
    gdf = ox.features_from_place(config.OSM_PLACE_NAME, tags=config.OSM_POI_TAGS)
    log(f"下載完成：{len(gdf)} 筆（含未命名，稍後過濾）")
    return gdf


def classify_category(row):
    for key in ("amenity", "railway", "leisure", "shop"):
        val = row.get(key)
        if isinstance(val, str) and val in config.OSM_POI_TAGS.get(key, []):
            return val
    return "other"


def to_point(geom):
    if geom.geom_type == "Point":
        return geom
    return geom.centroid


def import_pois():
    gdf = fetch_pois()
    gdf = gdf[gdf["name"].notna()]
    log(f"有名稱的地標：{len(gdf)} 筆")

    rows = []
    seen_ids = set()
    for idx, row in gdf.iterrows():
        # osmnx 2.x 回傳 MultiIndex (element, id)；node 跟 way 可能有相同數字 id，
        # 用 element 前綴避免碰撞。
        poi_id = f"{idx[0]}_{idx[1]}" if isinstance(idx, tuple) else str(idx)
        if poi_id in seen_ids:
            continue
        seen_ids.add(poi_id)
        point = to_point(row["geometry"])
        category = classify_category(row)
        rows.append((poi_id, row["name"], category, point.wkt))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            log("清空舊資料（poi）...")
            cur.execute("TRUNCATE poi")
            log(f"寫入 poi：{len(rows)} 筆")
            execute_values(
                cur,
                "INSERT INTO poi (poi_id, name, category, geometry) VALUES %s",
                rows,
                template="(%s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326))",
            )
        conn.commit()
        log("匯入完成。")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import_pois()
