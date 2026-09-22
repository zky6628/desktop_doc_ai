# -*- coding: utf-8 -*-
"""评测数据导出：查询运行 → JSONL（供离线计算 Recall@K/MRR/nDCG）

每行一个查询运行：运行事实、分段指标、全部候选（各阶段排名/分数与
是否进入上下文）与引用快照。只读取延迟值与排名事实，不导出密钥；
问题与回答正文按评测合同导出（本地文件，非网络接口）。

用法：python export_evaluations.py [--db data/workbench.db] [--out eval.jsonl]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "workbench.db"


def _export_rows(conn: sqlite3.Connection):
    """按创建顺序产出每个查询运行的导出行"""
    runs = conn.execute(
        "SELECT id, knowledge_base_id, question, state, refused,"
        " rerank_degraded, retrieval_ms, rerank_ms, prompt_build_ms,"
        " model_ttft_ms, server_ttft_ms, total_ms, input_tokens,"
        " output_tokens, error_code, created_at"
        " FROM query_runs ORDER BY created_at, id"
    ).fetchall()
    # 切片参数配置缓存：配置行内容在运行间大量重复，按配置 ID 去重读取
    chunking_config_cache: dict[str, dict | None] = {}
    for run in runs:
        candidates = conn.execute(
            "SELECT chunk_id, source, vector_rank, vector_score, keyword_rank,"
            " keyword_score, rrf_rank, rrf_score, rerank_rank, rerank_score,"
            " in_context FROM retrieval_candidates WHERE query_run_id = ?"
            " ORDER BY rrf_rank",
            (run[0],),
        ).fetchall()
        citations = conn.execute(
            "SELECT citation_order, chunk_id, file_name_snapshot,"
            " version_no_snapshot, page_no, section_path, validation_state"
            " FROM citations WHERE query_run_id = ? ORDER BY citation_order",
            (run[0],),
        ).fetchall()
        yield {
            "query_id": run[0],
            "kb_id": run[1],
            "question": run[2],
            "state": run[3],
            "refused": bool(run[4]),
            "rerank_degraded": bool(run[5]),
            "retrieval_ms": run[6],
            "rerank_ms": run[7],
            "prompt_build_ms": run[8],
            "model_ttft_ms": run[9],
            "server_ttft_ms": run[10],
            "total_ms": run[11],
            "input_tokens": run[12],
            "output_tokens": run[13],
            "error_code": run[14],
            "created_at": run[15],
            "chunking_config": _chunking_config_for_run(
                conn, run[0], chunking_config_cache
            ),
            "candidates": [
                {
                    "chunk_id": c[0],
                    "source": c[1],
                    "vector_rank": c[2],
                    "vector_score": c[3],
                    "keyword_rank": c[4],
                    "keyword_score": c[5],
                    "rrf_rank": c[6],
                    "rrf_score": c[7],
                    "rerank_rank": c[8],
                    "rerank_score": c[9],
                    "in_context": bool(c[10]),
                }
                for c in candidates
            ],
            "citations": [
                {
                    "citation_order": c[0],
                    "chunk_id": c[1],
                    "file_name": c[2],
                    "version_no": c[3],
                    "page_no": c[4],
                    "section_path": c[5],
                    "validation_state": c[6],
                }
                for c in citations
            ],
        }


def _chunking_config_for_run(
    conn: sqlite3.Connection, run_id: str, cache: dict[str, dict | None]
) -> dict | None:
    """读取查询运行候选归属索引版本的切片参数（对比报告自描述）

    候选经切片主键回查索引版本与配置行；运行无候选或配置行缺失
    时返回 None（指标事实不受影响，参数缺失仅无法参与参数对比）
    """
    row = conn.execute(
        "SELECT iv.chunking_config_id, pc.config_json"
        " FROM retrieval_candidates rc"
        " JOIN chunks ch ON ch.id = rc.chunk_id"
        " JOIN index_versions iv ON iv.id = ch.index_version_id"
        " LEFT JOIN pipeline_configs pc ON pc.id = iv.chunking_config_id"
        " WHERE rc.query_run_id = ? LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None or row[1] is None:
        return None
    config_id = row[0]
    if config_id not in cache:
        cache[config_id] = json.loads(row[1])
    return cache[config_id]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出查询评测数据为 JSONL")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--out", default=None, help="输出文件路径（缺省 stdout）")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        lines = (
            json.dumps(row, ensure_ascii=False)
            for row in _export_rows(conn)
        )
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(line + "\n")
            print(f"exported: {args.out}")
        else:
            for line in lines:
                print(line)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
