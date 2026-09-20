// 知识库域客户端与控制器测试：信封/DTO 解析、任务联查字段、
// 控制器选择持久化与错误分类。HTTP 层以 MockClient 注入。
import 'dart:async';
import 'dart:convert';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/api/api_error.dart';
import 'package:desktop_document_ai/api/dto/knowledge_dto.dart';
import 'package:desktop_document_ai/api/knowledge_api_client.dart';
import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/controllers/document_detail_controller.dart';
import 'package:desktop_document_ai/controllers/knowledge_base_controller.dart';
import 'package:desktop_document_ai/controllers/task_center_controller.dart';

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

KnowledgeApiClient clientWith(
  Future<http.Response> Function(http.Request) handler,
) => KnowledgeApiClient(
      client: MockClient(handler),
      baseUrl: Uri.parse('http://127.0.0.1:8000'),
    );

void main() {
  test('listKnowledgeBases 解析分页信封', () async {
    final client = clientWith((request) async {
      expect(request.url.path, '/api/v1/knowledge-bases');
      return envelope({
        'items': [
          {
            'id': 'kb-1',
            'name': '测试库',
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
    });

    final page = await client.listKnowledgeBases();
    expect(page.items.single.name, '测试库');
    expect(page.hasMore, isFalse);
  });

  test('listTasks 还原任务联查字段与线上状态枚举', () async {
    final client = clientWith((request) async {
      expect(request.url.queryParameters['state'], 'waiting_user');
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
    });

    final page = await client.listTasks(state: 'waiting_user');
    final task = page.items.single;
    expect(task.state, TaskWireStatus.waitingUser);
    expect(task.documentDisplayName, '员工手册.pdf');
    expect(task.documentSizeBytes, 2048);
    expect(task.routeReason, '扫描页');
  });

  test('非 2xx 信封错误归类为 ApiException', () async {
    final client = clientWith(
      (_) async => http.Response.bytes(
        utf8.encode(jsonEncode({
          'success': false,
          'request_id': 'req-2',
          'data': null,
          'error': {
            'code': 'NAME_CONFLICT',
            'message': '活动知识库名称已存在',
            'retryable': false,
          },
        })),
        409,
        headers: {'content-type': 'application/json; charset=utf-8'},
      ),
    );

    await expectLater(
      client.createKnowledgeBase('重名库'),
      throwsA(
        isA<ApiException>().having((e) => e.code, 'code', 'NAME_CONFLICT'),
      ),
    );
  });

  testWidgets('控制器选择知识库后持久化最近选择', (tester) async {
    SharedPreferences.setMockInitialValues({
      'ui.last_kb_id': 'kb-1',
    });
    final preferences = await AppPreferences.load();
    final client = clientWith((request) async {
      final path = request.url.path;
      if (path == '/api/v1/knowledge-bases') {
        return envelope({
          'items': [
            {
              'id': 'kb-1',
              'name': '恢复库',
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
      if (path == '/api/v1/tasks') {
        return envelope({'items': [], 'next_cursor': null});
      }
      return envelope({'items': [], 'next_cursor': null});
    });

    String? shellKbName;
    final controller = KnowledgeBaseController(
      knowledgeClient: client,
      preferences: preferences,
      onCurrentKnowledgeBase: (name) => shellKbName = name,
    );
    await controller.loadInitial();

    expect(controller.currentKb?.id, 'kb-1');
    expect(shellKbName, '恢复库');
    expect(preferences.lastKnowledgeBaseId, 'kb-1');
    controller.dispose();
  });

  test('并发刷新互不作废：文档与确认待办的响应均生效', () async {
    SharedPreferences.setMockInitialValues({'ui.last_kb_id': 'kb-1'});
    final preferences = await AppPreferences.load();
    final client = clientWith((request) async {
      final path = request.url.path;
      if (path == '/api/v1/knowledge-bases') {
        return envelope({
          'items': [
            {
              'id': 'kb-1',
              'name': '并发库',
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
        // 人为延迟：让确认待办的响应先于文档响应返回
        await Future<void>.delayed(const Duration(milliseconds: 60));
        return envelope({
          'items': [
            {
              'id': 'd-1',
              'knowledge_base_id': 'kb-1',
              'display_name': '迪士尼公司发展分析.pdf',
              'status': 'ready',
              'deleted_at': null,
              'created_at': '2026-09-20T00:00:00+00:00',
              'updated_at': '2026-09-20T00:00:00+00:00',
              'active_version': null,
              'chunk_count': 13,
              'latest_task': null,
            },
          ],
          'next_cursor': null,
        });
      }
      if (path == '/api/v1/tasks') {
        return envelope({'items': [], 'next_cursor': null});
      }
      return envelope({'items': [], 'next_cursor': null});
    });

    final controller = KnowledgeBaseController(
      knowledgeClient: client,
      preferences: preferences,
    );
    await controller.loadInitial();

    // 模拟顶栏刷新按钮的三连并发调用
    unawaited(controller.reloadKnowledgeBases());
    unawaited(controller.reloadDocuments());
    unawaited(controller.reloadPendingConfirmations());
    await Future<void>.delayed(const Duration(milliseconds: 150));

    // 旧实现共享守卫：文档响应会被更晚的待办请求作废（状态停留旧值）
    expect(controller.documents.single.status, 'ready');
    expect(controller.documents.single.chunkCount, 13);
    expect(controller.pendingConfirmations, isEmpty);
    controller.dispose();
  });

  test('详情删除幂等：目标已不存在同样视为成功', () async {
    SharedPreferences.setMockInitialValues({});
    final client = clientWith(
      (_) async => http.Response.bytes(
        utf8.encode(jsonEncode({
          'success': false,
          'request_id': 'req-3',
          'data': null,
          'error': {
            'code': 'DOCUMENT_NOT_FOUND',
            'message': '文档不存在或已删除',
            'retryable': false,
          },
        })),
        404,
        headers: {'content-type': 'application/json; charset=utf-8'},
      ),
    );

    final controller = DocumentDetailController(
      knowledgeClient: client,
      documentId: 'd-1',
    );

    expect(await controller.deleteDocument(), isTrue);
    expect(controller.error, isNull);
    controller.dispose();
  });

  test('存在处理中任务时每 5 秒自动轮询，全部终态后停止', () {
    fakeAsync((async) {
      SharedPreferences.setMockInitialValues({'ui.last_kb_id': 'kb-1'});
      var documentsCalls = 0;
      final client = clientWith((request) async {
        final path = request.url.path;
        if (path == '/api/v1/knowledge-bases') {
          return envelope({
            'items': [
              {
                'id': 'kb-1',
                'name': '轮询库',
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
          documentsCalls += 1;
          final taskState = documentsCalls == 1 ? 'queued' : 'succeeded';
          return envelope({
            'items': [
              {
                'id': 'd-1',
                'knowledge_base_id': 'kb-1',
                'display_name': '解析中.pdf',
                'status': 'ready',
                'deleted_at': null,
                'created_at': '2026-09-20T00:00:00+00:00',
                'updated_at': '2026-09-20T00:00:00+00:00',
                'active_version': null,
                'chunk_count': 13,
                'latest_task': {
                  'id': 't-1',
                  'task_type': 'import',
                  'state': taskState,
                  'stage': null,
                  'progress': 0,
                  'queue_position': null,
                  'cancellable': false,
                  'retryable': false,
                  'retry_count': 0,
                  'max_retries': 3,
                  'knowledge_base_id': 'kb-1',
                  'document_id': 'd-1',
                  'document_version_id': null,
                  'parent_task_id': null,
                  'error': null,
                  'created_at': '2026-09-20T00:00:00+00:00',
                  'started_at': null,
                  'finished_at': null,
                  'document_display_name': null,
                  'document_size_bytes': null,
                  'route_mode': null,
                  'route_reason': null,
                },
              },
            ],
            'next_cursor': null,
          });
        }
        return envelope({'items': [], 'next_cursor': null});
      });

      AppPreferences? preferences;
      AppPreferences.load().then((value) => preferences = value);
      async.elapse(const Duration(milliseconds: 1));

      final controller = KnowledgeBaseController(
        knowledgeClient: client,
        preferences: preferences!,
      );
      controller.loadInitial();
      async.elapse(const Duration(milliseconds: 10));

      // 首次载入存在处理中任务：安排了 5 秒轮询定时器
      expect(
        controller.documents.single.latestTask!.state.wireName,
        'queued',
      );
      expect(async.nonPeriodicTimerCount, 1);

      // 轮询触发后状态更新为终态，轮询自动停止
      async.elapse(const Duration(seconds: 5));
      expect(
        controller.documents.single.latestTask!.state.wireName,
        'succeeded',
      );
      expect(async.nonPeriodicTimerCount, 0);
      expect(documentsCalls, 2);
      controller.dispose();
    });
  });

  test('任务列表携带筛选参数并还原队列位次', () async {
    SharedPreferences.setMockInitialValues({});
    final requestedQueries = <String?>[];
    final client = clientWith((request) async {
      requestedQueries.add(request.url.queryParameters['state']);
      return envelope({
        'items': [
          {
            'id': 't-1',
            'task_type': 'import',
            'state': 'queued',
            'stage': null,
            'progress': 0,
            'queue_position': 3,
            'cancellable': true,
            'retryable': false,
            'retry_count': 0,
            'max_retries': 3,
            'knowledge_base_id': 'kb-1',
            'document_id': 'd-1',
            'document_version_id': null,
            'parent_task_id': null,
            'error': null,
            'created_at': '2026-09-20T00:00:00+00:00',
            'started_at': null,
            'finished_at': null,
            'document_display_name': '解析中.pdf',
            'document_size_bytes': null,
            'route_mode': null,
            'route_reason': null,
          },
        ],
        'next_cursor': null,
      });
    });

    final controller = TaskCenterController(knowledgeClient: client);
    await controller.loadInitial(state: 'queued');

    expect(requestedQueries.single, 'queued');
    final task = controller.tasks.single;
    expect(task.queuePosition, 3);
    expect(task.state.zhLabel, '排队中');
    controller.dispose();
  });

  test('取消任务成功后刷新列表状态', () async {
    SharedPreferences.setMockInitialValues({});
    var calls = 0;
    final client = clientWith((request) async {
      if (request.method == 'POST' &&
          request.url.path == '/api/v1/tasks/t-1/cancel') {
        return envelope({
          'task': {
            'id': 't-1',
            'task_type': 'import',
            'state': 'cancel_requested',
            'stage': 'parsing_local',
            'progress': 0.2,
            'queue_position': null,
            'cancellable': true,
            'retryable': false,
            'retry_count': 0,
            'max_retries': 3,
            'knowledge_base_id': 'kb-1',
            'document_id': 'd-1',
            'document_version_id': null,
            'parent_task_id': null,
            'error': null,
            'created_at': '2026-09-20T00:00:00+00:00',
            'started_at': null,
            'finished_at': null,
            'document_display_name': null,
            'document_size_bytes': null,
            'route_mode': null,
            'route_reason': null,
          },
        });
      }
      calls += 1;
      final state = calls == 1 ? 'queued' : 'cancel_requested';
      return envelope({
        'items': [
          {
            'id': 't-1',
            'task_type': 'import',
            'state': state,
            'stage': null,
            'progress': 0,
            'queue_position': null,
            'cancellable': true,
            'retryable': false,
            'retry_count': 0,
            'max_retries': 3,
            'knowledge_base_id': 'kb-1',
            'document_id': 'd-1',
            'document_version_id': null,
            'parent_task_id': null,
            'error': null,
            'created_at': '2026-09-20T00:00:00+00:00',
            'started_at': null,
            'finished_at': null,
            'document_display_name': null,
            'document_size_bytes': null,
            'route_mode': null,
            'route_reason': null,
          },
        ],
        'next_cursor': null,
      });
    });

    final controller = TaskCenterController(knowledgeClient: client);
    await controller.loadInitial();
    expect(controller.tasks.single.state, TaskWireStatus.queued);

    final ok = await controller.cancelTask('t-1');
    expect(ok, isTrue);
    expect(controller.tasks.single.state, TaskWireStatus.cancelRequested);
    controller.dispose();
  });

  test('任务中心存在非终态任务时自动轮询，全部终态后停止', () {
    fakeAsync((async) {
      SharedPreferences.setMockInitialValues({});
      var listCalls = 0;
      final client = clientWith((request) async {
        if (request.url.path == '/api/v1/tasks') {
          listCalls += 1;
          final state = listCalls == 1 ? 'queued' : 'succeeded';
          return envelope({
            'items': [
              {
                'id': 't-1',
                'task_type': 'import',
                'state': state,
                'stage': null,
                'progress': 0,
                'queue_position': null,
                'cancellable': false,
                'retryable': false,
                'retry_count': 0,
                'max_retries': 3,
                'knowledge_base_id': null,
                'document_id': null,
                'document_version_id': null,
                'parent_task_id': null,
                'error': null,
                'created_at': '2026-09-20T00:00:00+00:00',
                'started_at': null,
                'finished_at': null,
                'document_display_name': null,
                'document_size_bytes': null,
                'route_mode': null,
                'route_reason': null,
              },
            ],
            'next_cursor': null,
          });
        }
        return envelope({'items': [], 'next_cursor': null});
      });

      final controller = TaskCenterController(knowledgeClient: client);
      controller.loadInitial();
      async.elapse(const Duration(milliseconds: 10));
      expect(controller.tasks.single.state, TaskWireStatus.queued);
      expect(async.nonPeriodicTimerCount, 1);

      async.elapse(const Duration(seconds: 5));
      expect(controller.tasks.single.state, TaskWireStatus.succeeded);
      expect(async.nonPeriodicTimerCount, 0);
      expect(listCalls, 2);
      controller.dispose();
    });
  });
}
