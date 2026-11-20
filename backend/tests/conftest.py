import os
import sys
import tempfile
from pathlib import Path

import pytest

# 在导入 app.* 之前把数据库指到临时目录，保证本地夹具跑通且不污染开发库。
_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name
os.environ.setdefault(
    "FIXTURES_DIR", str(Path(__file__).resolve().parent.parent.parent / "fixtures")
)

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def _fresh_schema():
    """删库重建：空 schema + 双占表/触发器，不灌种子。"""
    from app import seed
    from app.db import DB_PATH, connect
    from app.modules import hit_double_claim_hold as hdch

    if DB_PATH.exists():
        DB_PATH.unlink()
    c = connect()
    c.executescript(seed.SCHEMA)
    hdch.init_schema(c)
    c.commit()
    return c


@pytest.fixture()
def conn():
    """每个测试独立的空库连接（SQLite 临时文件，测试间零串数据）。"""
    c = _fresh_schema()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def fixtures_dir():
    from app.db import FIXTURES_DIR

    return FIXTURES_DIR


@pytest.fixture()
def clean_db():
    """端到端用：交给应用自身 connect() 的空库（TestClient / service 共用
    同一个 DB_PATH）；用完删库恢复。"""
    from app.db import DB_PATH

    _fresh_schema().close()
    yield DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
