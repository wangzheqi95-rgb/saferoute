"""M4：安全路線（SPEC.md §5）。

邊權重：weight = length_m × (1 + α × total_risk^p)，p=ROUTE_RISK_EXPONENT。
指數 p>1 讓輕微風險幾乎不影響路線，只對高風險路段強力避開。α=0 為最快路線。
路網用 MultiGraph——segment_topology 保留了同一對節點間的平行路段（109 組節點對
有多條路段，見 reports/M4_report.md），若用普通 Graph 平行邊會互相覆蓋、路段會
從路網中消失。平行邊之間，權重函式取最小代價者。
"""
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.db.connection import get_connection

TIER_RANK = {"low": 0, "medium": 1, "high": 2}


def build_graph(conn, segment_risk_map=None):
    """建 MultiGraph。segment_risk_map 未提供時，讀 segment_risk 表目前的值；
    提供時（例如某個 as_of 的風險快照）優先採用它，供回溯測試使用。
    """
    if segment_risk_map is None:
        cur = conn.cursor()
        cur.execute("SELECT segment_id, total_risk, display_tier FROM segment_risk")
        segment_risk_map = {
            seg_id: {"total_risk": tr, "display_tier": tier}
            for seg_id, tr, tier in cur.fetchall()
        }

    cur = conn.cursor()
    cur.execute(
        """
        SELECT st.segment_id, st.from_node, st.to_node, rs.length_m
        FROM segment_topology st JOIN road_segment rs ON rs.segment_id = st.segment_id
        """
    )
    rows = cur.fetchall()

    G = nx.MultiGraph()
    for segment_id, from_node, to_node, length_m in rows:
        risk_info = segment_risk_map.get(segment_id, {"total_risk": 0.0, "display_tier": "low"})
        G.add_edge(
            from_node, to_node,
            key=segment_id,
            segment_id=segment_id,
            length_m=length_m,
            total_risk=risk_info["total_risk"],
            display_tier=risk_info["display_tier"],
        )

    return G


def make_weight_fn(alpha: float):
    """回傳給 networkx 用的權重函式。因為 G 是 MultiGraph，networkx 若偵測到
    weight 是 callable 會直接把「該節點對的所有平行邊字典」傳進來（key -> attrs），
    不會自動取 min，所以這裡自己處理：平行邊之間取權重最小者。
    """
    p = config.ROUTE_RISK_EXPONENT

    def weight_fn(u, v, edge_data):
        if edge_data and all(isinstance(val, dict) for val in edge_data.values()):
            candidates = edge_data.values()
        else:
            candidates = [edge_data]
        return min(d["length_m"] * (1 + alpha * d["total_risk"] ** p) for d in candidates)

    return weight_fn


def _path_edge_for_alpha(G, u, v, alpha):
    p = config.ROUTE_RISK_EXPONENT
    edges = G.get_edge_data(u, v)
    best_key = min(edges, key=lambda k: edges[k]["length_m"] * (1 + alpha * edges[k]["total_risk"] ** p))
    return edges[best_key]


def find_route(G, source, target, alpha: float):
    """回傳這條路線的完整資訊：node 路徑、segment 清單、距離、時間、平均風險、最高 tier。"""
    weight_fn = make_weight_fn(alpha)
    node_path = nx.shortest_path(G, source, target, weight=weight_fn, method="dijkstra")

    segments = []
    tiers = []
    total_length = 0.0
    weighted_risk_sum = 0.0
    max_tier_rank = 0
    max_risk = 0.0
    high_length = 0.0
    medium_plus_length = 0.0

    for u, v in zip(node_path, node_path[1:]):
        edge = _path_edge_for_alpha(G, u, v, alpha)
        segments.append(edge["segment_id"])
        tiers.append(edge["display_tier"])
        total_length += edge["length_m"]
        weighted_risk_sum += edge["length_m"] * edge["total_risk"]
        max_tier_rank = max(max_tier_rank, TIER_RANK[edge["display_tier"]])
        max_risk = max(max_risk, edge["total_risk"])
        if edge["display_tier"] == "high":
            high_length += edge["length_m"]
        if edge["display_tier"] in ("medium", "high"):
            medium_plus_length += edge["length_m"]

    avg_risk = weighted_risk_sum / total_length if total_length > 0 else 0.0
    max_tier = [k for k, v in TIER_RANK.items() if v == max_tier_rank][0]
    walk_time_min = total_length / config.WALKING_SPEED_MPS / 60

    return {
        "node_path": node_path,
        "segments": segments,
        "tiers": tiers,
        "length_m": total_length,
        "walk_time_min": walk_time_min,
        "avg_risk": avg_risk,
        "max_risk": max_risk,
        "high_length_m": high_length,
        "medium_plus_length_m": medium_plus_length,
        "max_tier": max_tier,
    }


def compare_routes(G, source, target, alpha: float):
    """回傳 fast(α=0) 與 safe(給定 α) 兩條路線的完整比較結果。"""
    fast = find_route(G, source, target, config.ROUTE_ALPHA_FAST)
    safe = find_route(G, source, target, alpha)

    fast_len_map = {s: _edge_length_for_segment(G, fast, s) for s in fast["segments"]}
    common_segments = set(fast["segments"]) & set(safe["segments"])
    common_length = sum(fast_len_map[s] for s in common_segments)
    overlap_pct = (common_length / fast["length_m"] * 100) if fast["length_m"] > 0 else 0.0

    detour_pct = (
        (safe["length_m"] / fast["length_m"] - 1) * 100 if fast["length_m"] > 0 else 0.0
    )
    risk_reduction_pct = (
        (fast["avg_risk"] - safe["avg_risk"]) / fast["avg_risk"] * 100
        if fast["avg_risk"] > 0 else 0.0
    )

    note = None
    if overlap_pct > config.ROUTE_OVERLAP_THRESHOLD * 100:
        note = "此區域無明顯更安全的替代路線"

    return {
        "fast": fast,
        "safe": safe,
        "overlap_pct": overlap_pct,
        "detour_pct": detour_pct,
        "risk_reduction_pct": risk_reduction_pct,
        "note": note,
    }


def _edge_length_for_segment(G, route: dict, segment_id: str) -> float:
    idx = route["segments"].index(segment_id)
    u, v = route["node_path"][idx], route["node_path"][idx + 1]
    edges = G.get_edge_data(u, v)
    for attrs in edges.values():
        if attrs["segment_id"] == segment_id:
            return attrs["length_m"]
    return 0.0


if __name__ == "__main__":
    conn = get_connection()
    G = build_graph(conn)
    print(f"[safe_route] 建圖完成：{G.number_of_nodes()} nodes, {G.number_of_edges()} edges (MultiGraph)")
    conn.close()
