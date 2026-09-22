# -*- coding: utf-8 -*-
"""SQLite Repository Adapter：领域仓储 Port 的具体实现"""
from .chunk_repository import SQLiteChunkRepository
from .citation_repository import SQLiteCitationRepository
from .config_repository import SQLiteConfigRepository
from .content_repository import SQLiteContentRepository
from .conversation_repository import SQLiteConversationRepository
from .deletion_repository import SQLiteDeletionRepository
from .document_repository import SQLiteDocumentRepository
from .document_version_repository import SQLiteDocumentVersionRepository
from .embedding_cache_repository import SQLiteEmbeddingCacheRepository
from .evaluation_run_repository import SQLiteEvaluationRunRepository
from .external_task_repository import SQLiteExternalTaskRepository
from .index_version_repository import SQLiteIndexVersionRepository
from .knowledge_base_repository import SQLiteKnowledgeBaseRepository
from .query_event_store import SQLiteQueryEventStore
from .query_run_repository import SQLiteQueryRunRepository
from .system_settings_repository import SQLiteSystemSettingsRepository
from .task_repository import SQLiteTaskRepository

__all__ = [
    "SQLiteChunkRepository",
    "SQLiteCitationRepository",
    "SQLiteConfigRepository",
    "SQLiteContentRepository",
    "SQLiteConversationRepository",
    "SQLiteDeletionRepository",
    "SQLiteDocumentRepository",
    "SQLiteDocumentVersionRepository",
    "SQLiteEmbeddingCacheRepository",
    "SQLiteEvaluationRunRepository",
    "SQLiteExternalTaskRepository",
    "SQLiteIndexVersionRepository",
    "SQLiteKnowledgeBaseRepository",
    "SQLiteQueryEventStore",
    "SQLiteQueryRunRepository",
    "SQLiteSystemSettingsRepository",
    "SQLiteTaskRepository",
]
