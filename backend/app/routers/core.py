import json

from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import hit_double_claim_hold as hdch
from app.seed import idle_timeout_sec
from app.services import ingest as ingest_svc
from app.services import sessions as sess_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "visitors": conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"],
            "sessions": conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
            "hits_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hits WHERE channel='primary'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
            "double_claim_holds": hdch.count_holds(conn),
            "idle_timeout_sec": idle_timeout_sec(conn),
        }
    finally:
        conn.close()


@router.get("/visitors")
def list_visitors():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, label, created_at FROM visitors ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions")
def list_sessions(visitor_id: str | None = None):
    conn = connect()
    try:
        return sess_svc.list_sessions(conn, visitor_id)
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    conn = connect()
    try:
        detail = sess_svc.session_detail(conn, session_id)
        if not detail:
            raise HTTPException(404, "会话不存在")
        return detail
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    """搁置页：通用搁置 + 双占搁置（协同展示，双占记录带占用会话）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, visitor_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        out = [dict(r) for r in rows]
        for h in hdch.list_holds(conn):
            out.append(
                {
                    "id": h["id"],
                    "source": "hit_double_claim_hold",
                    "visitor_id": h["conflict_visitor_id"],
                    "reason": "hit_id_double_claim",
                    "member_json": json.dumps(h["payload"], ensure_ascii=False),
                    "created_at": h["created_at"],
                    "hit_id": h["hit_id"],
                    "hold_key": h["hold_key"],
                    "claim_session_id": h["claim_session_id"],
                    "conflict_session_id": h["conflict_session_id"],
                    "batch_id": h["batch_id"],
                    "status": h["status"],
                }
            )
        return out
    finally:
        conn.close()


@router.get("/double-claims")
def double_claims(hit_id: str | None = None, batch_id: str | None = None):
    """双占核证（只读）：hit_id、占用会话、冲突批次、搁置键。

    可按 hit_id / batch_id 过滤；不提供任何写入或改挂入口。
    """
    conn = connect()
    try:
        return hdch.list_holds(conn, hit_id=hit_id, batch_id=batch_id)
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/session-proofs")
def session_proofs(session_id: str | None = None):
    conn = connect()
    try:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"idle_timeout_sec": idle_timeout_sec(conn)}
    finally:
        conn.close()


@router.put("/settings/idle-timeout")
def put_idle_timeout(body: dict):
    sec = int(body.get("idle_timeout_sec", 1800))
    if sec < 60:
        raise HTTPException(400, "空闲超时至少 60 秒")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            (str(sec),),
        )
        conn.commit()
        return {"idle_timeout_sec": sec}
    finally:
        conn.close()
