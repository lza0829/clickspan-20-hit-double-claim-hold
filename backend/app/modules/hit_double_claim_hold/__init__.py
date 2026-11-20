"""hit_id 跨会话双占搁置（hit_double_claim_hold）。

正式通道硬约束：hit_id 作为装载身份，全局最多属于一个会话成员集合（含
closed）。二次收流把已入会击点改挂到另一 open 时，冲突点立即进搁置，
写明占用会话；不得写入新成员，也不得从旧会话静默撕走改挂。

本题按 hit_id 争用裁决，不按事件指纹去重。

落库形态（两择一，取形态一）：
  1. 双占搁置表 ``hit_double_claim_holds`` —— 挂 hit_id / 占用会话
     ``claim_session_id`` / 冲突载荷 ``payload_json`` / 批次 ``batch_id``。
  2. 另加触发器 ``hits_claim_immutable`` 作硬约束兜底：hits 行首次写入后
     任何 session_id 改挂一律 ABORT（hits.id 主键本身已保证一个 hit_id
     不可能同时出现在两个会话的成员集合里）。

失败闭路径（两择一，取路径 A）：
  入口先按 hit_id 查占用 -> 命中即在同一事务写搁置、正式通道不动；
  未命中才走正式写入。禁止覆盖改挂、禁止半截改挂。
"""

import json
import uuid
from datetime import datetime, timezone

STATUS = "implemented"
REASON = "hit_id_double_claim"

# 形态一：双占搁置表（禁止只靠内存 set，冲突必须落库）
SCHEMA = """
CREATE TABLE IF NOT EXISTS hit_double_claim_holds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  hold_key TEXT NOT NULL UNIQUE,
  hit_id TEXT NOT NULL,
  claim_session_id TEXT NOT NULL,
  claim_visitor_id TEXT,
  conflict_session_id TEXT,
  conflict_visitor_id TEXT,
  batch_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'released')),
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hdch_hit_id ON hit_double_claim_holds(hit_id);
CREATE INDEX IF NOT EXISTS idx_hdch_batch ON hit_double_claim_holds(batch_id);
"""

# 硬约束兜底：hits 行的会话归属（claim_session_id）首次写入后拒绝改挂。
# 命中即 ABORT 整条语句，调用方无法用「覆盖改挂」冒充解决。
TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS hits_claim_immutable
BEFORE UPDATE OF session_id ON hits
WHEN OLD.session_id IS NOT NULL
 AND NEW.session_id IS NOT NULL
 AND NEW.session_id <> OLD.session_id
BEGIN
  SELECT RAISE(ABORT, 'hit_id claim immutable: session_id 改挂被拒绝');
END;
"""


def init_schema(conn) -> None:
    """幂等建表 / 建触发器。"""
    conn.executescript(SCHEMA)
    conn.executescript(TRIGGER_DDL)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_claim(conn, hit_id: str):
    """按 hit_id 查占用（全局，不分访客/不分通道/含 closed 会话）。

    返回占用行 sqlite3.Row，无占用返回 None。
    """
    return conn.execute(
        """
        SELECT h.id AS hit_id, h.session_id AS claim_session_id,
               h.visitor_id AS claim_visitor_id, h.channel
        FROM hits h
        WHERE h.id = ?
        """,
        (hit_id,),
    ).fetchone()


def hold_double_claim(
    conn,
    *,
    hit_id: str,
    claim_session_id: str,
    batch_id: str,
    payload: dict,
    claim_visitor_id: str | None = None,
    conflict_session_id: str | None = None,
    conflict_visitor_id: str | None = None,
    created_at: str | None = None,
) -> str:
    """把双占冲突点写入搁置表（同事务调用，正式通道不动）。

    每个冲突点一行：搁置键 hold_key = 批次 + hit_id + 短随机后缀，
    同一批次内同一 hit_id 的多次争用各自留痕（二次争用立即搁置，
    冲突载荷逐条可核）；(batch_id, hit_id) 另建非唯一索引便于检索。
    """
    hold_key = f"{batch_id}:{hit_id}:{uuid.uuid4().hex[:8]}"
    ts = created_at or now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO hit_double_claim_holds(
          hold_key, hit_id, claim_session_id, claim_visitor_id,
          conflict_session_id, conflict_visitor_id,
          batch_id, payload_json, status, created_at
        ) VALUES (?,?,?,?,?,?,?,?, 'held', ?)
        """,
        (
            hold_key,
            hit_id,
            claim_session_id,
            claim_visitor_id,
            conflict_session_id,
            conflict_visitor_id,
            batch_id,
            json.dumps(payload, ensure_ascii=False),
            ts,
        ),
    )
    return hold_key


def list_holds(conn, hit_id: str | None = None, batch_id: str | None = None):
    """双占核证只读查询：hit_id、占用会话、冲突批次、搁置键等。"""
    sql = (
        "SELECT * FROM hit_double_claim_holds "
        "WHERE 1=1"
    )
    args: list = []
    if hit_id is not None:
        sql += " AND hit_id = ?"
        args.append(hit_id)
    if batch_id is not None:
        sql += " AND batch_id = ?"
        args.append(batch_id)
    sql += " ORDER BY id"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        out.append(d)
    return out


def count_holds(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM hit_double_claim_holds WHERE status='held'"
    ).fetchone()["c"]


def settle_hit(
    conn,
    *,
    hit: dict,
    target_session_id: str,
    batch_id: str,
) -> dict:
    """收流入口的 hit_id 争用裁决（须在已开启的事务内调用）。

    路径 A（入口查占用 -> 同事务写搁置 / 正式写入）：
      - 已被任一会话占用：写搁置、正式通道完全不动，返回 held；
      - 未占用：正式写入 hits（INSERT，主键兜底防双占），返回 written。

    调用方负责 sessions / session_proofs 等成员物化；本函数只裁决
    hit_id 装载身份本身，保证不会出现新旧会话成员集合同时含该 id。
    """
    hit_id = hit["id"]
    claim = find_claim(conn, hit_id)
    if claim is not None:
        # 二次争用立即搁置；已占用会话成员保持冲突前，不撕走不改挂。
        hold_double_claim(
            conn,
            hit_id=hit_id,
            claim_session_id=claim["claim_session_id"],
            claim_visitor_id=claim["claim_visitor_id"],
            conflict_session_id=target_session_id,
            conflict_visitor_id=hit.get("visitor_id"),
            batch_id=batch_id,
            payload={
                "hit_id": hit_id,
                "incoming": hit,
                "claim": {
                    "session_id": claim["claim_session_id"],
                    "visitor_id": claim["claim_visitor_id"],
                    "channel": claim["channel"],
                },
                "target_session_id": target_session_id,
                "reason": REASON,
            },
        )
        return {
            "hit_id": hit_id,
            "outcome": "held",
            "reason": REASON,
            "claim_session_id": claim["claim_session_id"],
            "conflict_session_id": target_session_id,
        }

    # 未占用：正式写入。纯 INSERT（不用 OR REPLACE），主键冲突会直接报错，
    # 杜绝同事务内覆盖改挂；触发器再拒一切 session_id UPDATE 改挂。
    conn.execute(
        """
        INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
        VALUES (?,?,?,?,?,?, 'primary')
        """,
        (
            hit_id,
            hit["visitor_id"],
            target_session_id,
            hit["event_ts"],
            hit["page_path"],
            hit["event_name"],
        ),
    )
    return {
        "hit_id": hit_id,
        "outcome": "written",
        "session_id": target_session_id,
    }
