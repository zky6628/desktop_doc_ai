import '../api_error.dart';

/// 知识库
class KbDto {
  const KbDto({
    required this.id,
    required this.name,
    required this.description,
    required this.status,
    required this.deletedAt,
    required this.createdAt,
    required this.updatedAt,
  });

  factory KbDto.fromJson(Map<String, dynamic> json) => KbDto(
        id: json['id'] as String,
        name: json['name'] as String,
        description: json['description'] as String?,
        status: json['status'] as String,
        deletedAt: json['deleted_at'] as String?,
        createdAt: json['created_at'] as String,
        updatedAt: json['updated_at'] as String,
      );

  final String id;
  final String name;
  final String? description;
  final String status;
  final String? deletedAt;
  final String createdAt;
  final String updatedAt;
}

/// 文档列表项：文档事实 + 活动版本摘要 + 切片数 + 最近任务
class DocumentSummary {
  const DocumentSummary({
    required this.id,
    required this.knowledgeBaseId,
    required this.displayName,
    required this.status,
    required this.deletedAt,
    required this.createdAt,
    required this.updatedAt,
    required this.activeVersion,
    required this.chunkCount,
    required this.latestTask,
  });

  factory DocumentSummary.fromJson(Map<String, dynamic> json) =>
      DocumentSummary(
        id: json['id'] as String,
        knowledgeBaseId: json['knowledge_base_id'] as String,
        displayName: json['display_name'] as String,
        status: json['status'] as String,
        deletedAt: json['deleted_at'] as String?,
        createdAt: json['created_at'] as String,
        updatedAt: json['updated_at'] as String,
        activeVersion: json['active_version'] == null
            ? null
            : ActiveVersionSummary.fromJson(
                json['active_version'] as Map<String, dynamic>,
              ),
        chunkCount: json['chunk_count'] as int?,
        latestTask: json['latest_task'] == null
            ? null
            : TaskDto.fromJson(json['latest_task'] as Map<String, dynamic>),
      );

  final String id;
  final String knowledgeBaseId;
  final String displayName;
  final String status;
  final String? deletedAt;
  final String createdAt;
  final String updatedAt;
  final ActiveVersionSummary? activeVersion;
  final int? chunkCount;
  final TaskDto? latestTask;
}

/// 活动版本摘要（列表用）
class ActiveVersionSummary {
  const ActiveVersionSummary({
    required this.id,
    required this.versionNo,
    required this.parserMode,
    required this.status,
  });

  factory ActiveVersionSummary.fromJson(Map<String, dynamic> json) =>
      ActiveVersionSummary(
        id: json['id'] as String,
        versionNo: json['version_no'] as int,
        parserMode: json['parser_mode'] as String?,
        status: json['status'] as String,
      );

  final String id;
  final int versionNo;
  final String? parserMode;
  final String status;
}

/// 文档版本
class VersionDto {
  const VersionDto({
    required this.id,
    required this.documentId,
    required this.versionNo,
    required this.sourceSha256,
    required this.mimeType,
    required this.sizeBytes,
    required this.parserMode,
    required this.parserProvider,
    required this.parserVersion,
    required this.status,
    required this.activeIndexVersionId,
    required this.createdAt,
    required this.activatedAt,
  });

  factory VersionDto.fromJson(Map<String, dynamic> json) => VersionDto(
        id: json['id'] as String,
        documentId: json['document_id'] as String,
        versionNo: json['version_no'] as int,
        sourceSha256: json['source_sha256'] as String,
        mimeType: json['mime_type'] as String?,
        sizeBytes: json['size_bytes'] as int?,
        parserMode: json['parser_mode'] as String?,
        parserProvider: json['parser_provider'] as String?,
        parserVersion: json['parser_version'] as String?,
        status: json['status'] as String,
        activeIndexVersionId: json['active_index_version_id'] as String?,
        createdAt: json['created_at'] as String,
        activatedAt: json['activated_at'] as String?,
      );

  final String id;
  final String documentId;
  final int versionNo;
  final String sourceSha256;
  final String? mimeType;
  final int? sizeBytes;
  final String? parserMode;
  final String? parserProvider;
  final String? parserVersion;
  final String status;
  final String? activeIndexVersionId;
  final String createdAt;
  final String? activatedAt;
}

/// 解析块预览（表格证据不含 raw_html，安全渲染由客户端负责）
class BlockPreview {
  const BlockPreview({
    required this.id,
    required this.blockType,
    required this.ordinal,
    required this.pageNo,
    required this.sectionPath,
    required this.contentText,
    required this.table,
  });

  factory BlockPreview.fromJson(Map<String, dynamic> json) => BlockPreview(
        id: json['id'] as String,
        blockType: json['block_type'] as String,
        ordinal: json['ordinal'] as int,
        pageNo: json['page_no'] as int?,
        sectionPath: json['section_path'] as String? ?? '',
        contentText: json['content_text'] as String? ?? '',
        table: json['table'] == null
            ? null
            : BlockTableEvidence.fromJson(json['table'] as Map<String, dynamic>),
      );

  final String id;
  final String blockType;
  final int ordinal;
  final int? pageNo;
  final String sectionPath;
  final String contentText;
  final BlockTableEvidence? table;
}

class BlockTableEvidence {
  const BlockTableEvidence({
    required this.rawMarkdown,
    required this.structureJson,
    required this.searchableText,
  });

  factory BlockTableEvidence.fromJson(Map<String, dynamic> json) =>
      BlockTableEvidence(
        rawMarkdown: json['raw_markdown'] as String? ?? '',
        structureJson: json['structure_json'] as String? ?? '',
        searchableText: json['searchable_text'] as String? ?? '',
      );

  final String rawMarkdown;
  final String structureJson;
  final String searchableText;
}

/// 任务线上状态（对齐后端 TaskStatus 枚举值）
enum TaskWireStatus {
  queued('queued'),
  waitingUser('waiting_user'),
  running('running'),
  waitingExternal('waiting_external'),
  retryWaiting('retry_waiting'),
  cancelRequested('cancel_requested'),
  succeeded('succeeded'),
  failed('failed'),
  cancelled('cancelled');

  const TaskWireStatus(this.wireName);

  final String wireName;

  static TaskWireStatus fromName(String name) {
    for (final value in values) {
      if (value.wireName == name) return value;
    }
    throw FormatException('未知的任务状态: $name');
  }

  bool get isTerminal =>
      this == succeeded || this == failed || this == cancelled;
}

/// 任务（对齐后端 TaskDTO，含展示联查字段）
class TaskDto {
  const TaskDto({
    required this.id,
    required this.taskType,
    required this.state,
    required this.stage,
    required this.progress,
    required this.queuePosition,
    required this.cancellable,
    required this.retryable,
    required this.retryCount,
    required this.maxRetries,
    required this.knowledgeBaseId,
    required this.documentId,
    required this.documentVersionId,
    required this.parentTaskId,
    required this.error,
    required this.createdAt,
    required this.startedAt,
    required this.finishedAt,
    required this.documentDisplayName,
    required this.documentSizeBytes,
    required this.routeMode,
    required this.routeReason,
  });

  factory TaskDto.fromJson(Map<String, dynamic> json) => TaskDto(
        id: json['id'] as String,
        taskType: json['task_type'] as String,
        state: TaskWireStatus.fromName(json['state'] as String),
        stage: json['stage'] as String?,
        progress: (json['progress'] as num?)?.toDouble() ?? 0,
        queuePosition: json['queue_position'] as int?,
        cancellable: (json['cancellable'] ?? false) as bool,
        retryable: (json['retryable'] ?? false) as bool,
        retryCount: json['retry_count'] as int? ?? 0,
        maxRetries: json['max_retries'] as int? ?? 3,
        knowledgeBaseId: json['knowledge_base_id'] as String?,
        documentId: json['document_id'] as String?,
        documentVersionId: json['document_version_id'] as String?,
        parentTaskId: json['parent_task_id'] as String?,
        error: json['error'] == null
            ? null
            : ApiError.fromJson(json['error'] as Map<String, dynamic>),
        createdAt: json['created_at'] as String,
        startedAt: json['started_at'] as String?,
        finishedAt: json['finished_at'] as String?,
        documentDisplayName: json['document_display_name'] as String?,
        documentSizeBytes: json['document_size_bytes'] as int?,
        routeMode: json['route_mode'] as String?,
        routeReason: json['route_reason'] as String?,
      );

  final String id;
  final String taskType;
  final TaskWireStatus state;
  final String? stage;
  final double progress;
  final int? queuePosition;
  final bool cancellable;
  final bool retryable;
  final int retryCount;
  final int maxRetries;
  final String? knowledgeBaseId;
  final String? documentId;
  final String? documentVersionId;
  final String? parentTaskId;
  final ApiError? error;
  final String createdAt;
  final String? startedAt;
  final String? finishedAt;
  final String? documentDisplayName;
  final int? documentSizeBytes;
  final String? routeMode;
  final String? routeReason;
}

/// 任务详情（任务 + 最近事件）
class TaskDetail {
  const TaskDetail({required this.task, required this.recentEvents});

  factory TaskDetail.fromJson(Map<String, dynamic> json) => TaskDetail(
        task: TaskDto.fromJson(json['task'] as Map<String, dynamic>),
        recentEvents: [
          for (final item in (json['recent_events'] ?? []) as List)
            TaskEventDto.fromJson(item as Map<String, dynamic>),
        ],
      );

  final TaskDto task;
  final List<TaskEventDto> recentEvents;
}

/// 任务审计事件
class TaskEventDto {
  const TaskEventDto({
    required this.id,
    required this.eventType,
    required this.state,
    required this.stage,
    required this.createdAt,
    required this.errorCode,
    required this.detailJson,
  });

  factory TaskEventDto.fromJson(Map<String, dynamic> json) => TaskEventDto(
        id: json['id'] as String,
        eventType: json['event_type'] as String,
        state: json['state'] as String,
        stage: json['stage'] as String?,
        createdAt: json['created_at'] as String,
        errorCode: json['error_code'] as String?,
        detailJson: json['detail_json'] as String?,
      );

  final String id;
  final String eventType;
  final String state;
  final String? stage;
  final String createdAt;
  final String? errorCode;
  final String? detailJson;
}

/// 单文件上传/替换结果
class UploadFileResult {
  const UploadFileResult({
    required this.displayName,
    required this.accepted,
    this.documentId,
    this.documentVersionId,
    this.taskId,
    this.taskState,
    this.routeMode,
    this.routeReason,
    this.errorCode,
    this.errorMessage,
  });

  factory UploadFileResult.fromJson(Map<String, dynamic> json) =>
      UploadFileResult(
        displayName: json['display_name'] as String,
        accepted: json['accepted'] as bool,
        documentId: json['document_id'] as String?,
        documentVersionId: json['document_version_id'] as String?,
        taskId: json['task_id'] as String?,
        taskState: json['task_state'] as String?,
        routeMode: (json['route'] as Map<String, dynamic>?)?['mode'] as String?,
        routeReason:
            (json['route'] as Map<String, dynamic>?)?['reason'] as String?,
        errorCode: (json['error'] as Map<String, dynamic>?)?['code'] as String?,
        errorMessage:
            (json['error'] as Map<String, dynamic>?)?['message'] as String?,
      );

  final String displayName;
  final bool accepted;
  final String? documentId;
  final String? documentVersionId;
  final String? taskId;
  final String? taskState;
  final String? routeMode;
  final String? routeReason;
  final String? errorCode;
  final String? errorMessage;
}

/// 文档详情（文档事实 + 活动版本全量 + 最近任务）
class DocumentDetail {
  const DocumentDetail({
    required this.id,
    required this.knowledgeBaseId,
    required this.displayName,
    required this.status,
    required this.deletedAt,
    required this.createdAt,
    required this.updatedAt,
    required this.activeVersion,
    required this.recentTasks,
  });

  factory DocumentDetail.fromJson(Map<String, dynamic> json) => DocumentDetail(
        id: json['id'] as String,
        knowledgeBaseId: json['knowledge_base_id'] as String,
        displayName: json['display_name'] as String,
        status: json['status'] as String,
        deletedAt: json['deleted_at'] as String?,
        createdAt: json['created_at'] as String,
        updatedAt: json['updated_at'] as String,
        activeVersion: json['active_version'] == null
            ? null
            : VersionDto.fromJson(json['active_version'] as Map<String, dynamic>),
        recentTasks: [
          for (final item in (json['recent_tasks'] ?? []) as List)
            TaskDto.fromJson(item as Map<String, dynamic>),
        ],
      );

  final String id;
  final String knowledgeBaseId;
  final String displayName;
  final String status;
  final String? deletedAt;
  final String createdAt;
  final String updatedAt;
  final VersionDto? activeVersion;
  final List<TaskDto> recentTasks;
}

/// keyset 分页容器
class ApiPage<T> {
  const ApiPage({required this.items, required this.nextCursor});

  final List<T> items;
  final String? nextCursor;

  bool get hasMore => nextCursor != null;
}
