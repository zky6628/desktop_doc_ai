import 'dart:convert';

import 'package:http/http.dart' as http;

import 'api_client_base.dart';
import 'dto/knowledge_dto.dart';

/// 待上传文件的输入（来自文件选择器或拖拽落地的本地路径）
class UploadFileInput {
  const UploadFileInput({required this.displayName, required this.path});

  final String displayName;
  final String path;
}

/// 知识库/文档/任务域 API 客户端
///
/// 信封与错误解析复用 [ApiClientBase]；上传为 multipart 批量长操作
/// （202，逐文件独立接受或拒绝）。
class KnowledgeApiClient {
  KnowledgeApiClient({http.Client? client, Uri? baseUrl})
    : _base = ApiClientBase(client: client, baseUrl: baseUrl);

  final ApiClientBase _base;

  // ===================== 知识库 =====================

  Future<KbDto> createKnowledgeBase(
    String name, {
    String? description,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/knowledge-bases'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'name': name, 'description': ?description}),
      ),
    );
    return KbDto.fromJson(_base.dataOf(response));
  }

  Future<ApiPage<KbDto>> listKnowledgeBases({String? cursor, int? limit}) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/knowledge-bases').replace(
              queryParameters: {
                'cursor': ?cursor,
                if (limit != null) 'limit': '$limit',
              },
            ),
      ),
    );
    return _pageOf(_base.dataOf(response), KbDto.fromJson);
  }

  Future<KbDto> getKnowledgeBase(String kbId) async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/knowledge-bases/$kbId')),
    );
    return KbDto.fromJson(_base.dataOf(response));
  }

  Future<KbDto> renameKnowledgeBase(
    String kbId, {
    required String name,
    String? description,
  }) async {
    final response = await _base.send(
      (client) => client.patch(
        _base.uri('/api/v1/knowledge-bases/$kbId'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'name': name, 'description': ?description}),
      ),
    );
    return KbDto.fromJson(_base.dataOf(response));
  }

  /// 删除知识库（软删除 + 物理清理任务，202）
  Future<TaskDto> deleteKnowledgeBase(
    String kbId, {
    required String idempotencyKey,
  }) async {
    final response = await _base.send(
      (client) => client.delete(
        _base.uri('/api/v1/knowledge-bases/$kbId'),
        headers: {'Idempotency-Key': idempotencyKey},
      ),
    );
    return _taskOf(_base.dataOf(response));
  }

  /// 创建知识库健康检查任务（202）
  Future<TaskDto> createKnowledgeBaseHealthCheck(
    String kbId, {
    required String idempotencyKey,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/health/knowledge-bases/$kbId'),
        headers: {'Idempotency-Key': idempotencyKey},
      ),
    );
    return _taskOf(_base.dataOf(response));
  }

  // ===================== 文档 =====================

  Future<ApiPage<DocumentSummary>> listDocuments(
    String kbId, {
    String? cursor,
    int? limit,
    bool includeDeleted = false,
  }) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/knowledge-bases/$kbId/documents').replace(
              queryParameters: {
                'cursor': ?cursor,
                if (limit != null) 'limit': '$limit',
                if (includeDeleted) 'include_deleted': 'true',
              },
            ),
      ),
    );
    return _pageOf(_base.dataOf(response), DocumentSummary.fromJson);
  }

  Future<DocumentDetail> getDocument(String documentId) async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/documents/$documentId')),
    );
    return DocumentDetail.fromJson(_base.dataOf(response));
  }

  Future<ApiPage<VersionDto>> listDocumentVersions(
    String documentId, {
    int? limit,
  }) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/documents/$documentId/versions').replace(
              queryParameters: {
                if (limit != null) 'limit': '$limit',
              },
            ),
      ),
    );
    return _pageOf(_base.dataOf(response), VersionDto.fromJson);
  }

  Future<ApiPage<BlockPreview>> listDocumentBlocks(
    String documentId, {
    String? cursor,
    int? limit,
  }) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/documents/$documentId/blocks').replace(
              queryParameters: {
                'cursor': ?cursor,
                if (limit != null) 'limit': '$limit',
              },
            ),
      ),
    );
    return _pageOf(_base.dataOf(response), BlockPreview.fromJson);
  }

  /// 批量上传文档（multipart；202，逐文件独立接受或拒绝）
  Future<List<UploadFileResult>> uploadDocuments(
    String kbId, {
    required List<UploadFileInput> files,
    required String idempotencyKey,
    String parserPreference = 'auto',
    String duplicatePolicy = 'skip',
  }) async {
    final request = http.MultipartRequest(
      'POST',
      _base.uri('/api/v1/knowledge-bases/$kbId/documents'),
    )
      ..fields['parser_preference'] = parserPreference
      ..fields['duplicate_policy'] = duplicatePolicy
      ..headers['Idempotency-Key'] = idempotencyKey;
    for (final file in files) {
      // 流式挂载文件：不在内存堆积整文件（单文件上限 100MB）
      request.files
          .add(await http.MultipartFile.fromPath('files', file.path));
    }
    final streamed = await _base.send(
      (client) async => http.Response.fromStream(await client.send(request)),
    );
    final results = (_base.dataOf(streamed)['results'] ?? []) as List;
    return [
      for (final item in results)
        UploadFileResult.fromJson(item as Map<String, dynamic>),
    ];
  }

  /// 替换文档内容（202；新版本走导入管线，旧活动版本继续服务）
  Future<UploadFileResult> replaceDocument(
    String documentId, {
    required UploadFileInput file,
    required String idempotencyKey,
    String parserPreference = 'auto',
  }) async {
    final request = http.MultipartRequest(
      'POST',
      _base.uri('/api/v1/documents/$documentId/replace'),
    )
      ..fields['parser_preference'] = parserPreference
      ..headers['Idempotency-Key'] = idempotencyKey;
    request.files
        .add(await http.MultipartFile.fromPath('file', file.path));
    final streamed = await _base.send(
      (client) async => http.Response.fromStream(await client.send(request)),
    );
    return UploadFileResult.fromJson(
      _base.dataOf(streamed)['result'] as Map<String, dynamic>,
    );
  }

  /// 重建文档索引（202；新索引激活前旧索引继续服务）
  Future<TaskDto> rebuildDocument(
    String documentId, {
    required String idempotencyKey,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/documents/$documentId/rebuild'),
        headers: {'Idempotency-Key': idempotencyKey},
      ),
    );
    return _taskOf(_base.dataOf(response));
  }

  /// 删除文档（软删除 + 物理清理任务，202）
  Future<TaskDto> deleteDocument(
    String documentId, {
    required String idempotencyKey,
  }) async {
    final response = await _base.send(
      (client) => client.delete(
        _base.uri('/api/v1/documents/$documentId'),
        headers: {'Idempotency-Key': idempotencyKey},
      ),
    );
    return _taskOf(_base.dataOf(response));
  }

  // ===================== 任务 =====================

  Future<ApiPage<TaskDto>> listTasks({
    String? cursor,
    int? limit,
    String? state,
    String? taskType,
    String? knowledgeBaseId,
    String? documentId,
  }) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/tasks').replace(
              queryParameters: {
                'cursor': ?cursor,
                if (limit != null) 'limit': '$limit',
                'state': ?state,
                'task_type': ?taskType,
                'knowledge_base_id': ?knowledgeBaseId,
                'document_id': ?documentId,
              },
            ),
      ),
    );
    return _pageOf(_base.dataOf(response), TaskDto.fromJson);
  }

  Future<TaskDetail> getTask(String taskId) async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/tasks/$taskId')),
    );
    return TaskDetail.fromJson(_base.dataOf(response));
  }

  /// 请求取消任务（幂等；执行中转等待取消）
  Future<TaskDto> cancelTask(String taskId) async {
    final response = await _base.send(
      (client) => client.post(_base.uri('/api/v1/tasks/$taskId/cancel')),
    );
    return _taskOf(_base.dataOf(response));
  }

  /// 手动重试失败任务（202；创建携带父关系的新任务）
  Future<TaskDto> retryTask(
    String taskId, {
    required String idempotencyKey,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/tasks/$taskId/retry'),
        headers: {'Idempotency-Key': idempotencyKey},
      ),
    );
    return _taskOf(_base.dataOf(response));
  }

  /// 云端解析确认（approve/reject；仅 waiting_user + routing_parser 有效）
  Future<TaskDto> updateCloudConfirmation(
    String taskId, {
    required String decision,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/tasks/$taskId/cloud-confirmation'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'decision': decision}),
      ),
    );
    return _taskOf(_base.dataOf(response));
  }

  // ===================== 信封辅助 =====================

  ApiPage<T> _pageOf<T>(
    Map<String, dynamic> data,
    T Function(Map<String, dynamic>) fromJson,
  ) => ApiPage(
        items: [
          for (final item in (data['items'] ?? []) as List)
            fromJson(item as Map<String, dynamic>),
        ],
        nextCursor: data['next_cursor'] as String?,
      );

  TaskDto _taskOf(Map<String, dynamic> data) =>
      TaskDto.fromJson(data['task'] as Map<String, dynamic>);
}
