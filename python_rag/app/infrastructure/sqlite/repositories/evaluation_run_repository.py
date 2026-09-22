# -*- coding: utf-8 -*-
"""评测运行仓储的 SQLite 实现：运行事实与组粒度结果快照

进度指针与结果快照按组粒度增量更新（每次组/问题边界一次小事务），
进程中断后运行事实留存（编排骨架可从任务与运行状态推断，不做断点
续跑——评测是可重跑的实验行为，失败重跑优于复杂续跑）。
"""
import json

from app.domain.clock import utc_now_iso
from app.domain.entities import EvaluationRun, EvaluationRunState
from app.domain.ids import uuid7
from app.domain.ports import EvaluationRunRepository as EvaluationRunRepositoryPort


def _row_to_run(row) -> EvaluationRun:
    return EvaluationRun(
        id=row[0],
        knowledge_base_id=row[1],
        task_id=row[2],
        target_version_ids=tuple(json.loads(row[3])),
        questions=tuple(json.loads(row[4])),
        param_groups=json.loads(row[5]),
        state=EvaluationRunState(row[6]),
        current_group_index=row[7],
        current_question_index=row[8],
        results=json.loads(row[9]) if row[9] is not None else None,
        error_code=row[10],
        created_at=row[11],
        updated_at=row[12],
    )

_SELECT_COLUMNS = (
    "id, knowledge_base_id, task_id, target_version_ids_json, questions_json,"
    " param_groups_json, state, current_group_index, current_question_index,"
    " results_json, error_code, created_at, updated_at"
)


class SQLiteEvaluationRunRepository(EvaluationRunRepositoryPort):
    """evaluation_runs 表的读写实现

    :param conn: 由调用方管理的 SQLite 连接（autocommit 模式）
    """

    def __init__(self, conn):
        self._conn = conn

    def create(
        self,
        *,
        knowledge_base_id: str,
        task_id: str,
        target_version_ids,
        questions,
        param_groups,
    ) -> EvaluationRun:
        now = utc_now_iso()
        run_id = uuid7()
        self._conn.execute(
            "INSERT INTO evaluation_runs ("
            " id, knowledge_base_id, task_id, target_version_ids_json,"
            " questions_json, param_groups_json, state, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (
                run_id,
                knowledge_base_id,
                task_id,
                json.dumps(list(target_version_ids)),
                json.dumps(list(questions), ensure_ascii=False),
                json.dumps(list(param_groups)),
                now,
                now,
            ),
        )
        return self.get(run_id)

    def get(self, run_id: str):
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM evaluation_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else _row_to_run(row)

    def get_by_task(self, task_id: str):
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM evaluation_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return None if row is None else _row_to_run(row)

    def list_by_knowledge_base(self, knowledge_base_id: str, *, limit: int = 20):
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM evaluation_runs"
            " WHERE knowledge_base_id = ? ORDER BY created_at DESC, id DESC"
            " LIMIT ?",
            (knowledge_base_id, limit),
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def get_running(self):
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM evaluation_runs"
            " WHERE state = 'running' ORDER BY created_at LIMIT 1"
        ).fetchone()
        return None if row is None else _row_to_run(row)

    def mark_progress(self, run_id: str, group_index: int, question_index: int) -> None:
        self._conn.execute(
            "UPDATE evaluation_runs SET current_group_index = ?,"
            " current_question_index = ?, updated_at = ? WHERE id = ?",
            (group_index, question_index, utc_now_iso(), run_id),
        )

    def save_group_result(self, run_id: str, group_index: int, result: dict) -> None:
        run = self.get(run_id)
        if run is None:
            return
        results = list(run.results) if run.results is not None else []
        while len(results) <= group_index:
            results.append(None)
        results[group_index] = result
        self._conn.execute(
            "UPDATE evaluation_runs SET results_json = ?, updated_at = ?"
            " WHERE id = ?",
            (
                json.dumps(results, ensure_ascii=False),
                utc_now_iso(),
                run_id,
            ),
        )

    def mark_terminal(self, run_id: str, state, *, error_code: str | None = None) -> None:
        self._conn.execute(
            "UPDATE evaluation_runs SET state = ?, error_code = ?, updated_at = ?"
            " WHERE id = ?",
            (state.value, error_code, utc_now_iso(), run_id),
        )
