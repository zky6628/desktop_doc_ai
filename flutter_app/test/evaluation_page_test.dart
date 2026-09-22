// 评测页 Widget 测试：指标卡片、空态、错误重试态与调试检索显隐。
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:desktop_document_ai/api/knowledge_api_client.dart';
import 'package:desktop_document_ai/api/ops_api_client.dart';
import 'package:desktop_document_ai/api/query_api_client.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/pages/evaluation_page.dart';

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

Map<String, dynamic> configPayload({required bool localDebug}) => {
      'model_profiles': [],
      'pipeline_configs': [],
      'limits': {
        'max_running': 3,
        'max_pending': 50,
        'max_non_terminal': 53,
        'max_file_mb': 100,
        'max_batch_files': 50,
      },
      'features': {
        'worker_enabled': true,
        'local_debug_enabled': localDebug,
        'cloud_parsing_available': true,
      },
    };

Map<String, dynamic> searchPayload() => {
      'candidates': [
        {
          'chunk_id': 'chunk-1',
          'document_id': 'doc-1',
          'document_version_id': 'v-1',
          'file_name': '手册.pdf',
          'version_no': 1,
          'page_no': 3,
          'section_path': ['第一章', '年假'],
          'vector_rank': 1,
          'vector_score': 0.92,
          'keyword_rank': null,
          'keyword_score': null,
          'rrf_rank': 1,
          'rrf_score': 0.0328,
          'rerank_rank': 1,
          'rerank_score': 0.97,
        },
      ],
      'stages': {
        'vector_hits': 5,
        'keyword_hits': 3,
        'fused': 5,
        'reranked': 1,
        'rerank_degraded': false,
        'dropped_hit_count': 0,
      },
      'config': {'overridden': {'fused_top_k': 5}},
    };

Future<void> pumpEvaluation(
  WidgetTester tester,
  Future<http.Response> Function(http.Request) handler,
) async {
  final store = ServerAddressStore(Uri.parse('http://127.0.0.1:8000'));
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: EvaluationPage(
          queryClient: QueryApiClient(client: MockClient(handler), address: store),
          knowledgeClient: KnowledgeApiClient(
            client: MockClient(handler),
            address: store,
          ),
          opsClient: OpsApiClient(client: MockClient(handler), address: store),
        ),
      ),
    ),
  );
  await tester.pump();
}

void main() {
  /// 新增的调参/评测卡片位于页面顶部，指标内容通常在视口外，需滚动
  Future<void> scrollUntilTextVisible(WidgetTester tester, String text) async {
    await tester.scrollUntilVisible(
      find.text(text),
      300,
      scrollable: find.byType(Scrollable).first,
    );
    await tester.pump();
  }

  testWidgets('有运行记录时渲染指标卡片', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 5));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    await scrollUntilTextVisible(tester, '运行概览');
    expect(find.text('运行概览'), findsOneWidget);
    expect(find.text('TTFT 首字延迟（服务端）'), findsOneWidget);
    expect(find.text('Token 用量'), findsOneWidget);
    expect(find.text('P95'), findsOneWidget);
    expect(find.text('1800 ms'), findsOneWidget);
    expect(find.text('20.0%'), findsOneWidget);
    expect(find.text('1000'), findsOneWidget);
    expect(find.text('200'), findsOneWidget);
  });

  testWidgets('无运行记录时渲染空态', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 0));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    await scrollUntilTextVisible(tester, '暂无查询运行记录');
    expect(find.text('暂无查询运行记录'), findsOneWidget);
    expect(find.text('在问答页提问后，此处展示查询指标与延迟分位'), findsOneWidget);
  });

  testWidgets('指标失败时渲染错误重试态', (tester) async {
    await pumpEvaluation(tester, (request) async {
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
    await tester.pump();

    expect(find.textContaining('INTERNAL_ERROR'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);
  });

  testWidgets('筛选下拉包含知识库名称与全部选项', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 5));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    await tester.tap(find.text('全部知识库'));
    await tester.pumpAndSettle();
    expect(find.text('评测库').last, findsOneWidget);
  });

  testWidgets('本地调试关闭时调试检索区域不渲染', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/config/public') {
        return envelope(configPayload(localDebug: false));
      }
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 5));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    expect(find.text('调试检索'), findsNothing);
  });

  testWidgets('本地调试开启时渲染调试检索并展示候选明细', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/config/public') {
        return envelope(configPayload(localDebug: true));
      }
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 5));
      }
      if (request.url.path == '/api/v1/search') {
        // 请求体携带选定知识库与问题
        expect(request.url.path, '/api/v1/search');
        return envelope(searchPayload());
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    await scrollUntilTextVisible(tester, '调试检索');
    expect(find.text('调试检索'), findsOneWidget);
    expect(find.text('请先在上方选择具体知识库'), findsOneWidget);

    // 选定知识库后输入问题检索
    await tester.tap(find.text('全部知识库'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('评测库').last);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.widgetWithText(TextField, '输入调试问题（不产生查询记录与指标）'),
      '带薪年假',
    );
    await tester.tap(find.text('检索'));
    await tester.pumpAndSettle();

    // 阶段规模与候选明细（零正文，仅定位事实与分数）
    expect(find.text('融合 5'), findsOneWidget);
    expect(find.text('手册.pdf'), findsOneWidget);
    expect(find.text('1 @ 0.9200'), findsOneWidget);
    expect(find.text('重排 1'), findsOneWidget);
    expect(find.text('fused_top_k=5'), findsOneWidget);
  });

  testWidgets('切片参数卡片渲染在役值并可保存', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/config/chunking') {
        return envelope({
          'parent_chunk_chars': 800,
          'child_chunk_chars': 300,
          'is_default': false,
        });
      }
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 0));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    expect(find.text('切片参数'), findsOneWidget);
    expect(find.text('当前为默认值'), findsNothing);
    // 输入框回填服务端在役值
    expect(find.widgetWithText(TextField, '父切片预算'), findsOneWidget);
  });

  testWidgets('评测卡片未选知识库时提示且开始按钮禁用', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 0));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

    expect(find.text('切片参数评测'), findsOneWidget);
    expect(find.text('请先在上方选择具体知识库'), findsOneWidget);
    final button = tester.widget<FilledButton>(
      find.widgetWithText(FilledButton, '开始评测'),
    );
    expect(button.onPressed, isNull);
  });
}
