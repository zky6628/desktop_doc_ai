import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

import 'api_client_base.dart';
import 'api_error.dart';
import 'dto/metrics_dto.dart';
import 'dto/query_dto.dart';
import '../app/server_address.dart';

/// SSE 连接工厂：按 Last-Event-ID 打开事件流，返回响应与连接关闭句柄
typedef SseConnect = Future<(http.StreamedResponse, void Function())>
Function(int lastEventId);

/// 查询域 API 客户端：查询创建/聚合/取消/客户端遥测 + SSE 连接工厂
///
/// 信封与错误解析复用 [ApiClientBase]；SSE 连接每次独立建立，断开
/// 由调用方关闭句柄。
class QueryApiClient {
  QueryApiClient({
    http.Client? client,
    required ServerAddressStore address,
    this.instanceId,
  }) : _base = ApiClientBase(client: client, address: address);

  /// 客户端实例标识（客户端本地生成并持久化的随机 UUID），仅遥测头携带
  final String? instanceId;

  final ApiClientBase _base;

  // ===================== 查询端点 =====================

  /// 创建查询运行（202 + 流地址）；幂等键命中时服务端返回既有查询
  Future<QueryCreated> createQuery({
    required String knowledgeBaseId,
    required String question,
    String? conversationId,
    String? idempotencyKey,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/queries'),
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
    return QueryCreated.fromJson(_base.dataOf(response));
  }

  /// 聚合结果：断线恢复与最终正文/引用的权威来源
  Future<QueryAggregate> getAggregate(String queryId) async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/queries/$queryId')),
    );
    return QueryAggregate.fromJson(_base.dataOf(response));
  }

  /// 请求取消（幂等）；取消在服务端检查点生效，返回迁移后的状态
  Future<QueryRunStatus> cancelQuery(String queryId) async {
    final response = await _base.send(
      (client) => client.post(_base.uri('/api/v1/queries/$queryId/cancel')),
    );
    final data = _base.dataOf(response);
    return QueryRunStatus.fromName(data['state'] as String);
  }

  /// 查询聚合指标：TTFT 分位与状态计数（评测页消费）
  Future<QueryMetricsSummary> queryMetrics({String? knowledgeBaseId}) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/metrics/queries').replace(
              queryParameters: {'knowledge_base_id': ?knowledgeBaseId},
            ),
      ),
    );
    return QueryMetricsSummary.fromJson(_base.dataOf(response));
  }

  /// 重置查询指标：删除全部失败与取消的运行（失败污染清除），成功
  /// 历史与 TTFT/token 聚合保留。返回删除的运行数
  Future<int> resetQueryMetrics() async {
    final response = await _base.send(
      (client) => client.delete(_base.uri('/api/v1/metrics/queries')),
    );
    final data = _base.dataOf(response);
    return (data['deleted'] as num).toInt();
  }

  /// 上报客户端遥测（204；幂等，服务端忽略重复上报）
  Future<void> reportClientMetrics({
    required String queryId,
    required DateTime clientSendAt,
    required DateTime firstSseTokenReceivedAt,
    required DateTime firstTokenRenderedAt,
    Map<String, String>? networkContext,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/queries/$queryId/client-metrics'),
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
        _base.uri('/api/v1/queries/$queryId/events'),
      )..headers['Accept'] = 'text/event-stream';
      if (lastEventId > 0) {
        request.headers['Last-Event-ID'] = '$lastEventId';
      }
      final response = await io.send(request).timeout(apiRequestTimeout);
      return (response, io.close);
    } on Exception {
      io.close();
      rethrow;
    }
  }

  /// ISO-8601 UTC（Dart 零毫秒时省略小数部分，服务端 fromisoformat 兼容）
  String _isoUtc(DateTime value) => value.toUtc().toIso8601String();
}
