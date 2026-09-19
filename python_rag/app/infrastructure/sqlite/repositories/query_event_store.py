# -*- coding: utf-8 -*-
"""查询事件仓储的 SQLite 实现：单调序号追加与断点重放

event_seq 在单事务内按查询内最大序号 +1 分配（唯一索引兜底并发）；
读取过滤已过期 token 批次（聚合正文经查询读取获取）。
"""
import json
from datetime import UTC, datetime, timedelta

from app.domain.clock import utc_now_iso
from app.domain.entities import QueryEvent
from app.domain.errors import EntityNotFoundError
from app.domain.ids import uuid7
from app.domain.ports import QueryEventStore as QueryEventStorePort

from ..transactions import run_in_transaction

_EVENT_COLUMNS = (
    "query_run_id, event_seq, event_type, payload_json, token_text,"
    " token_seq_start, token_seq_end, created_at, expires_at"
)


class SQLiteQueryEventStore(QueryEventStorePort):
    """query_events 表的仓储实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def append(
        self,
        run_id: str,
        *,
        event_type: str,
        payload: dict | None = None,
        token_text: str | None = None,
        token_seq_start: int | None = None,
        token_seq_end: int | None = None,
        expires_at: str | None = None,
    ) -> int:
        def _append(conn) -> int:
            exists = conn.execute(
                "SELECT 1 FROM query_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"查询不存在: {run_id}")
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM query_events"
                " WHERE query_run_id = ?",
                (run_id,),
            ).fetchone()[0]
            payload_json = (
                None
                if payload is None
                else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            conn.execute(
                "INSERT INTO query_events ("
                " id, query_run_id, event_seq, event_type, payload_json,"
                " token_text, token_seq_start, token_seq_end, created_at,"
                " expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid7(), run_id, next_seq, event_type, payload_json,
                    token_text, token_seq_start, token_seq_end,
                    utc_now_iso(), expires_at,
                ),
            )
            return next_seq

        return run_in_transaction(self._conn, _append, f"追加查询事件 {run_id}")

    def read_after(self, run_id: str, after_seq: int = 0) -> list[QueryEvent]:
        rows = self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM query_events"
            " WHERE query_run_id = ? AND event_seq > ?"
            "   AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY event_seq",
            (run_id, after_seq, utc_now_iso()),
        ).fetchall()
        return [self._to_event(row) for row in rows]

    def expire_tokens(self, run_id: str, expiry_horizon_seconds: int) -> None:
        def _expire(conn) -> None:
            exists = conn.execute(
                "SELECT 1 FROM query_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise EntityNotFoundError(f"查询不存在: {run_id}")
            expiry = (
                datetime.now(UTC) + timedelta(seconds=expiry_horizon_seconds)
            ).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE query_events SET expires_at = ?"
                " WHERE query_run_id = ? AND event_type = 'tokens'",
                (expiry, run_id),
            )

        run_in_transaction(
            self._conn, _expire, f"设置查询 token 批次过期 {run_id}"
        )

    def purge_expired_tokens(self) -> int:
        """删除全部已过期 token 批次行（方法契约见领域 Port 定义）"""
        now = utc_now_iso()
        cursor = self._conn.execute(
            "DELETE FROM query_events"
            " WHERE event_type = 'tokens' AND expires_at IS NOT NULL"
            "   AND expires_at <= ?",
            (now,),
        )
        return cursor.rowcount

    @staticmethod
    def _to_event(row) -> QueryEvent:
        return QueryEvent(
            query_run_id=row[0],
            event_seq=row[1],
            event_type=row[2],
            payload_json=row[3],
            token_text=row[4],
            token_seq_start=row[5],
            token_seq_end=row[6],
            created_at=row[7],
            expires_at=row[8],
        )
