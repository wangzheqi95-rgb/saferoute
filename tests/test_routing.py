"""雙路線規劃的冒煙測試（SPEC.md §5）。

不連接資料庫——手動建一個小型合成路網（兩條平行路徑：一條較短但高風險、一條
較長但安全），驗證 fast（α=0）與 safe（α=config.ROUTE_ALPHA_SAFE）確實會選出
不同路徑，不檢查實際 α 數值。
"""
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.routing.safe_route import find_route


def _build_synthetic_graph():
    """A --[risky_short, 100m, high risk]--> B
       A --[safe_long_1, 80m, 低風險] --> C --[safe_long_2, 80m, 低風險] --> B
    短路徑距離更短但風險高；長路徑距離較長但風險低，用來迫使 fast/safe 路線分流。
    """
    G = nx.MultiGraph()
    G.add_edge(
        "A", "B", key="risky_short",
        segment_id="risky_short", length_m=100.0, total_risk=0.9, display_tier="high",
    )
    G.add_edge(
        "A", "C", key="safe_long_1",
        segment_id="safe_long_1", length_m=80.0, total_risk=0.05, display_tier="low",
    )
    G.add_edge(
        "C", "B", key="safe_long_2",
        segment_id="safe_long_2", length_m=80.0, total_risk=0.05, display_tier="low",
    )
    return G


def test_two_routes_differ_with_risk():
    G = _build_synthetic_graph()

    fast = find_route(G, "A", "B", alpha=config.ROUTE_ALPHA_FAST)
    safe = find_route(G, "A", "B", alpha=config.ROUTE_ALPHA_SAFE)

    assert fast["segments"] == ["risky_short"]
    assert safe["segments"] == ["safe_long_1", "safe_long_2"]
    assert fast["node_path"] != safe["node_path"]
    assert safe["avg_risk"] < fast["avg_risk"]
