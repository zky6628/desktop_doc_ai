# -*- coding: utf-8 -*-
"""会话 API 测试：列表 keyset 分页与最后消息摘要、历史消息、删除级联

删除的级联与指标保留依赖存储层外键行为，此处一并验证：消息与引用
随会话清除，查询运行的会话/消息关联置空且运行行保留。
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import (
    ApiV1Dependencies,
    ConversationDependencies,
    create_api_router,
)
from app.domain.ids import uuid7
from app.infrastructure.sqlite.connection import connect
from app.infrastructure.sqlite.repositories import (
    SQLiteCitationRepository,
    SQLiteConversationRepository,
    SQLiteKnowledgeBaseRepository,
)
from tests.infrastructure.schema_helpers import FIXED_TIME, fresh_db, insert_kb


@pytest.fixture()
def runtime(tmp_path):
    """临时库 + 独立 v1 应用（含会话路由）+ TestClient"""
    db_path, _ = fresh_db(tmp_path, "conversation_api.db")
    conn = connect(db_path)
    kb_id = insert_kb(conn, "会话测试库")
    conversation_repo = SQLiteConversationRepository(conn)
    application = FastAPI()
    application.include_router(
        create_api_router(
            ApiV1Dependencies(
                orchestrator=None,
                task_repo=None,
                conversations=ConversationDependencies(
                    kb_repo=SQLiteKnowledgeBaseRepository(conn),
                    conversation_repo=conversation_repo,
                    citation_repo=SQLiteCitationRepository(conn),
                ),
            )
        )
    )
    env = SimpleNamespace(
        conn=conn,
        kb_id=kb_id,
        client=TestClient(application),
        conversation_repo=conversation_repo,
    )
    yield env
    conn.close()


def _conversation_with_messages(env, messages: list[tuple[str, str]]) -> str:
    """建立会话并依序写入消息，返回会话 ID"""
    conversation_id = env.conversation_repo.ensure_conversation(env.kb_id, None)
    for role, content in messages:
        env.conversation_repo.add_message(conversation_id, role, content)
    return conversation_id


def test_list_conversations_includes_last_message_summary(runtime):
    """会话列表携带最后消息摘要（空白折叠后的正文摘录）"""
    env = runtime
    conversation_id = _conversation_with_messages(
        env, [("user", "什么是年假制度？\n\n请详细说明。"), ("assistant", "回答正文")]
    )

    response = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    items = body["data"]["items"]
    assert len(items) == 1
    summary = items[0]
    assert summary["id"] == conversation_id
    assert summary["knowledge_base_id"] == env.kb_id
    # 未命名会话由首条用户消息生成默认标题（空白折叠，未达截断长度）
    assert summary["title"] == "什么是年假制度？ 请详细说明。"
    assert summary["last_message"]["role"] == "assistant"
    assert summary["last_message"]["content_excerpt"] == "回答正文"
    assert summary["last_message"]["created_at"]


def test_list_conversations_orders_by_recent_activity(runtime):
    """最近活跃的会话排在列表前（updated_at 倒序）"""
    env = runtime
    earlier = _conversation_with_messages(env, [("user", "早的问题")])
    later = _conversation_with_messages(env, [("user", "晚的问题")])

    response = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert [item["id"] for item in response.json()["data"]["items"]] == [
        later,
        earlier,
    ]

    # 早期会话有新消息后回到列表头
    env.conversation_repo.add_message(earlier, "assistant", "新的回答")
    response = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert [item["id"] for item in response.json()["data"]["items"]] == [
        earlier,
        later,
    ]


def test_list_conversations_keyset_pagination(runtime):
    """keyset 分页：游标续页且页尾游标为空表示最后一页"""
    env = runtime
    ids = [_conversation_with_messages(env, [("user", f"问题 {index}")]) for index in range(3)]

    first_page = env.client.get(
        f"/api/v1/conversations?kb_id={env.kb_id}&limit=2"
    ).json()["data"]
    assert [item["id"] for item in first_page["items"]] == ids[::-1][:2]
    assert first_page["next_cursor"]

    second_page = env.client.get(
        f"/api/v1/conversations?kb_id={env.kb_id}&limit=2"
        f"&cursor={first_page['next_cursor']}"
    ).json()["data"]
    assert [item["id"] for item in second_page["items"]] == [ids[0]]
    assert second_page["next_cursor"] is None


def test_list_conversations_rejects_missing_kb_and_bad_params(runtime):
    """知识库不存在 404；游标非法与页大小非正数 400"""
    env = runtime
    missing = env.client.get("/api/v1/conversations?kb_id=no-such-kb")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    bad_cursor = env.client.get(
        f"/api/v1/conversations?kb_id={env.kb_id}&cursor=not-base64!"
    )
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["error"]["code"] == "INVALID_PARAM"

    bad_limit = env.client.get(
        f"/api/v1/conversations?kb_id={env.kb_id}&limit=0"
    )
    assert bad_limit.status_code == 400
    assert bad_limit.json()["error"]["code"] == "INVALID_PARAM"


def test_deleted_kb_conversations_still_readable(runtime):
    """知识库软删除后历史会话仍可读（410 只约束新建会话与查询）"""
    env = runtime
    conversation_id = _conversation_with_messages(env, [("user", "历史问题")])
    env.conn.execute(
        "UPDATE knowledge_bases SET deleted_at = ? WHERE id = ?",
        (FIXED_TIME, env.kb_id),
    )

    listed = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["id"] == conversation_id

    messages = env.client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    )
    assert messages.status_code == 200
    assert messages.json()["data"]["items"][0]["content"] == "历史问题"


def test_list_conversations_without_kb_id_returns_all_libraries(runtime):
    """省略 kb_id 返回跨库全量会话（含已删除知识库的历史会话）"""
    env = runtime
    other_kb_id = insert_kb(env.conn, "第二知识库")
    in_default = _conversation_with_messages(env, [("user", "默认库的问题")])
    in_other = env.conversation_repo.ensure_conversation(other_kb_id, None)
    env.conversation_repo.add_message(in_other, "user", "第二库的问题")
    env.conn.execute(
        "UPDATE knowledge_bases SET deleted_at = ? WHERE id = ?",
        (FIXED_TIME, env.kb_id),
    )

    response = env.client.get("/api/v1/conversations")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert {item["id"] for item in items} == {in_default, in_other}

    # 指定已删除知识库仍可按库过滤查看（410 只约束新建会话与查询）
    filtered = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["data"]["items"]] == [
        in_default
    ]


def test_messages_returns_full_ascending_history(runtime):
    """历史消息按写入顺序升序全量返回"""
    env = runtime
    conversation_id = _conversation_with_messages(
        env, [("user", "第一问"), ("assistant", "第一答"), ("user", "第二问")]
    )

    response = env.client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["conversation_id"] == conversation_id
    contents = [item["content"] for item in data["items"]]
    assert contents == ["第一问", "第一答", "第二问"]
    roles = [item["role"] for item in data["items"]]
    assert roles == ["user", "assistant", "user"]
    assert all(item["id"] and item["created_at"] for item in data["items"])


def test_messages_attach_citations_to_assistant_messages(runtime):
    """助手消息携带引用快照（含引用编号），用户消息引用为空列表"""
    env = runtime
    conversation_id = _conversation_with_messages(
        env, [("user", "问题"), ("assistant", "根据[S1]回答。")]
    )
    assistant_id = env.conn.execute(
        "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant'",
        (conversation_id,),
    ).fetchone()[0]
    env.conn.execute(
        "INSERT INTO citations (id, assistant_message_id, citation_order,"
        " chunk_id, knowledge_base_id_snapshot, document_id_snapshot,"
        " document_version_id_snapshot, file_name_snapshot, version_no_snapshot,"
        " quoted_text_snapshot, content_snapshot, page_no, section_path,"
        " validation_state, vector_score, rerank_score, created_at)"
        " VALUES (?, ?, 1, NULL, ?, 'doc-1', 'ver-1', '手册.pdf', 2,"
        " '引文', '引用正文快照', 3, '休假/年假', 'validated', 0.9, 0.8, ?)",
        (uuid7(), assistant_id, env.kb_id, FIXED_TIME),
    )

    response = env.client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items[0]["citations"] == []
    citations = items[1]["citations"]
    assert len(citations) == 1
    citation = citations[0]
    assert citation["citation_order"] == 1
    assert citation["chunk_id"] is None
    assert citation["file_name"] == "手册.pdf"
    assert citation["version_no"] == 2
    assert citation["page_no"] == 3
    assert citation["section_path"] == "休假/年假"
    assert citation["content"] == "引用正文快照"
    assert citation["validation_state"] == "validated"
    assert citation["vector_score"] == 0.9
    assert citation["rerank_score"] == 0.8


def test_messages_rejects_unknown_conversation(runtime):
    """会话不存在按 404 会话错误码表达"""
    env = runtime
    response = env.client.get(f"/api/v1/conversations/{uuid7()}/messages")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_delete_conversation_keeps_query_metrics(runtime):
    """删除会话：消息与引用级联清除，查询运行行与问题事实保留且关联置空"""
    env = runtime
    conversation_id = _conversation_with_messages(
        env, [("user", "问题"), ("assistant", "回答")]
    )
    message_ids = [
        row[0]
        for row in env.conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ?"
            " ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
    ]
    assistant_message_id = message_ids[-1]
    env.conn.execute(
        "INSERT INTO citations (id, assistant_message_id, citation_order,"
        " knowledge_base_id_snapshot, quoted_text_snapshot, validation_state,"
        " created_at) VALUES (?, ?, 1, ?, '引文', 'validated', ?)",
        (uuid7(), assistant_message_id, env.kb_id, FIXED_TIME),
    )
    query_run_id = uuid7()
    env.conn.execute(
        "INSERT INTO query_runs (id, knowledge_base_id, conversation_id,"
        " user_message_id, assistant_message_id, question, state, refused,"
        " rerank_degraded, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'completed', 0, 0, ?)",
        (
            query_run_id,
            env.kb_id,
            conversation_id,
            message_ids[0],
            assistant_message_id,
            "问题",
            FIXED_TIME,
        ),
    )

    response = env.client.delete(f"/api/v1/conversations/{conversation_id}")

    assert response.status_code == 204
    assert env.conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()[0] == 0
    assert env.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()[0] == 0
    assert env.conn.execute(
        "SELECT COUNT(*) FROM citations WHERE assistant_message_id = ?",
        (assistant_message_id,),
    ).fetchone()[0] == 0
    row = env.conn.execute(
        "SELECT conversation_id, user_message_id, assistant_message_id, question"
        " FROM query_runs WHERE id = ?",
        (query_run_id,),
    ).fetchone()
    assert row == (None, None, None, "问题")


def test_delete_conversation_rejects_unknown_conversation(runtime):
    """删除不存在的会话按 404 会话错误码表达"""
    env = runtime
    response = env.client.delete(f"/api/v1/conversations/{uuid7()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_first_user_message_names_unnamed_conversation(runtime):
    """未命名会话以首条用户消息生成默认标题（空白折叠截前缀，仅首次生效）"""
    env = runtime
    conversation_id = env.conversation_repo.ensure_conversation(env.kb_id, None)
    env.conversation_repo.add_message(conversation_id, "user", "年假政策是什么")
    env.conversation_repo.add_message(conversation_id, "assistant", "回答正文")

    listed = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert listed.json()["data"]["items"][0]["title"] == "年假政策是什么"

    # 已有标题后不再随消息变化；助手消息不触发命名
    env.conversation_repo.add_message(conversation_id, "assistant", "第二条")
    env.conversation_repo.add_message(conversation_id, "user", "第二个问题")
    relisted = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert relisted.json()["data"]["items"][0]["title"] == "年假政策是什么"


def test_first_user_message_title_truncates_long_question(runtime):
    """默认标题为空白折叠后的 20 字符前缀（不附加省略号）"""
    env = runtime
    conversation_id = env.conversation_repo.ensure_conversation(env.kb_id, None)
    env.conversation_repo.add_message(
        conversation_id, "user", "  很长的问题\t\n超过二十个字符的边界在哪里呢继续延伸  "
    )

    listed = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    assert listed.json()["data"]["items"][0]["title"] == (
        "很长的问题 超过二十个字符的边界在哪里呢"
    )


def test_rename_conversation_updates_title_without_touching_recency(runtime):
    """重命名 204 且列表反映；不改变最近活跃排序；空标题 400；不存在 404"""
    env = runtime
    earlier = _conversation_with_messages(env, [("user", "早的问题")])
    later = _conversation_with_messages(env, [("user", "晚的问题")])

    response = env.client.patch(
        f"/api/v1/conversations/{earlier}", json={"title": "  年假制度梳理  "}
    )
    assert response.status_code == 204

    listed = env.client.get(f"/api/v1/conversations?kb_id={env.kb_id}")
    items = listed.json()["data"]["items"]
    assert [item["id"] for item in items] == [later, earlier]
    assert items[1]["title"] == "年假制度梳理"

    blank = env.client.patch(
        f"/api/v1/conversations/{earlier}", json={"title": "   "}
    )
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "INVALID_PARAM"

    missing = env.client.patch(
        "/api/v1/conversations/no-such-conversation", json={"title": "标题"}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_delete_all_conversations_keeps_query_metrics(runtime):
    """清空全部会话：消息与引用级联清除，查询运行行与问题事实保留且关联置空"""
    env = runtime
    first = _conversation_with_messages(env, [("user", "问题一"), ("assistant", "回答一")])
    second = _conversation_with_messages(env, [("user", "问题二"), ("assistant", "回答二")])
    for conversation_id in (first, second):
        env.conn.execute(
            "INSERT INTO query_runs (id, knowledge_base_id, conversation_id,"
            " question, state, refused, rerank_degraded, created_at)"
            " VALUES (?, ?, ?, ?, 'completed', 0, 0, ?)",
            (uuid7(), env.kb_id, conversation_id, "问题", FIXED_TIME),
        )

    response = env.client.delete("/api/v1/conversations")

    assert response.status_code == 200
    assert response.json()["data"]["deleted"] == 2
    assert env.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM query_runs").fetchone()[0] == 2
    assert env.conn.execute(
        "SELECT COUNT(*) FROM query_runs WHERE conversation_id IS NOT NULL"
    ).fetchone()[0] == 0
