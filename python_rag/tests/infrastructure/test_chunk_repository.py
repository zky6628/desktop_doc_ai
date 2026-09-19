# -*- coding: utf-8 -*-
"""切片与流水线配置仓储测试：按序号 upsert、定位关系与配置复用"""
import pytest

from app.domain.chunking import (
    CHUNKING_CONFIG_TYPE,
    Chunk,
    chunking_config_json,
    make_chunk,
)
from app.domain.errors import EntityNotFoundError
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteChunkRepository,
    SQLiteConfigRepository,
)

from .schema_helpers import (
    fresh_db,
    insert_block,
    insert_document,
    insert_index_version,
    insert_kb,
    insert_version,
)


@pytest.fixture()
def env(tmp_path):
    """应用全部迁移的临时库 + 切片/配置仓储"""

    class Env:
        def __init__(self, conn) -> None:
            self.conn = conn
            self.chunks = SQLiteChunkRepository(conn)
            self.configs = SQLiteConfigRepository(conn)

        def new_index_version(self, block_count: int = 1) -> tuple[str, list[str]]:
            """在外键链路上创建 staging 索引版本与解析块，返回（索引 ID, 块 ID）"""
            kb_id = insert_kb(self.conn, "kb")
            doc_id = insert_document(self.conn, kb_id, "a" * 64)
            version_id = insert_version(self.conn, doc_id)
            index_id = insert_index_version(self.conn, version_id)
            block_ids = [
                insert_block(self.conn, version_id, ordinal=number)
                for number in range(block_count)
            ]
            return index_id, block_ids

    db_path, _ = fresh_db(tmp_path, name="chunk_repo.db")
    conn = connect(db_path)
    yield Env(conn)
    conn.close()


def _chunk(
    ordinal: int,
    *,
    parent: int | None = None,
    content: str = "内容",
    block_ids: tuple[str, ...] = (),
) -> Chunk:
    """构造切片值对象（定位关系默认为空，按需传入真实落库块主键）"""
    return make_chunk(
        ordinal=ordinal,
        content=content,
        parent_ordinal=parent,
        section_path="第一章",
        page_start=1,
        page_end=1,
        block_ids=block_ids,
    )


def test_write_persists_chunks_links_and_parent_pointers(env):
    """切片与定位关系落库，子切片父指针解析为实际主键"""
    index_id, block_ids = env.new_index_version(block_count=2)
    chunks = [
        _chunk(0, content="父内容", block_ids=tuple(block_ids)),
        _chunk(1, parent=0, content="子内容", block_ids=(block_ids[0],)),
    ]

    written = env.chunks.replace_index_chunks(index_id, chunks)

    assert written == 2
    rows = env.conn.execute(
        "SELECT id, ordinal, content, parent_chunk_id, content_hash"
        " FROM chunks WHERE index_version_id = ? ORDER BY ordinal",
        (index_id,),
    ).fetchall()
    assert [row[1] for row in rows] == [0, 1]
    assert rows[0][2] == "父内容"
    assert rows[1][2] == "子内容"
    # 子切片的父指针解析为父切片实际主键
    assert rows[1][3] == rows[0][0]
    # 定位关系按切片落库：父链接全部素材块，子链接自身素材块
    assert env.conn.execute(
        "SELECT COUNT(*) FROM chunk_block_links WHERE chunk_id = ?", (rows[0][0],)
    ).fetchone()[0] == 2
    assert env.conn.execute(
        "SELECT COUNT(*) FROM chunk_block_links WHERE chunk_id = ?", (rows[1][0],)
    ).fetchone()[0] == 1


def test_replay_preserves_ids_without_duplicates(env):
    """重放 upsert：行数不变且主键保持首次写入值"""
    index_id, block_ids = env.new_index_version(block_count=1)
    chunks = [
        _chunk(0, block_ids=(block_ids[0],)),
        _chunk(1, parent=0, block_ids=(block_ids[0],)),
    ]

    env.chunks.replace_index_chunks(index_id, chunks)
    ids_before = [
        row[0]
        for row in env.conn.execute(
            "SELECT id FROM chunks WHERE index_version_id = ? ORDER BY ordinal",
            (index_id,),
        ).fetchall()
    ]
    env.chunks.replace_index_chunks(index_id, chunks)

    rows = env.conn.execute(
        "SELECT id, ordinal FROM chunks WHERE index_version_id = ? ORDER BY ordinal",
        (index_id,),
    ).fetchall()
    assert [row[0] for row in rows] == ids_before
    assert env.conn.execute(
        "SELECT COUNT(*) FROM chunk_block_links"
        " WHERE chunk_id IN (SELECT id FROM chunks WHERE index_version_id = ?)",
        (index_id,),
    ).fetchone()[0] == 2


def test_stale_ordinal_rows_cleaned_on_replay(env):
    """超界残留行在重放时清除（关系行随级联消失）"""
    index_id, _ = env.new_index_version(block_count=1)
    env.chunks.replace_index_chunks(index_id, [_chunk(0), _chunk(1, parent=0)])
    # 模拟中断残留的高序号切片
    env.conn.execute(
        "INSERT INTO chunks"
        " (id, index_version_id, parent_chunk_id, ordinal, content, token_count,"
        "  section_path, page_start, page_end, content_hash, metadata_json)"
        " VALUES ('stale', ?, NULL, 5, '残留', NULL, NULL, NULL, NULL, 'h', NULL)",
        (index_id,),
    )

    env.chunks.replace_index_chunks(index_id, [_chunk(0), _chunk(1, parent=0)])

    ordinals = [
        row[0]
        for row in env.conn.execute(
            "SELECT ordinal FROM chunks WHERE index_version_id = ? ORDER BY ordinal",
            (index_id,),
        ).fetchall()
    ]
    assert ordinals == [0, 1]
    assert env.conn.execute(
        "SELECT COUNT(*) FROM chunk_block_links WHERE chunk_id = 'stale'"
    ).fetchone()[0] == 0


def test_missing_index_version_rejected(env):
    """索引版本不存在时写入切片抛领域错误"""
    with pytest.raises(EntityNotFoundError):
        env.chunks.replace_index_chunks(
            "01900000-0000-7000-8000-000000000000", [_chunk(0)]
        )


def test_get_chunk_anchors_returns_facts_by_ids(env):
    """按主键批量读取锚点事实：归属索引版本与序号"""
    index_id, _ = env.new_index_version(block_count=1)
    env.chunks.replace_index_chunks(index_id, [_chunk(0), _chunk(1, parent=0)])
    rows = env.conn.execute(
        "SELECT id FROM chunks WHERE index_version_id = ? ORDER BY ordinal",
        (index_id,),
    ).fetchall()

    anchors = env.chunks.get_chunk_anchors([rows[0][0], rows[1][0]])

    by_id = {anchor.chunk_id: anchor for anchor in anchors}
    assert by_id[rows[0][0]].index_version_id == index_id
    assert by_id[rows[0][0]].ordinal == 0
    assert by_id[rows[1][0]].ordinal == 1


def test_get_chunk_anchors_missing_ids_produce_no_rows(env):
    """不存在的主键不产生结果行（命中回查的丢弃判定依据）"""
    index_id, _ = env.new_index_version(block_count=1)
    env.chunks.replace_index_chunks(index_id, [_chunk(0)])
    existing = env.conn.execute(
        "SELECT id FROM chunks WHERE index_version_id = ?", (index_id,)
    ).fetchone()[0]

    anchors = env.chunks.get_chunk_anchors([existing, "missing-chunk-id"])

    assert [anchor.chunk_id for anchor in anchors] == [existing]


def test_get_chunk_anchors_empty_input_returns_empty(env):
    """空输入直接返回空列表"""
    assert env.chunks.get_chunk_anchors([]) == []


def test_config_reused_for_same_content_and_new_version_for_change(env):
    """同内容配置幂等复用；内容变化创建类型内递增的新版本行"""
    config_json = chunking_config_json()

    first_id = env.configs.ensure_config(CHUNKING_CONFIG_TYPE, config_json)
    again_id = env.configs.ensure_config(CHUNKING_CONFIG_TYPE, config_json)
    assert first_id == again_id

    changed = env.configs.ensure_config(
        CHUNKING_CONFIG_TYPE, '{"chunking_config_version":"2"}'
    )
    assert changed != first_id

    rows = env.conn.execute(
        "SELECT version, config_json FROM pipeline_configs"
        " WHERE config_type = ? ORDER BY version",
        (CHUNKING_CONFIG_TYPE,),
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][1] == config_json
