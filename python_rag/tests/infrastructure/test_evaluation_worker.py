# -*- coding: utf-8 -*-
"""评测运行 Worker 集成测试：参数组执行、结果聚合、参数恢复与取消"""

from app.infrastructure.evaluation import EvaluationRunService

from .schema_helpers import import_file


def _stub_service(env, version_id: str, ttft_ms: int = 120) -> EvaluationRunService:
    """查询链路替身：同步假运行 ID 与固定指标行（不触达生成）"""
    return EvaluationRunService(
        evaluation_repo=env.repos["evaluations"],
        settings_repo=env.repos["settings"],
        content_repo=env.repos["content"],
        version_ids_reader=lambda kb_id: [version_id],
        query_launcher=lambda kb_id, question: f"qr-{question}",
        query_metrics_reader=lambda rid: {
            "state": "completed",
            "refused": False,
            "rerank_degraded": False,
            "server_ttft_ms": ttft_ms,
            "input_tokens": 10,
            "output_tokens": 5,
        },
    )


def _prepared_run(env):
    """导入文档后创建评测运行（两参数组）与编排任务，返回三元组"""
    task_id = import_file(env, "评测.txt", "评测内容第一段\n\n评测内容第二段".encode())
    worker = env.build()
    while env.repos["tasks"].get(task_id).state.value == "queued":
        assert worker.process_next() is True
    task = env.repos["tasks"].get(task_id)
    assert task.state.value == "succeeded"
    version_id = task.document_version_id

    eval_task = env.repos["tasks"].create(
        "evaluation_run", knowledge_base_id=env.kb_id
    )
    run = env.repos["evaluations"].create(
        knowledge_base_id=env.kb_id,
        task_id=eval_task.id,
        target_version_ids=[version_id],
        questions=["什么是年假", "如何申请"],
        param_groups=[
            {"parent_chunk_chars": 800, "child_chunk_chars": 300},
            {"parent_chunk_chars": 1200, "child_chunk_chars": 400},
        ],
    )
    return run, version_id


def test_evaluation_run_executes_groups_and_restores_params(env):
    """两参数组顺序执行：结果聚合正确、参数恢复默认、恢复重建入队"""
    run, version_id = _prepared_run(env)
    service = _stub_service(env, version_id)
    full_worker = env.build(
        evaluation_repo=env.repos["evaluations"], evaluation_service=service
    )

    # 编排任务处理至终态（导入残留的清理任务可能并存，循环处理）
    while env.repos["tasks"].get(run.task_id).state.value == "queued":
        assert full_worker.process_next() is True

    finished = env.repos["evaluations"].get(run.id)
    assert finished.state.value == "completed"
    assert finished.results is not None and len(finished.results) == 2
    assert finished.results[0]["group"] == {
        "parent_chunk_chars": 800,
        "child_chunk_chars": 300,
    }
    metrics = finished.results[0]["metrics"]
    assert metrics["total"] == 2
    assert metrics["completed"] == 2
    assert metrics["ttft_p50_ms"] == 120
    assert metrics["input_tokens"] == 20
    assert metrics["output_tokens"] == 10

    # 在役参数恢复为缺省（评测前未设置过覆盖）
    assert env.repos["settings"].get("chunking_override") is None

    # 恢复重建任务已入队（每个参评版本一个）
    queued = env.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type = 'rebuild_index'"
        " AND state = 'queued'"
    ).fetchone()[0]
    assert queued == 1


def test_evaluation_cancel_restores_params(env):
    """取消信号打断编排：运行取消、在役参数恢复为评测前值"""
    task_id = import_file(env, "评测.txt", "取消场景第一段\n\n取消场景第二段".encode())
    worker = env.build()
    while env.repos["tasks"].get(task_id).state.value == "queued":
        assert worker.process_next() is True
    version_id = env.repos["tasks"].get(task_id).document_version_id

    # 评测前写一个非默认覆盖：取消后必须恢复为该值而非默认值
    env.repos["settings"].put(
        "chunking_override",
        '{"child_chunk_chars":300,"parent_chunk_chars":900}',
    )
    eval_task = env.repos["tasks"].create(
        "evaluation_run", knowledge_base_id=env.kb_id
    )
    run = env.repos["evaluations"].create(
        knowledge_base_id=env.kb_id,
        task_id=eval_task.id,
        target_version_ids=[version_id],
        questions=["问题一", "问题二"],
        param_groups=[
            {"parent_chunk_chars": 600, "child_chunk_chars": 200},
            {"parent_chunk_chars": 1200, "child_chunk_chars": 400},
        ],
    )

    rebuild_calls = {"count": 0}

    def rebuild_noop(version_id_arg: str) -> None:
        rebuild_calls["count"] += 1

    service = _stub_service(env, version_id)
    # 首次重建完成后取消请求到达：问题边界检查点打断编排
    service.execute(
        run,
        rebuild_version=rebuild_noop,
        is_cancel_requested=lambda: rebuild_calls["count"] >= 1,
    )

    finished = env.repos["evaluations"].get(run.id)
    assert finished.state.value == "cancelled"
    assert rebuild_calls["count"] == 1
    assert (
        env.repos["settings"].get("chunking_override")
        == '{"child_chunk_chars":300,"parent_chunk_chars":900}'
    )
