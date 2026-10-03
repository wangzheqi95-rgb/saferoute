"""M1：OSM 路網匯入（SPEC.md §2）。

下載台北市文山區的步行路網（osmnx, network_type="walk"），轉換為
road_segment / road_node / segment_topology 三張表並寫入資料庫。
"""
import sys
from pathlib import Path

import networkx as nx
import osmnx as ox
import pandas as pd
from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection

FOOTWAY_HIGHWAY_TYPES = {"footway", "pedestrian"}
PRIMARY_LIKE_HIGHWAY_TYPES = {"primary", "secondary", "tertiary"}


def _first(value):
    """osmnx 的欄位值可能是純值，也可能是 list（簡化合併後的結果），取第一個。"""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _name_contains_alley_keyword(name) -> bool:
    names = name if isinstance(name, list) else [name]
    for n in names:
        if isinstance(n, str):
            for kw in config.ROAD_TYPE_ALLEY_NAME_KEYWORDS:
                if kw in n:
                    return True
    return False


def _parse_width(width) -> float | None:
    w = _first(width)
    if w is None or (isinstance(w, float) and pd.isna(w)):
        return None
    try:
        # OSM width 有時帶單位字串，如 "3.5" 或 "3.5 m"
        return float(str(w).split()[0])
    except (ValueError, IndexError):
        return None


def classify_road_type(name, highway, width, service) -> str:
    """依使用者確認的優先序判斷 road_type（見 config.py 註解）。"""
    if _name_contains_alley_keyword(name):
        return "alley"

    hw = _first(highway)

    if hw == "service":
        svc = _first(service)
        if svc in config.ROAD_TYPE_SERVICE_ACCESS_SUBTYPES:
            return "service_access"
        return "service_alley"  # service=alley 或無子標籤／其他子標籤
    if hw == "unclassified":
        return "residential"
    if hw in ("residential", "living_street"):
        return "residential"
    if hw == "steps":
        return "steps"
    if hw == "path":
        return "path"
    if hw in FOOTWAY_HIGHWAY_TYPES:
        return "footway"
    if hw in PRIMARY_LIKE_HIGHWAY_TYPES:
        return hw

    road_type = "residential"  # 其他 -> residential（含未列類型的 fallback）

    width_m = _parse_width(width)
    if width_m is not None and width_m < config.ROAD_TYPE_ALLEY_WIDTH_THRESHOLD_M:
        road_type = "alley"

    return road_type


def build_segment_ids(edges: pd.DataFrame) -> pd.Series:
    """同一 OSM way（osmid）若被路口拆成多條 edge，依 (u,v) 排序後加後綴 _1, _2..."""
    base_ids = edges["osmid"].map(lambda v: str(_first(v)))
    order_key = edges.index.to_frame(index=False)[["u", "v"]].reset_index(drop=True)
    tmp = pd.DataFrame({"base_id": base_ids.values, "u": order_key["u"], "v": order_key["v"]})
    tmp = tmp.sort_values(["base_id", "u", "v"]).reset_index()  # index = original position
    tmp["rank"] = tmp.groupby("base_id").cumcount() + 1
    counts = tmp.groupby("base_id")["base_id"].transform("count")

    segment_ids = pd.Series(index=tmp["index"], dtype=object)
    for pos, base_id, rank, count in zip(tmp["index"], tmp["base_id"], tmp["rank"], counts):
        segment_ids.loc[pos] = base_id if count == 1 else f"{base_id}_{rank}"

    segment_ids = segment_ids.sort_index()
    segment_ids.index = edges.index
    return segment_ids


def fetch_graph() -> nx.MultiDiGraph:
    print(f"下載 OSM 路網：{config.OSM_PLACE_NAME}（network_type={config.OSM_NETWORK_TYPE}）")
    G = ox.graph_from_place(config.OSM_PLACE_NAME, network_type=config.OSM_NETWORK_TYPE)
    print(f"下載完成：{G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    return G


def dedupe_bidirectional_edges(edges: pd.DataFrame) -> pd.DataFrame:
    """osmnx 對步行網路的每條實體路段都存成 (u,v) 與 (v,u) 兩條有向 edge
    （因為 network_type="walk" 一律 oneway=False）。同一物理路段只需入庫一次，
    用 (min(u,v), max(u,v), key) 當去重 key，保留先出現的方向。
    """
    pair_key = edges.apply(lambda r: (min(r.u, r.v), max(r.u, r.v), r.key), axis=1)
    return edges.loc[~pair_key.duplicated(keep="first")].reset_index(drop=True)


def import_network():
    G = fetch_graph()
    nodes, edges = ox.graph_to_gdfs(G)

    edges = edges.reset_index()  # 展開 u, v, key 成一般欄位，並保留原始順序
    before = len(edges)
    edges = dedupe_bidirectional_edges(edges)
    print(f"去除雙向重複 edge：{before} -> {len(edges)}")

    edges_3826 = edges.set_geometry("geometry").to_crs(config.CRS_TWD97)
    length_m = edges_3826.geometry.length

    segment_ids = build_segment_ids(edges.set_index(["u", "v", "key"]))
    segment_ids = segment_ids.reset_index(drop=True)

    road_types = [
        classify_road_type(
            row.get("name"), row.get("highway"), row.get("width"), row.get("service")
        )
        for _, row in edges.iterrows()
    ]

    road_segment_rows = []
    segment_topology_rows = []
    for i, row in edges.iterrows():
        seg_id = segment_ids.iloc[i]
        name = _first(row.get("name"))
        name = name if isinstance(name, str) else None
        geom_wkt = row["geometry"].wkt
        road_segment_rows.append((seg_id, name, geom_wkt, float(length_m.iloc[i]), road_types[i]))
        segment_topology_rows.append((seg_id, str(row["u"]), str(row["v"])))

    road_node_rows = [
        (str(node_id), geom.wkt) for node_id, geom in zip(nodes.index, nodes["geometry"])
    ]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("清空舊資料（road_segment / road_node / segment_topology）...")
            cur.execute("TRUNCATE segment_topology, road_segment, road_node CASCADE")

            print(f"寫入 road_node：{len(road_node_rows)} 筆")
            execute_values(
                cur,
                "INSERT INTO road_node (node_id, geometry) VALUES %s",
                road_node_rows,
                template="(%s, ST_SetSRID(ST_GeomFromText(%s), 4326))",
            )

            print(f"寫入 road_segment：{len(road_segment_rows)} 筆")
            execute_values(
                cur,
                """INSERT INTO road_segment
                   (segment_id, name, geometry, length_m, road_type)
                   VALUES %s""",
                road_segment_rows,
                template="(%s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s, %s)",
            )

            print(f"寫入 segment_topology：{len(segment_topology_rows)} 筆")
            execute_values(
                cur,
                """INSERT INTO segment_topology (segment_id, from_node, to_node)
                   VALUES %s""",
                segment_topology_rows,
            )
        conn.commit()
        print("匯入完成。")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import_network()
