# -*- coding: utf-8 -*-
"""流水线配置仓储的 SQLite 实现：内容哈希幂等的配置版本行

配置记录不可原地修改：同类型同内容的配置行全局唯一并跨任务复用；
内容变化（含参数与算法版本）通过类型内递增的新版本行表达。在役
判定以退役时间为空为准，已退役的哈希不参与复用。
"""
import hashlib

from app.domain.clock import utc_now_iso
from app.domain.ids import uuid7
from app.domain.ports import PipelineConfigRepository as PipelineConfigRepositoryPort

from ..transactions import run_in_transaction


class SQLiteConfigRepository(PipelineConfigRepositoryPort):
    """pipeline_configs 表的写入实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def ensure_config(self, config_type: str, config_json: str) -> str:
        """确保配置行存在并返回其 ID（方法契约见领域 Port 定义）"""

        def _ensure(conn) -> str:
            config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
            existing = conn.execute(
                "SELECT id FROM pipeline_configs"
                " WHERE config_type = ? AND config_hash = ? AND retired_at IS NULL"
                " ORDER BY version DESC LIMIT 1",
                (config_type, config_hash),
            ).fetchone()
            if existing is not None:
                return existing[0]

            next_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM pipeline_configs"
                " WHERE config_type = ?",
                (config_type,),
            ).fetchone()[0]
            config_id = uuid7()
            conn.execute(
                "INSERT INTO pipeline_configs"
                " (id, config_type, version, config_json, config_hash,"
                "  created_at, retired_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    config_id, config_type, next_version, config_json,
                    config_hash, utc_now_iso(),
                ),
            )
            return config_id

        return run_in_transaction(
            self._conn, _ensure, f"确保流水线配置 {config_type}"
        )
