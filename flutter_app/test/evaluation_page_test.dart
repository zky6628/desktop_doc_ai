// 评测页 Widget 测试：指标卡片、空态与错误重试态。
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:desktop_document_ai/api/knowledge_api_client.dart';
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
        ),
      ),
    ),
  );
  await tester.pump();
}

void main() {
  testWidgets('有运行记录时渲染指标卡片', (tester) async {
    await pumpEvaluation(tester, (request) async {
      if (request.url.path == '/api/v1/metrics/queries') {
        return envelope(metricsPayload(total: 5));
      }
      return envelope(kbPayload());
    });
    await tester.pump();

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
}
