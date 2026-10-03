"""B7：事件合併判定（SPEC.md §4.4）。

粗篩（find_candidate_events）是固定的規格邏輯，任何 Decider 都共用同一批候選。
MergeDecider 介面負責在粗篩通過的候選中做最終判定；規則層（本檔案的
RuleBasedMergeDecider）是目前唯一實作，直接選最近活躍的候選。M6 要接 LLM 時，
寫一個新的 Decider（用 report 的 raw_text 在候選裡重新篩選/否決）即可，
find_candidate_events() 不用改。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import config
from src.engine.decay import SEVERITY_RANK


def find_candidate_events(conn, category: str, segment_id: str, location_wkt: str, as_of):
    """粗篩：同 category、status=active、與 last_at 差距在時間窗內、且
    （同路段 或 與該事件最新一筆 report 的距離在 config.MERGE_DISTANCE_M 內）的事件。
    附帶算出每個候選與新回報的距離（公尺），供 decide() 的 tie-break 用
    （B7 決議：多個候選時先比距離，最近者優先；距離相同才比 last_at 最新）。
    """
    window_minutes = config.MERGE_TIME_WINDOW_MINUTES.get(category, 30)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.event_id, e.segment_id, e.severity, e.last_at, e.report_count,
               ST_Distance(
                   (SELECT r.location FROM report r WHERE r.event_id = e.event_id
                    ORDER BY r.created_at DESC LIMIT 1)::geography,
                   ST_SetSRID(ST_GeomFromText(%s), 4326)::geography
               ) AS distance_m
        FROM event e
        WHERE e.category = %s AND e.status = 'active'
          AND e.last_at >= %s - (%s || ' minutes')::interval
          AND (
            e.segment_id = %s
            OR ST_DWithin(
                (SELECT r.location FROM report r WHERE r.event_id = e.event_id
                 ORDER BY r.created_at DESC LIMIT 1)::geography,
                ST_SetSRID(ST_GeomFromText(%s), 4326)::geography,
                %s
            )
          )
        """,
        (location_wkt, category, as_of, window_minutes, segment_id, location_wkt, config.MERGE_DISTANCE_M),
    )
    return [
        {"event_id": eid, "segment_id": sid, "severity": sev, "last_at": last_at,
         "report_count": rc, "distance_m": dist}
        for eid, sid, sev, last_at, rc, dist in cur.fetchall()
    ]


class MergeDecider:
    """最終判定介面。輸入已通過粗篩的候選清單，回傳要併入的 event_id，或 None
    （代表都不符合，建立新事件）。"""

    def decide(self, candidates: list[dict], report: dict):
        raise NotImplementedError


class RuleBasedMergeDecider(MergeDecider):
    """規則層預設實作：粗篩本身就是完整判準，不看 raw_text（B7 決議，M6 接 LLM
    Decider 時才會用文字內容做最終判定或否決）。多個候選時 tie-break：距離最近
    優先，距離相同則取 last_at 最新者（B7 決議）。"""

    def decide(self, candidates: list[dict], report: dict):
        if not candidates:
            return None
        best = min(candidates, key=lambda c: (c["distance_m"], -c["last_at"].timestamp()))
        return best["event_id"]


def max_severity(a: str, b: str) -> str:
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b
