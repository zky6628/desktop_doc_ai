// 知识库页面路由与 MinerU 确认横幅测试：详情契约路由可达、
// waiting_user 任务驱动确认横幅与对话框（mock 驱动，无真实后端）。
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:desktop_document_ai/api/knowledge_api_client.dart';

import 'support/app_test_env.dart';

http.Response envelope(Map<String, dynamic> data) => http.Response.bytes(
      utf8.encode(jsonEncode({
        'success': true,
        'request_id': 'req-1',
        'data': data,
        'error': null,
      })),
      200,
      headers: {'content-type': 'application/json; charset=utf-8'},
    );

/// 预置一个知识库与一个等待云端确认的任务
Future<KnowledgeApiClient> mockKnowledgeClient() async {
  return KnowledgeApiClient(
    client: MockClient((request) async {
      final path = request.url.path;
      if (path == '/api/v1/knowledge-bases') {
        return envelope({
          'items': [
            {
              'id': 'kb-1',
              'name': '确认测试库',
              'description': null,
              'status': 'active',
              'deleted_at': null,
              'delete_requested_at': null,
              'created_at': '2026-09-20T00:00:00+00:00',
              'updated_at': '2026-09-20T00:00:00+00:00',
            },
          ],
          'next_cursor': null,
        });
      }
      if (path.endsWith('/documents')) {
        return envelope({'items': [], 'next_cursor': null});
      }
      if (path == '/api/v1/tasks') {
        return envelope({
          'items': [
            {
              'id': 't-1',
              'task_type': 'import',
              'state': 'waiting_user',
              'stage': 'routing_parser',
              'progress': 0,
              'queue_position': null,
              'cancellable': true,
              'retryable': false,
              'retry_count': 0,
              'max_retries': 3,
              'knowledge_base_id': 'kb-1',
              'document_id': 'd-1',
              'document_version_id': 'v-1',
              'parent_task_id': null,
              'error': null,
              'created_at': '2026-09-20T00:00:00+00:00',
              'started_at': null,
              'finished_at': null,
              'document_display_name': '员工手册.pdf',
              'document_size_bytes': 2048,
              'route_mode': 'cloud',
              'route_reason': '扫描页',
            },
          ],
          'next_cursor': null,
        });
      }
      return envelope({'items': [], 'next_cursor': null});
    }),
  );
}

void main() {
  late Directory tempDir;

  setUp(() async {
    tempDir = await AppTestEnv.setUp();
  });

  tearDown(() async {
    await AppTestEnv.tearDown(tempDir);
  });

  testWidgets('MinerU 确认横幅由 waiting_user 任务驱动并打开确认对话框',
      (WidgetTester tester) async {
    final preferences = {'ui.last_kb_id': 'kb-1'};
    final knowledgeClient = await mockKnowledgeClient();
    await tester.pumpWidget(
      await buildWorkbenchAppWithKnowledge(
        preferences: preferences,
        knowledgeClient: knowledgeClient,
      ),
    );
    await settleChatConnection(tester);

    // 切到知识库页：自动恢复最近 KB 后横幅出现
    final rail = find.descendant(
      of: find.byType(NavigationRail),
      matching: find.byIcon(Icons.library_books_outlined),
    );
    await tester.tap(rail.first);
    // 知识库页初始化链路（列表→选择→文档→确认待办）需要多轮事件循环
    for (var i = 0; i < 10; i++) {
      await tester.runAsync(() async {
        await Future<void>.delayed(const Duration(milliseconds: 50));
      });
      await tester.pump();
      if (find.text('1 待确认').evaluate().isNotEmpty) break;
    }

    expect(find.text('1 待确认'), findsOneWidget);

    // 打开确认对话框：文件名、大小与隐私提示在场
    await tester.tap(find.text('1 待确认'));
    await tester.pumpAndSettle();
    expect(find.text('云端解析确认'), findsOneWidget);
    expect(find.text('员工手册.pdf'), findsOneWidget);
    expect(find.text('2.0 KB · 路由原因：扫描页'), findsOneWidget);
    expect(find.text('全部批准'), findsOneWidget);
    expect(find.text('全部拒绝'), findsOneWidget);
  });

  testWidgets('任务中心：队列行展示与详情事件时间线展开', (WidgetTester tester) async {
    var listCalls = 0;
    Future<http.Response> tasksResponse() async {
      listCalls += 1;
      final state = listCalls == 1 ? 'queued' : 'succeeded';
      return http.Response.bytes(
        utf8.encode(jsonEncode({
          'success': true,
          'request_id': 'req-1',
          'data': {
            'items': [
              {
                'id': 't-9',
                'task_type': 'import',
                'state': state,
                'stage': 'parsing_local',
                'progress': 0.2,
                'queue_position': 1,
                'cancellable': true,
                'retryable': false,
                'retry_count': 0,
                'max_retries': 3,
                'knowledge_base_id': 'kb-1',
                'document_id': 'd-1',
                'document_version_id': null,
                'parent_task_id': null,
                'error': null,
                'created_at': '2026-09-20T15:40:00+00:00',
                'started_at': null,
                'finished_at': null,
                'document_display_name': '迪士尼公司发展分析.pdf',
                'document_size_bytes': 9437184,
                'route_mode': null,
                'route_reason': null,
              },
            ],
            'next_cursor': null,
          },
          'error': null,
        })),
        200,
        headers: {'content-type': 'application/json; charset=utf-8'},
      );
    }

    final knowledgeClient = KnowledgeApiClient(
      client: MockClient((request) async {
        final path = request.url.path;
        if (path == '/api/v1/tasks') return tasksResponse();
        if (path == '/api/v1/tasks/t-9') {
          return http.Response.bytes(
            utf8.encode(jsonEncode({
              'success': true,
              'request_id': 'req-2',
              'data': {
                'task': {
                  'id': 't-9',
                  'task_type': 'import',
                  'state': 'queued',
                  'stage': 'parsing_local',
                  'progress': 0.2,
                  'queue_position': 1,
                  'cancellable': true,
                  'retryable': false,
                  'retry_count': 0,
                  'max_retries': 3,
                  'knowledge_base_id': 'kb-1',
                  'document_id': 'd-1',
                  'document_version_id': null,
                  'parent_task_id': null,
                  'error': null,
                  'created_at': '2026-09-20T15:40:00+00:00',
                  'started_at': null,
                  'finished_at': null,
                  'document_display_name': null,
                  'document_size_bytes': null,
                  'route_mode': null,
                  'route_reason': null,
                },
                'recent_events': [
                  {
                    'id': 'ev-1',
                    'event_type': 'created',
                    'state': 'queued',
                    'stage': null,
                    'attempt_count': 0,
                    'worker': null,
                    'created_at': '2026-09-20T15:40:00+00:00',
                    'error_code': null,
                    'detail_json': null,
                  },
                ],
              },
              'error': null,
            })),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        return http.Response.bytes(
          utf8.encode(jsonEncode({
            'success': true,
            'request_id': 'req-x',
            'data': {'items': [], 'next_cursor': null},
            'error': null,
          })),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    final preferences = {'ui.last_location': '/tasks'};
    await tester.pumpWidget(
      await buildWorkbenchAppWithKnowledge(
        preferences: preferences,
        knowledgeClient: knowledgeClient,
      ),
    );
    await settleChatConnection(tester);

    // 队列行展示：settle 过程已推进一轮 5 秒自动轮询（mock 第二次
    // 返回终态），任务行应显示为成功
    expect(find.text('迪士尼公司发展分析.pdf'), findsOneWidget);
    expect(find.text('成功'), findsOneWidget);

    // 点击行展开详情：事件时间线在场
    await tester.tap(find.text('迪士尼公司发展分析.pdf'));
    await tester.pumpAndSettle();
    expect(find.text('事件时间线'), findsOneWidget);
    expect(find.text('created'), findsOneWidget);
  });
}
