import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

import 'api_error.dart';
import 'dto/query_dto.dart';

/// 请求超时：本机服务场景下超过即按网络故障处理
const _requestTimeout = Duration(seconds: 15);

/// SSE 连接工厂：按 Last-Event-ID 打开事件流，返回响应与连接关闭句柄
typedef SseConnect = Future<(http.StreamedResponse, void Function())>
    Function(int lastEventId);

/// 查询域 API 客户端：查询创建/聚合/取消/客户端遥测 + SSE 连接工厂
///
/// 统一解析 v1 信封；业务错误抛出 [ApiException]，网络层故障归类为
/// NETWORK_ERROR。SSE 连接每次独立建立，断开由调用方关闭句柄。
class QueryApiClient {
  QueryApiClient({
    http.Client? client,
    Uri? baseUrl,
    this.instanceId,
  })  : _client = client ?? http.Client(),
        baseUrl = baseUrl ?? Uri.parse('http://127.0.0.1:8000');

  /// 本机服务地址仅在设置页可配置（后续任务接入）
  final Uri baseUrl;

  /// 客户端实例标识（客户端本地生成并持久化的随机 UUID），仅遥测头携带
  final String? instanceId;

  final http.Client _client;

  // ===================== 查询端点 =====================

  /// 创建查询运行（202 + 流地址）；幂等键命中时服务端返回既有查询
  Future<QueryCreated> createQuery({
    required String knowledgeBaseId,
    required String question,
    String? conversationId,
    String? idempotencyKey,
  }) async {
    final response = await _send(
      () => _client.post(
            _uri('/api/v1/queries'),
            headers: {
              'Content-Type': 'application/json',
              'Idempotency-Key': ?idempotencyKey,
            },
            body: jsonEncode({
              'knowledge_base_id': knowledgeBaseId,
              'question': question,
              'conversation_id': ?conversationId,
            }),
          ),
    );
    return QueryCreated.fromJson(_dataOf(response));
  }

  /// 聚合结果：断线恢复与最终正文/引用的权威来源
  Future<QueryAggregate> getAggregate(String queryId) async {
    final response = await _send(
      () => _client.get(_uri('/api/v1/queries/$queryId')),
    );
    return QueryAggregate.fromJson(_dataOf(response));
  }

  /// 请求取消（幂等）；取消在服务端检查点生效，返回迁移后的状态
  Future<QueryRunStatus> cancelQuery(String queryId) async {
    final response = await _send(
      () => _client.post(_uri('/api/v1/queries/$queryId/cancel')),
    );
    final data = _dataOf(response);
    return QueryRunStatus.fromName(data['state'] as String);
  }

  /// 上报客户端遥测（204；幂等，服务端忽略重复上报）
  Future<void> reportClientMetrics({
    required String queryId,
    required DateTime clientSendAt,
    required DateTime firstSseTokenReceivedAt,
    required DateTime firstTokenRenderedAt,
    Map<String, String>? networkContext,
  }) async {
    final response = await _send(
      () => _client.post(
            _uri('/api/v1/queries/$queryId/client-metrics'),
            headers: {
              'Content-Type': 'application/json',
              'X-Client-Instance-Id': ?instanceId,
            },
            body: jsonEncode({
              'client_send_at': _isoUtc(clientSendAt),
              'first_sse_token_received_at': _isoUtc(firstSseTokenReceivedAt),
              'first_token_rendered_at': _isoUtc(firstTokenRenderedAt),
              'network_context': ?networkContext,
            }),
          ),
      expectsNoContent: true,
    );
    if (response.statusCode != 204) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '遥测上报返回非 204 状态: ${response.statusCode}',
        statusCode: response.statusCode,
      );
    }
  }

  // ===================== SSE 连接 =====================

  /// 打开查询事件流；Last-Event-ID 为 0 时不携带（全量重放）
  ///
  /// 返回的关闭句柄终止底层连接（断开/终态/放弃重连时由调用方触发）
  Future<(http.StreamedResponse, void Function())> openEventStream(
    String queryId,
    int lastEventId,
  ) async {
    // 每条连接独立 Client：关闭句柄即终止该连接，不污染其他请求
    final io = IOClient();
    try {
      final request = http.Request(
        'GET',
        _uri('/api/v1/queries/$queryId/events'),
      )..headers['Accept'] = 'text/event-stream';
      if (lastEventId > 0) {
        request.headers['Last-Event-ID'] = '$lastEventId';
      }
      final response = await io.send(request).timeout(_requestTimeout);
      return (response, io.close);
    } on Exception {
      io.close();
      rethrow;
    }
  }

  // ===================== 信封解析 =====================

  Uri _uri(String path) => baseUrl.replace(path: path);

  Future<http.Response> _send(
    Future<http.Response> Function() action, {
    bool expectsNoContent = false,
  }) async {
    http.Response response;
    try {
      response = await action().timeout(_requestTimeout);
    } on TimeoutException catch (error) {
      throw ApiException.network('请求超时: $error');
    } on IOException catch (error) {
      throw ApiException.network('网络请求失败: $error');
    } on http.ClientException catch (error) {
      throw ApiException.network('网络请求失败: ${error.message}');
    }
    if (expectsNoContent && response.statusCode == 204) return response;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw _errorOf(response);
    }
    return response;
  }

  /// 解析信封 data；success=false 或结构违约时抛出异常
  Map<String, dynamic> _dataOf(http.Response response) {
    dynamic body;
    try {
      body = jsonDecode(response.body);
    } on FormatException {
      throw ApiException(
        code: 'UNKNOWN',
        message: '响应不是合法 JSON',
        statusCode: response.statusCode,
      );
    }
    if (body is! Map<String, dynamic> || !body.containsKey('success')) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '响应不符合 v1 信封结构',
        statusCode: response.statusCode,
      );
    }
    if (body['success'] != true) {
      throw _envelopeError(body, response.statusCode);
    }
    final data = body['data'];
    if (data is! Map<String, dynamic>) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '信封 data 缺失或不是对象',
        statusCode: response.statusCode,
        requestId: body['request_id'] as String?,
      );
    }
    return data;
  }

  ApiException _envelopeError(Map<String, dynamic> body, int statusCode) {
    final errorJson = body['error'];
    final error = errorJson is Map<String, dynamic>
        ? ApiError.fromJson(errorJson)
        : const ApiError(code: 'UNKNOWN', message: '未提供错误信息');
    return ApiException(
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      statusCode: statusCode,
      requestId: body['request_id'] as String?,
    );
  }

  /// 非 2xx 响应：优先解析信封错误，无法解析时按 UNKNOWN 表达
  ApiException _errorOf(http.Response response) {
    dynamic body;
    try {
      body = jsonDecode(response.body);
    } on FormatException {
      body = null;
    }
    if (body is Map<String, dynamic> &&
        (body['success'] == false || body.containsKey('error'))) {
      return _envelopeError(body, response.statusCode);
    }
    return ApiException(
      code: 'UNKNOWN',
      message: '服务返回 ${response.statusCode} 且未提供信封错误',
      statusCode: response.statusCode,
    );
  }

  /// ISO-8601 UTC（Dart 零毫秒时省略小数部分，服务端 fromisoformat 兼容）
  String _isoUtc(DateTime value) => value.toUtc().toIso8601String();
}
