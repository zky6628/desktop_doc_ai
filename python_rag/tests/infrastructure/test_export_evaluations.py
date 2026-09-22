# -*- coding: utf-8 -*-
"""评测导出测试：JSONL 行结构与字段完整性"""
import json

from app.domain.ids import uuid7
from app.infrastructure.sqlite.connection import connect
from export_evaluations import main as export_main

from .schema_helpers import FIXED_TIME, fresh_db, insert_kb


def test_export_writes_jsonl_with_run_candidates_and_citations(tmp_path):
    """导出按行组织：运行事实 + 客户端 TTFT + 候选（各阶段分数）+ 引用快照"""
    db_path, _ = fresh_db(tmp_path, name="export.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "导出库")
    run_id = uuid7()
    conn.execute(
        "INSERT INTO query_runs (id, knowledge_base_id, question, state,"
        " refused, rerank_degraded, server_ttft_ms, total_ms, input_tokens,"
        " output_tokens, created_at)"
        " VALUES (?, ?, '年假制度', 'completed', 0, 0, 500, 2000, 100, 50, ?)",
        (run_id, kb_id, FIXED_TIME),
    )
    conn.execute(
        "INSERT INTO retrieval_candidates (id, query_run_id, chunk_id, source,"
        " vector_rank, vector_score, keyword_rank, keyword_score, rrf_rank,"
        " rrf_score, rerank_rank, rerank_score, in_context, created_at)"
        " VALUES (?, ?, NULL, 'dual', 1, 0.9, 1, 5.2, 1, 0.032,"
        " 1, 0.92, 1, ?)",
        (uuid7(), run_id, FIXED_TIME),
    )
    conn.execute(
        "INSERT INTO conversations (id, knowledge_base_id, title, created_at, updated_at)"
        " VALUES ('conv-1', ?, NULL, ?, ?)",
        (kb_id, FIXED_TIME, FIXED_TIME),
    )
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at)"
        " VALUES ('msg-1', 'conv-1', 'assistant', '回答', ?)",
        (FIXED_TIME,),
    )
    conn.execute(
        "INSERT INTO citations (id, assistant_message_id, citation_order,"
        " knowledge_base_id_snapshot, quoted_text_snapshot, validation_state,"
        " created_at, query_run_id)"
        " VALUES (?, 'msg-1', 1, ?, '引文', 'validated', ?, ?)",
        (uuid7(), kb_id, FIXED_TIME, run_id),
    )
    conn.execute(
        "INSERT INTO query_client_metrics (query_run_id, client_send_at,"
        " first_sse_token_received_at, first_token_rendered_at, client_ttft_ms,"
        " reported_at)"
        " VALUES (?, ?, ?, ?, 812, ?)",
        (run_id, FIXED_TIME, FIXED_TIME, FIXED_TIME, FIXED_TIME),
    )
    conn.close()

    out_path = tmp_path / "eval.jsonl"
    assert export_main(["--db", db_path, "--out", str(out_path)]) == 0

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["query_id"] == run_id
    assert row["question"] == "年假制度"
    assert row["state"] == "completed"
    assert row["server_ttft_ms"] == 500
    assert row["model_ttft_ms"] is None
    assert row["client_ttft_ms"] == 812
    assert row["candidates"] == [
        {
            "chunk_id": None,
            "source": "dual",
            "vector_rank": 1,
            "vector_score": 0.9,
            "keyword_rank": 1,
            "keyword_score": 5.2,
            "rrf_rank": 1,
            "rrf_score": 0.032,
            "rerank_rank": 1,
            "rerank_score": 0.92,
            "in_context": True,
            "file_name": None,
        }
    ]
    assert row["citations"][0]["validation_state"] == "validated"


def test_export_empty_database_yields_empty_output(tmp_path):
    """空库导出为空输出（退出码 0）"""
    db_path, _ = fresh_db(tmp_path, name="export_empty.db")

    assert export_main(["--db", db_path]) == 0
