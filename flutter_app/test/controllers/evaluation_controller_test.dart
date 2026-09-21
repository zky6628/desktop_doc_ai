// 评测控制器测试：指标解析、筛选项加载、知识库筛选传参与防过期响应。
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:desktop_document_ai/api/knowledge_api_client.dart';
import 'package:desktop_document_ai/api/ops_api_client.dart';
import 'package:desktop_document_ai/api/query_api_client.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/controllers/evaluation_controller.dart';

http.Response envelope(Map<String, dynamic> data, {int status = 200}) =>
    http.Response.bytes(
      utf8.encode(jsonEncode({
        'success': true,
        'request_id': 'req-1',
        'data': data,
        'error': null,
      })),
      status,
      headers: {'content-type': 'application/json; charset=utf-8'},
    );

Map<String, dynamic> metricsPayload({required int total}) => {
      'p50_ttft_ms': total == 0 ? null : 700,
      'p95_ttft_ms': total == 0 ? null : 1800,
      'p99_ttft_ms': total == 0 ? null : 2400,
      'ttft_sample_size': total == 0 ? 0 : 3,
      'total': total,
      'completed': total == 0 ? 0 : 3,
      'failed': total == 0 ? 0 : 1,
      'cancelled': total == 0 ? 0 : 1,
      'refused': total == 0 ? 0 : 1,
      'degraded': total == 0 ? 0 : 1,
      'failure_rate': total == 0 ? null : 0.2,
      'input_tokens': total == 0 ? 0 : 1000,
      'output_tokens': total == 0 ? 0 : 200,
    };

Map<String, dynamic> kbPayload() => {
      'items': [
        {
          'id': 'kb-1',
          'name': '评测库',
          'description': null,
          'status': 'active',
          'deleted_at': null,
          'created_at': '2026-09-20T00:00:00+00:00',
          'updated_at': '2026-09-20T00:00:00+00:00',
        },
      ],
      'next_cursor': null,
    };

EvaluationController controllerWith(
  Future<http.Response> Function(http.Request) handler,
) {
  final store = ServerAddressStore(Uri.parse('http://127.0.0.1:8000'));
  return EvaluationController(
    queryClient: QueryApiClient(client: MockClient(handler), address: store),
    knowledgeClient: KnowledgeApiClient(client: MockClient(handler), address: store),
    opsClient: OpsApiClient(client: MockClient(handler), address: store),
  );
}

void main() {
  test('loadInitial 并行解析指标与筛选项', () async {
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 5));
      }
      return envelope(kbPayload());
    });

    await controller.loadInitial();

    expect(controller.loading, isFalse);
    expect(controller.metrics!.total, 5);
    expect(controller.metrics!.p95TtftMs, 1800);
    expect(controller.metrics!.failureRate, 0.2);
    expect(controller.knowledgeBases.single.name, '评测库');
    expect(controller.error, isNull);
  });

  test('无运行记录时指标 total 为 0（页面渲染空态）', () async {
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 0));
      }
      return envelope(kbPayload());
    });

    await controller.loadInitial();
    expect(controller.metrics!.total, 0);
  });

  test('指标失败进入错误态且不覆盖空值', () async {
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return http.Response.bytes(
          utf8.encode(jsonEncode({
            'success': false,
            'request_id': 'req-1',
            'data': null,
            'error': {
              'code': 'INTERNAL_ERROR',
              'message': '服务异常',
              'retryable': true,
            },
          })),
          500,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }
      return envelope(kbPayload());
    });

    await controller.loadInitial();
    expect(controller.error, isNotNull);
    expect(controller.error!.code, 'INTERNAL_ERROR');
    expect(controller.metrics, isNull);
  });

  test('setKbFilter 携带 knowledge_base_id 查询参数', () async {
    final metricsUrls = <Uri>[];
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        metricsUrls.add(request.url);
        return envelope(metricsPayload(total: 1));
      }
      return envelope(kbPayload());
    });

    await controller.loadInitial();
    await controller.setKbFilter('kb-1');

    expect(controller.kbFilter, 'kb-1');
    expect(metricsUrls.last.queryParameters['knowledge_base_id'], 'kb-1');
  });

  test('筛选切换后过期响应被丢弃', () async {
    // 以参数区分两路请求的闸门：后发 (kb-b) 先返回，先发 (kb-a) 的
    // 过期响应不得覆盖最新指标
    final gates = {
      'kb-a': Completer<void>(),
      'kb-b': Completer<void>(),
    };
    final controller = controllerWith((request) async {
      final kb = request.url.queryParameters['knowledge_base_id'];
      if (request.url.path != '/api/v1/metrics/queries') {
        return envelope(kbPayload());
      }
      await gates[kb]!.future;
      return envelope(metricsPayload(total: kb == 'kb-a' ? 1 : 2));
    });

    final first = controller.setKbFilter('kb-a');
    final second = controller.setKbFilter('kb-b');
    gates['kb-b']!.complete();
    await second;
    gates['kb-a']!.complete();
    await first;

    expect(controller.kbFilter, 'kb-b');
    expect(controller.metrics!.total, 2);
  });

  test('refresh 保留当前筛选', () async {
    final metricsUrls = <Uri>[];
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        metricsUrls.add(request.url);
        return envelope(metricsPayload(total: 1));
      }
      return envelope(kbPayload());
    });

    await controller.loadInitial();
    await controller.setKbFilter('kb-1');
    await controller.refresh();

    expect(metricsUrls.last.queryParameters['knowledge_base_id'], 'kb-1');
  });

  test('运行配置开关驱动调试检索可用性', () async {
    final enabled = controllerWith((request) async {
      if (request.url.path == '/api/v1/config/public') {
        return envelope({
          'model_profiles': [],
          'pipeline_configs': [],
          'limits': {},
          'features': {
            'worker_enabled': true,
            'local_debug_enabled': true,
            'cloud_parsing_available': true,
          },
        });
      }
      return envelope(kbPayload());
    });
    final disabled = controllerWith((request) async {
      if (request.url.path == '/api/v1/config/public') {
        return envelope({
          'model_profiles': [],
          'pipeline_configs': [],
          'limits': {},
          'features': {
            'worker_enabled': true,
            'local_debug_enabled': false,
            'cloud_parsing_available': true,
          },
        });
      }
      return envelope(kbPayload());
    });

    await enabled.loadInitial();
    await disabled.loadInitial();

    expect(enabled.debugAvailable, isTrue);
    expect(disabled.debugAvailable, isFalse);
  });

  test('runDebugSearch 携带知识库、问题与覆盖参数并解析结果', () async {
    final bodies = <Map<String, dynamic>>[];
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/search') {
        bodies.add(jsonDecode(request.body) as Map<String, dynamic>);
        return envelope({
          'candidates': [
            {
              'chunk_id': 'chunk-1',
              'document_version_id': 'v-1',
              'file_name': '手册.pdf',
              'section_path': ['第一章'],
              'vector_rank': 1,
              'vector_score': 0.92,
              'rrf_rank': 1,
              'rrf_score': 0.0328,
              'rerank_rank': null,
              'rerank_score': null,
            },
          ],
          'stages': {
            'vector_hits': 5,
            'keyword_hits': 3,
            'fused': 5,
            'reranked': 0,
            'rerank_degraded': true,
            'dropped_hit_count': 0,
          },
          'config': {'overridden': {'fused_top_k': 5}},
        });
      }
      return envelope(kbPayload());
    });
    await controller.setKbFilter('kb-1');

    await controller.runDebugSearch(
      question: '带薪年假',
      fusedTopK: 5,
      rerankTopN: 2,
    );

    expect(bodies.single['knowledge_base_id'], 'kb-1');
    expect(bodies.single['question'], '带薪年假');
    expect(bodies.single['fused_top_k'], 5);
    expect(bodies.single['rerank_top_n'], 2);
    expect(bodies.single.containsKey('vector_top_k'), isFalse);
    expect(controller.debugError, isNull);
    expect(controller.debugResult!.candidates.single.fileName, '手册.pdf');
    expect(controller.debugResult!.stages.rerankDegraded, isTrue);
    expect(controller.debugResult!.overridden, {'fused_top_k': 5});
  });

  test('未选定知识库或空白问题不发起调试检索', () async {
    var searchCalls = 0;
    final controller = controllerWith((request) async {
      if (request.url.path == '/api/v1/search') {
        searchCalls += 1;
        return envelope({
          'candidates': [],
          'stages': {},
          'config': {'overridden': {}},
        });
      }
      return envelope(kbPayload());
    });

    // 未选定知识库（kbFilter 为 null）
    await controller.runDebugSearch(question: '问题');
    expect(searchCalls, 0);

    // 选定后空白问题同样不发起
    await controller.setKbFilter('kb-1');
    await controller.runDebugSearch(question: '   ');
    expect(searchCalls, 0);
    expect(controller.debugResult, isNull);
  });
}
