import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect


def run_batch() -> dict:
    """基座装载：按夹具追加击点；超时切会话 / 脏点隔离留给功能题空桩。"""
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        for hit in hits:
            vid = hit["visitor_id"]
            exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
                    (vid, hit.get("visitor_label") or vid, _now()),
                )
            # 基座：挂到该访客最新 open 会话；无则新建（不做超时切分）
            sess = conn.execute(
                """
                SELECT id FROM sessions
                WHERE visitor_id=? AND status='open'
                ORDER BY started_at DESC LIMIT 1
                """,
                (vid,),
            ).fetchone()
            if sess:
                sid = sess["id"]
            else:
                sid = f"S-{vid}-{uuid.uuid4().hex[:6]}"
                conn.execute(
                    """
                    INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
                    VALUES (?,?,?,?,0,'open')
                    """,
                    (sid, vid, hit["event_ts"], hit["event_ts"]),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hit["id"],
                    vid,
                    sid,
                    hit["event_ts"],
                    hit["page_path"],
                    hit["event_name"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                conn.execute(
                    """
                    UPDATE sessions
                    SET hit_count = hit_count + 1,
                        ended_at = CASE
                          WHEN ended_at IS NULL OR ended_at < ? THEN ?
                          ELSE ended_at END
                    WHERE id=?
                    """,
                    (hit["event_ts"], hit["event_ts"], sid),
                )
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                        (sid,),
                    ).fetchall()
                ]
                sess_row = conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO session_proofs(
                      session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        sid,
                        vid,
                        json.dumps(ids, ensure_ascii=False),
                        sess_row["hit_count"],
                        sess_row["started_at"],
                        sess_row["ended_at"],
                        _now(),
                    ),
                )
        report = {"batch_id": batch_id, "written": written, "input": len(hits)}
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
