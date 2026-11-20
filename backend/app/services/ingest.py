import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import hit_double_claim_hold as hdch


def run_batch(fixture_path=None) -> dict:
    """收流：按夹具追加击点。

    hit_id 争用裁决（hit_double_claim_hold，失败闭路径 A）：
    入口按 hit_id 查占用 -> 已占用则同事务写双占搁置、正式通道不动；
    未占用才正式入会并做成员物化（计数 / 证明）。
    """
    path = fixture_path or (FIXTURES_DIR / "ingest_batch.json")
    if not path.exists():
        return {"ok": False, "error": f"缺少夹具 {path}"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        held = []
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

            # 入口裁决：已被任一会话占用 -> 搁置，正式通道不动；否则正式写入。
            verdict = hdch.settle_hit(conn, hit=hit, target_session_id=sid, batch_id=batch_id)
            if verdict["outcome"] == "held":
                held.append(
                    {
                        "hit_id": verdict["hit_id"],
                        "reason": verdict["reason"],
                        "claim_session_id": verdict["claim_session_id"],
                        "conflict_session_id": verdict["conflict_session_id"],
                    }
                )
                # 不得从旧会话撕走、不得给新会话加成员：sessions /
                # session_proofs 一律不动，直接处理下一条。
                continue

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
        report = {
            "batch_id": batch_id,
            "written": written,
            "held": len(held),
            "input": len(hits),
            "double_claims": held,
        }
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
