import '../api_error.dart';

/// 查询运行状态（对齐后端 QueryRunState 枚举值）
enum QueryRunStatus {
  queued('queued'),
  running('running'),
  cancelRequested('cancel_requested'),
  completed('completed'),
  failed('failed'),
  cancelled('cancelled');

  const QueryRunStatus(this.wireName);

  /// 服务端线上名（snake_case）
  final String wireName;

  static QueryRunStatus fromName(String name) {
    for (final value in values) {
      if (value.wireName == name) return value;
    }
    throw FormatException('未知的查询状态: $name');
  }
}

/// POST /queries 的 202 响应
class QueryCreated {
  const QueryCreated({
    required this.queryId,
    required this.conversationId,
    required this.streamUrl,
    required this.state,
  });

  factory QueryCreated.fromJson(Map<String, dynamic> json) => QueryCreated(
        queryId: json['query_id'] as String,
        conversationId: json['conversation_id'] as String?,
        streamUrl: json['stream_url'] as String,
        state: QueryRunStatus.fromName(json['state'] as String),
      );

  final String queryId;

  /// 新建查询路径回带的会话归属（幂等重放为 null）
  final String? conversationId;
  final String streamUrl;
  final QueryRunStatus state;
}

/// 引用快照：聚合接口全量形状；SSE citation 事件为其中不含
/// id/document_id/document_version_id 的子集（可空字段统一承载）
class CitationSnapshot {
  const CitationSnapshot({
    this.id,
    this.chunkId,
    this.documentId,
    this.documentVersionId,
    required this.fileName,
    required this.versionNo,
    required this.pageNo,
    required this.sectionPath,
    required this.content,
    required this.validationState,
    this.citationOrder,
    this.vectorScore,
    this.keywordScore,
    this.fusionScore,
    this.rerankScore,
  });

  factory CitationSnapshot.fromJson(Map<String, dynamic> json) =>
      CitationSnapshot(
        id: json['id'] as String?,
        chunkId: json['chunk_id'] as String?,
        documentId: json['document_id'] as String?,
        documentVersionId: json['document_version_id'] as String?,
        fileName: json['file_name'] as String,
        versionNo: json['version_no'] as int,
        pageNo: json['page_no'] as int?,
        sectionPath: json['section_path'] as String? ?? '',
        content: json['content'] as String? ?? '',
        validationState: json['validation_state'] as String? ?? '',
        citationOrder: json['citation_order'] as int?,
        vectorScore: (json['vector_score'] as num?)?.toDouble(),
        keywordScore: (json['keyword_score'] as num?)?.toDouble(),
        fusionScore: (json['fusion_score'] as num?)?.toDouble(),
        rerankScore: (json['rerank_score'] as num?)?.toDouble(),
      );

  final String? id;
  final String? chunkId;
  final String? documentId;
  final String? documentVersionId;
  final String fileName;
  final int versionNo;
  final int? pageNo;
  final String sectionPath;
  final String content;
  final String validationState;
  final int? citationOrder;
  final double? vectorScore;
  final double? keywordScore;
  final double? fusionScore;
  final double? rerankScore;
}

/// GET /queries/{id} 的聚合结果：断线恢复与终态判定的权威来源
class QueryAggregate {
  const QueryAggregate({
    required this.queryId,
    required this.state,
    required this.refused,
    required this.rerankDegraded,
    required this.serverTtftMs,
    required this.totalMs,
    required this.error,
    required this.answer,
    required this.citations,
  });

  factory QueryAggregate.fromJson(Map<String, dynamic> json) {
    final errorJson = json['error'] as Map<String, dynamic>?;
    return QueryAggregate(
      queryId: json['query_id'] as String,
      state: QueryRunStatus.fromName(json['state'] as String),
      refused: (json['refused'] ?? false) as bool,
      rerankDegraded: (json['rerank_degraded'] ?? false) as bool,
      serverTtftMs: json['server_ttft_ms'] as int?,
      totalMs: json['total_ms'] as int?,
      error: errorJson == null ? null : ApiError.fromJson(errorJson),
      answer: json['answer'] as String?,
      citations: [
        for (final item in (json['citations'] ?? []) as List)
          CitationSnapshot.fromJson(item as Map<String, dynamic>),
      ],
    );
  }

  final String queryId;
  final QueryRunStatus state;
  final bool refused;
  final bool rerankDegraded;
  final int? serverTtftMs;
  final int? totalMs;
  final ApiError? error;
  final String? answer;
  final List<CitationSnapshot> citations;
}

/// stage 事件载荷：阶段名与可选附注（如检索完成的候选数）
class QueryStageEvent {
  const QueryStageEvent({required this.name, this.candidates});

  factory QueryStageEvent.fromJson(Map<String, dynamic> json) =>
      QueryStageEvent(
        name: json['name'] as String,
        candidates: json['candidates'] as int?,
      );

  final String name;
  final int? candidates;
}

/// tokens 事件载荷：批次增量文本与严格递增的 token 序号区间
class QueryTokenBatch {
  const QueryTokenBatch({
    required this.from,
    required this.to,
    required this.text,
  });

  factory QueryTokenBatch.fromJson(Map<String, dynamic> json) =>
      QueryTokenBatch(
        from: json['from'] as int,
        to: json['to'] as int,
        text: json['text'] as String? ?? '',
      );

  final int from;
  final int to;
  final String text;
}
