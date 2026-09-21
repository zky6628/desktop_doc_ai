// 查询 API 客户端测试：信封解析、DTO 还原、遥测头与时间戳格式、
// 网络故障与合同违约的归类。HTTP 层以 MockClient 注入。
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:desktop_document_ai/api/api_error.dart';
import 'package:desktop_document_ai/api/dto/query_dto.dart';
import 'package:desktop_document_ai/api/query_api_client.dart';
import 'package:desktop_document_ai/app/server_address.dart';

/// 构造 v1 信封响应体（UTF-8 字节承载中文，需声明 charset 供 body 解码）
http.Response envelopeResponse(
  int statusCode,
  Map<String, dynamic> json,
) => http.Response.bytes(
      utf8.encode(jsonEncode(json)),
      statusCode,
      headers: {'content-type': 'application/json; charset=utf-8'},
    );

/// 构造 v1 信封 JSON
Map<String, dynamic> envelope({
  required bool success,
  Map<String, dynamic>? data,
  String code = 'UNKNOWN',
  String message = 'm',
  bool retryable = false,
  String requestId = 'req-1',
}) {
  return {
    'success': success,
    'request_id': requestId,
    'data': data,
    'error': success
        ? null
        : {'code': code, 'message': message, 'retryable': retryable},
  };
}

QueryApiClient clientWith(
  Future<http.Response> Function(http.Request) handler, {
  String? instanceId,
}) {
  return QueryApiClient(
    client: MockClient(handler),
    address: ServerAddressStore(Uri.parse('http://127.0.0.1:8000')),
    instanceId: instanceId,
  );
}

void main() {
  group('createQuery', () {
    test('解析 202 载荷并透传幂等键与请求体', () async {
      http.Request? captured;
      final client = clientWith((request) async {
        captured = request;
        return envelopeResponse(
          202,
          envelope(
            success: true,
            data: {
              'query_id': 'q-1',
              'stream_url': '/api/v1/queries/q-1/events',
              'state': 'queued',
            },
          ),
        );
      });

      final created = await client.createQuery(
        knowledgeBaseId: 'kb-1',
        question: '公司的年假制度是什么？',
        idempotencyKey: 'idem-1',
      );

      expect(created.queryId, 'q-1');
      expect(created.streamUrl, '/api/v1/queries/q-1/events');
      expect(created.state, QueryRunStatus.queued);
      expect(captured!.url.path, '/api/v1/queries');
      expect(captured!.headers['Idempotency-Key'], 'idem-1');
      final body = jsonDecode(captured!.body) as Map<String, dynamic>;
      expect(body['knowledge_base_id'], 'kb-1');
      expect(body['question'], '公司的年假制度是什么？');
    });

    test('410 信封错误抛出异常并携带请求 ID', () async {
      final client = clientWith(
        (_) async => envelopeResponse(
          410,
          envelope(
            success: false,
            code: 'KNOWLEDGE_BASE_DELETED',
            message: '知识库已删除',
            requestId: 'req-9',
          ),
        ),
      );

      await expectLater(
        client.createQuery(knowledgeBaseId: 'kb-1', question: 'q'),
        throwsA(
          isA<ApiException>()
              .having((e) => e.code, 'code', 'KNOWLEDGE_BASE_DELETED')
              .having((e) => e.statusCode, 'statusCode', 410)
              .having((e) => e.requestId, 'requestId', 'req-9')
              .having((e) => e.isRetryable, 'retryable', isFalse),
        ),
      );
    });
  });

  group('getAggregate', () {
    test('还原聚合结果与引用快照（可选字段缺失容忍）', () async {
      final client = clientWith(
        (_) async => envelopeResponse(
          200,
          envelope(
            success: true,
            data: {
              'query_id': 'q-1',
              'state': 'completed',
              'refused': false,
              'rerank_degraded': true,
              'server_ttft_ms': 2850,
              'total_ms': 6800,
              'error': null,
              'answer': '答案正文',
              'citations': [
                {
                  'id': 'c-1',
                  'chunk_id': 'ch-1',
                  'document_id': 'd-1',
                  'document_version_id': 'dv-1',
                  'file_name': '员工手册.pdf',
                  'version_no': 2,
                  'page_no': 12,
                  'section_path': '休假制度/年假',
                  'content': '员工累计工作满一年后...',
                  'validation_state': 'valid',
                },
              ],
            },
          ),
        ),
      );

      final aggregate = await client.getAggregate('q-1');

      expect(aggregate.state, QueryRunStatus.completed);
      expect(aggregate.rerankDegraded, isTrue);
      expect(aggregate.answer, '答案正文');
      final citation = aggregate.citations.single;
      expect(citation.fileName, '员工手册.pdf');
      expect(citation.pageNo, 12);
      expect(citation.sectionPath, '休假制度/年假');
      expect(citation.citationOrder, isNull);
      expect(citation.rerankScore, isNull);
    });
  });

  group('cancelQuery', () {
    test('返回迁移后的状态枚举', () async {
      http.Request? captured;
      final client = clientWith((request) async {
        captured = request;
        return envelopeResponse(
          200,
          envelope(
            success: true,
            data: {'query_id': 'q-1', 'state': 'cancel_requested'},
          ),
        );
      });

      final state = await client.cancelQuery('q-1');

      expect(state, QueryRunStatus.cancelRequested);
      expect(captured!.url.path, '/api/v1/queries/q-1/cancel');
    });
  });

  group('reportClientMetrics', () {
    test('以 ISO-UTC 时间戳与实例头上报并接受 204', () async {
      http.Request? captured;
      final client = clientWith((request) async {
        captured = request;
        return http.Response('', 204);
      }, instanceId: 'inst-1');

      await client.reportClientMetrics(
        queryId: 'q-1',
        clientSendAt: DateTime.utc(2026, 9, 20, 1, 0, 0),
        firstSseTokenReceivedAt: DateTime.utc(2026, 9, 20, 1, 0, 3, 820),
        firstTokenRenderedAt: DateTime.utc(2026, 9, 20, 1, 0, 3, 910),
      );

      expect(captured!.url.path, '/api/v1/queries/q-1/client-metrics');
      expect(captured!.headers['X-Client-Instance-Id'], 'inst-1');
      final body = jsonDecode(captured!.body) as Map<String, dynamic>;
      expect(body['client_send_at'], '2026-09-20T01:00:00.000Z');
      expect(body['first_sse_token_received_at'], '2026-09-20T01:00:03.820Z');
      expect(body['first_token_rendered_at'], '2026-09-20T01:00:03.910Z');
    });
  });

  group('queryMetrics', () {
    test('解析聚合指标并透传知识库筛选', () async {
      http.Request? captured;
      final client = clientWith((request) async {
        captured = request;
        return envelopeResponse(200, envelope(success: true, data: {
          'p50_ttft_ms': 700,
          'p95_ttft_ms': 1800,
          'p99_ttft_ms': 2400,
          'ttft_sample_size': 3,
          'total': 5,
          'completed': 3,
          'failed': 1,
          'cancelled': 1,
          'refused': 1,
          'degraded': 1,
          'failure_rate': 0.2,
          'input_tokens': 1000,
          'output_tokens': 200,
        }));
      });

      final metrics = await client.queryMetrics(knowledgeBaseId: 'kb-1');

      expect(captured!.url.path, '/api/v1/metrics/queries');
      expect(captured!.url.queryParameters['knowledge_base_id'], 'kb-1');
      expect(metrics.p95TtftMs, 1800);
      expect(metrics.ttftSampleSize, 3);
      expect(metrics.failureRate, 0.2);
    });

    test('无样本时分位与失败率为 null', () async {
      final client = clientWith(
        (_) async => envelopeResponse(200, envelope(success: true, data: {
              'p50_ttft_ms': null,
              'p95_ttft_ms': null,
              'p99_ttft_ms': null,
              'ttft_sample_size': 0,
              'total': 0,
              'completed': 0,
              'failed': 0,
              'cancelled': 0,
              'refused': 0,
              'degraded': 0,
              'failure_rate': null,
              'input_tokens': 0,
              'output_tokens': 0,
            })),
      );

      final metrics = await client.queryMetrics();

      expect(metrics.p95TtftMs, isNull);
      expect(metrics.failureRate, isNull);
      expect(metrics.total, 0);
    });
  });

  group('地址切换', () {
    test('地址源更新后请求立即走新地址', () async {
      final store = ServerAddressStore(Uri.parse('http://127.0.0.1:8000'));
      final urls = <Uri>[];
      final client = QueryApiClient(
        client: MockClient((request) async {
          urls.add(request.url);
          return http.Response('', 204);
        }),
        address: store,
      );
      Future<void> report() => client.reportClientMetrics(
            queryId: 'q-1',
            clientSendAt: DateTime.utc(2026, 9, 20, 1),
            firstSseTokenReceivedAt: DateTime.utc(2026, 9, 20, 1, 0, 1),
            firstTokenRenderedAt: DateTime.utc(2026, 9, 20, 1, 0, 2),
          );

      await report();
      store.update(Uri.parse('http://127.0.0.1:9000'));
      await report();

      expect(urls.first.port, 8000);
      expect(urls.last.port, 9000);
    });
  });

  group('故障归类', () {
    test('网络异常归类为 NETWORK_ERROR 且可重试', () async {
      final client = QueryApiClient(
        client: MockClient(
          (_) => throw http.ClientException('connection refused'),
        ),
        address: ServerAddressStore(Uri.parse('http://127.0.0.1:8000')),
      );

      await expectLater(
        client.getAggregate('q-1'),
        throwsA(
          isA<ApiException>()
              .having((e) => e.code, 'code', 'NETWORK_ERROR')
              .having((e) => e.isRetryable, 'retryable', isTrue),
        ),
      );
    });

    test('非信封响应归类为 UNKNOWN', () async {
      final client = clientWith(
        (_) async => http.Response('<html>gateway error</html>', 503),
      );

      await expectLater(
        client.getAggregate('q-1'),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'UNKNOWN'),
        ),
      );
    });
  });
}
