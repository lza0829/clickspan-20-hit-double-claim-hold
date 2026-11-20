"""hit_double_claim_hold 验收（SQLite，本地夹具）。

覆盖题面四条：
1. 正式通道硬约束：hit_id 全局最多属于一个会话成员集合；冲突进搁置并
   写明占用会话，不写新成员、不撕走旧成员。
2. 失败闭：入口查占用后同事务写搁置、正式不动（路径 A）。
3. 双占核证只读接口（hit_id / 占用会话 / 冲突批次 / 搁置键）。
4. 夹具先入会再二次争用同 hit_id：搁置且原会话成员不变。
"""

import json

import pytest

from app.modules import hit_double_claim_hold as hdch


def _seed_session_with_hit(conn, hit_id="H1", sid="S-V1-1", vid="V-1001"):
    """先让一个 hit_id 正式入会（占用方）。"""
    conn.execute(
        "INSERT OR IGNORE INTO visitors(id, label, created_at) VALUES (?,?,?)",
        (vid, vid, "2026-11-17T08:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)"
        " VALUES (?,?,?,?,1,'open')",
        (sid, vid, "2026-11-17T08:10:00Z", "2026-11-17T08:10:00Z"),
    )
    conn.execute(
        "INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)"
        " VALUES (?,?,?,?,?,?,'primary')",
        (hit_id, vid, sid, "2026-11-17T08:10:00Z", "/home", "view"),
    )
    conn.commit()
    return sid


def test_find_claim_detects_occupying_session_including_closed(conn):
    sid = _seed_session_with_hit(conn)
    claim = hdch.find_claim(conn, "H1")
    assert claim is not None
    assert claim["claim_session_id"] == sid
    # 含 closed 会话：关闭后占用仍然可查
    conn.execute("UPDATE sessions SET status='closed' WHERE id=?", (sid,))
    conn.commit()
    claim = hdch.find_claim(conn, "H1")
    assert claim is not None and claim["claim_session_id"] == sid
    assert hdch.find_claim(conn, "NOPE") is None


def test_double_claim_is_held_and_original_members_unchanged(conn):
    """第 2/4 条：二次争用立即搁置；原会话成员不变，新会话不出现该 id。"""
    sid_a = _seed_session_with_hit(conn, hit_id="H1", sid="S-V1-1", vid="V-1001")
    conn.execute(
        "INSERT OR IGNORE INTO visitors(id, label, created_at) VALUES (?,?,?)",
        ("V-1002", "访客乙", "2026-11-17T08:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)"
        " VALUES (?,?,?,?,0,'open')",
        ("S-V2-9", "V-1002", "2026-11-17T11:00:00Z", "2026-11-17T11:00:00Z"),
    )
    conn.commit()

    incoming = {
        "id": "H1",
        "visitor_id": "V-1002",
        "event_ts": "2026-11-17T11:15:00Z",
        "page_path": "/landing",
        "event_name": "view",
    }
    conn.execute("BEGIN IMMEDIATE")
    verdict = hdch.settle_hit(
        conn, hit=incoming, target_session_id="S-V2-9", batch_id="B-t-1"
    )
    conn.commit()

    assert verdict["outcome"] == "held"
    assert verdict["reason"] == hdch.REASON
    assert verdict["claim_session_id"] == sid_a
    assert verdict["conflict_session_id"] == "S-V2-9"

    # 搁置表落库（禁止只靠内存 set），字段齐全
    holds = hdch.list_holds(conn, hit_id="H1")
    assert len(holds) == 1
    h = holds[0]
    assert h["hold_key"].startswith("B-t-1:H1:")
    assert h["claim_session_id"] == sid_a
    assert h["conflict_session_id"] == "S-V2-9"
    assert h["batch_id"] == "B-t-1"
    assert h["status"] == "held"
    assert h["payload"]["incoming"]["page_path"] == "/landing"
    assert h["payload"]["claim"]["session_id"] == sid_a

    # 正式通道不动：hits 仍只有一条 H1 且归属旧会话
    rows = conn.execute("SELECT session_id FROM hits WHERE id='H1'").fetchall()
    assert len(rows) == 1 and rows[0]["session_id"] == sid_a
    # 新会话成员集合不含 H1，旧会话成员未少
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM hits WHERE session_id='S-V2-9'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM hits WHERE session_id=?", (sid_a,)
    ).fetchone()["c"] == 1


def test_repeated_contention_same_batch_is_idempotent_hold(conn):
    sid_a = _seed_session_with_hit(conn)
    incoming = {
        "id": "H1",
        "visitor_id": "V-1002",
        "event_ts": "2026-11-17T11:15:00Z",
        "page_path": "/x",
        "event_name": "view",
    }
    conn.execute("BEGIN IMMEDIATE")
    hdch.settle_hit(conn, hit=incoming, target_session_id="S-V2-9", batch_id="B-t-2")
    hdch.settle_hit(conn, hit=incoming, target_session_id="S-V2-9", batch_id="B-t-2")
    conn.commit()
    holds = hdch.list_holds(conn, hit_id="H1", batch_id="B-t-2")
    assert len(holds) == 2  # 每个冲突点各留一条搁置，逐条可核
    assert all(h["hold_key"].startswith("B-t-2:H1:") for h in holds)
    assert conn.execute("SELECT COUNT(*) AS c FROM hits WHERE id='H1'").fetchone()["c"] == 1


def test_unclaimed_hit_is_written_once_and_then_protected(conn):
    """未占用 -> 正式写入；之后同 id 再争用必须搁置。"""
    conn.execute(
        "INSERT OR IGNORE INTO visitors(id, label, created_at) VALUES (?,?,?)",
        ("V-1009", "V-1009", "2026-11-17T08:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)"
        " VALUES (?,?,?,?,0,'open')",
        ("S-V9-1", "V-1009", "2026-11-17T09:00:00Z", "2026-11-17T09:00:00Z"),
    )
    conn.commit()
    hit = {
        "id": "H99",
        "visitor_id": "V-1009",
        "event_ts": "2026-11-17T09:01:00Z",
        "page_path": "/a",
        "event_name": "view",
    }
    conn.execute("BEGIN IMMEDIATE")
    v1 = hdch.settle_hit(conn, hit=hit, target_session_id="S-V9-1", batch_id="B-t-3")
    conn.commit()
    assert v1["outcome"] == "written"

    conn.execute("BEGIN IMMEDIATE")
    v2 = hdch.settle_hit(conn, hit=hit, target_session_id="S-V9-2", batch_id="B-t-3")
    conn.commit()
    assert v2["outcome"] == "held"
    assert v2["claim_session_id"] == "S-V9-1"
    assert conn.execute("SELECT COUNT(*) AS c FROM hits WHERE id='H99'").fetchone()["c"] == 1


def test_trigger_rejects_session_reassignment(conn):
    """硬约束兜底：claim_session_id 首次写入后 UPDATE 改挂被 ABORT。"""
    sid_a = _seed_session_with_hit(conn, hit_id="H7", sid="S-A", vid="V-1001")
    conn.execute(
        "INSERT OR IGNORE INTO visitors(id, label, created_at) VALUES (?,?,?)",
        ("V-1002", "访客乙", "2026-11-17T08:00:00Z"),
    )
    conn.execute(
        "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)"
        " VALUES (?,?,?,?,0,'open')",
        ("S-B", "V-1002", "2026-11-17T11:00:00Z", "2026-11-17T11:00:00Z"),
    )
    conn.commit()
    with pytest.raises(Exception):
        conn.execute("UPDATE hits SET session_id='S-B' WHERE id='H7'")
    conn.rollback()
    row = conn.execute("SELECT session_id FROM hits WHERE id='H7'").fetchone()
    assert row["session_id"] == sid_a  # 改挂失败，归属不变


def test_ingest_fixture_holds_double_claims_and_keeps_members(fixtures_dir, clean_db):
    """端到端：ingest_double_claim.json 先入会再二次争用同 hit_id。"""
    from app.services import ingest as ingest_svc

    result = ingest_svc.run_batch(fixtures_dir / "ingest_double_claim.json")
    assert result["ok"] is True, result
    report = result["report"]
    assert report["batch_id"] == "B-double-claim-001"
    assert report["written"] == 2  # H1 首次、H2 各一条正式
    assert report["held"] == 2     # H1 的第二、第三次争用全部搁置

    from app.db import connect

    c = connect()
    try:
        # 核证四要素齐
        holds = hdch.list_holds(c, hit_id="H1")
        assert len(holds) == 2
        for h in holds:
            assert h["hit_id"] == "H1"
            assert h["claim_session_id"]
            assert h["batch_id"] == "B-double-claim-001"
            assert h["hold_key"]
        # 全局成员键唯一：hits 里 H1 只出现一次，且不存在任何会话双挂
        n = c.execute("SELECT COUNT(*) AS c FROM hits WHERE id='H1'").fetchone()["c"]
        assert n == 1
        dup = c.execute(
            "SELECT id FROM hits GROUP BY id HAVING COUNT(DISTINCT session_id) > 1"
        ).fetchall()
        assert dup == []
        # 跨访客争用：H1 占用方是 V-1001 的会话，V-1002 会话不含 H1
        claim_sid = holds[0]["claim_session_id"]
        owner = c.execute(
            "SELECT visitor_id FROM sessions WHERE id=?", (claim_sid,)
        ).fetchone()
        assert owner["visitor_id"] == "V-1001"
        v2_sessions = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM sessions WHERE visitor_id='V-1002'"
            ).fetchall()
        ]
        assert v2_sessions
        leaked = c.execute(
            f"SELECT COUNT(*) AS c FROM hits WHERE id='H1' AND session_id IN "
            f"({','.join('?' * len(v2_sessions))})",
            v2_sessions,
        ).fetchone()["c"]
        assert leaked == 0
    finally:
        c.close()


def test_api_double_claims_endpoint_readonly(fixtures_dir, clean_db):
    """第 3 条：只读核证接口 + 搁置页协同（/api/quarantine 合并双占）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import ingest as ingest_svc

    ingest_svc.run_batch(fixtures_dir / "ingest_double_claim.json")
    client = TestClient(app)
    res = client.get("/api/double-claims")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert {"hit_id", "claim_session_id", "batch_id", "hold_key"} <= set(data[0])

    filtered = client.get("/api/double-claims", params={"hit_id": "H1"}).json()
    assert len(filtered) == 2
    assert client.get("/api/double-claims", params={"hit_id": "H2"}).json() == []

    q = client.get("/api/quarantine").json()
    tagged = [r for r in q if r.get("source") == "hit_double_claim_hold"]
    assert len(tagged) == 2
    assert all(r["claim_session_id"] and r["hold_key"] for r in tagged)

    overview = client.get("/api/overview").json()
    assert overview["double_claim_holds"] == 2
