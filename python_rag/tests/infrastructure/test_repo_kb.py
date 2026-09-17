# -*- coding: utf-8 -*-
"""知识库仓储集成测试（真实临时 SQLite 库）"""
import pytest

from app.domain.errors import (
    DuplicateActiveNameError,
    EntityNotFoundError,
)
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import SQLiteKnowledgeBaseRepository

from .schema_helpers import fresh_db


@pytest.fixture()
def repo(tmp_path):
    """应用全部迁移的临时库 + 知识库仓储"""
    db_path, _ = fresh_db(tmp_path, name="repo_kb.db")
    conn = connect(db_path)
    yield SQLiteKnowledgeBaseRepository(conn), conn
    conn.close()


def test_create_and_get(repo):
    """创建后可按 ID 读回，字段与创建参数一致"""
    repository, _ = repo
    kb = repository.create(name="项目资料", description="工作文档")
    assert kb.id
    assert kb.name == "项目资料"
    assert kb.description == "工作文档"
    assert kb.status.value == "active"
    assert kb.deleted_at is None

    loaded = repository.get(kb.id)
    assert loaded == kb


def test_get_missing_returns_none(repo):
    """不存在的 ID 返回 None 而非异常"""
    repository, _ = repo
    assert repository.get("01900000-0000-7000-8000-000000000000") is None


def test_create_duplicate_active_name_rejected(repo):
    """活动名称唯一：重复创建抛领域错误"""
    repository, _ = repo
    repository.create(name="项目资料")
    with pytest.raises(DuplicateActiveNameError):
        repository.create(name="项目资料")


def test_soft_delete_allows_name_reuse(repo):
    """软删除后同名可重建；旧记录仍可按 ID 读取"""
    repository, _ = repo
    old = repository.create(name="项目资料")
    repository.soft_delete(old.id)

    with pytest.raises(EntityNotFoundError):
        repository.soft_delete(old.id)

    new = repository.create(name="项目资料")
    assert new.id != old.id

    loaded_old = repository.get(old.id)
    assert loaded_old is not None
    assert loaded_old.status.value == "deleted"
    assert loaded_old.deleted_at is not None
    assert loaded_old.delete_requested_at is not None


def test_find_active_by_name_and_list_active(repo):
    """活动查询过滤已删除记录"""
    repository, _ = repo
    kept = repository.create(name="保留")
    dropped = repository.create(name="删除")
    repository.soft_delete(dropped.id)

    assert repository.find_active_by_name("保留").id == kept.id
    assert repository.find_active_by_name("删除") is None

    active_names = [kb.name for kb in repository.list_active()]
    assert active_names == ["保留"]
