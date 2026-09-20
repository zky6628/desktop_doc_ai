// 查询流会话测试：正常序列、终态事件映射、断线重连去重、退避节奏、
// 重连耗尽后的聚合恢复与非重试错误的立即失败。
//
// 重连等待与聚合轮询使用 fake_async 推进虚拟时间。
import 'dart:async';
import 'dart:convert';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:desktop_document_ai/api/api_error.dart';
import 'package:desktop_document_ai/api/dto/query_dto.dart';
import 'package:desktop_document_ai/api/sse/query_stream_session.dart';

/// 把若干完整 SSE 帧封装为一个 200 事件流响应（帧文本一次性送达）
(http.StreamedResponse, void Function()) okStream(List<String> frames) {
  final body = utf8.encode(frames.join());
  return (
    http.StreamedResponse(
      Stream.fromIterable([body]),
      200,
      headers: {'content-type': 'text/event-stream'},
    ),
    () {},
  );
}

/// 便捷事件帧
String frame(int id, String event, Map<String, dynamic> data) =>
    'id: $id\nevent: $event\ndata: ${jsonEncode(data)}\n\n';

/// 连接工厂脚本：按调用次序返回预设响应，耗尽后抛网络错误；
/// 记录每次调用收到的 Last-Event-ID
class ScriptedConnect {
  ScriptedConnect(this.responses, {this.exhaustError});

  final List<(http.StreamedResponse, void Function())> responses;
  final ApiException? exhaustError;
  final List<int> lastEventIds = [];

  int get calls => lastEventIds.length;

  Future<(http.StreamedResponse, void Function())> call(int lastEventId) async {
    lastEventIds.add(lastEventId);
    if (lastEventIds.length <= responses.length) {
      return responses[lastEventIds.length - 1];
    }
    throw exhaustError ?? ApiException.network('脚本耗尽');
  }
}

void main() {
  test('正常事件序列产出阶段/增量/引用并到完成终态', () async {
    final connect = ScriptedConnect([
      okStream([
        frame(1, 'meta', {'query_id': 'q-1', 'knowledge_base_id': 'kb-1'}),
        frame(2, 'stage', {'name': 'retrieval_started'}),
        frame(3, 'tokens', {'from': 1, 'to': 2, 'text': '员工'}),
        frame(4, 'tokens', {'from': 3, 'to': 4, 'text': '满一年'}),
        frame(
          5,
          'citation',
          {
            'citation_order': 1,
            'chunk_id': 'ch-1',
            'file_name': '员工手册.pdf',
            'version_no': 2,
            'page_no': 12,
            'section_path': '休假/年假',
            'content': '累计工作满一年...',
            'validation_state': 'valid',
          },
        ),
        frame(6, 'done', {}),
      ]),
    ]);
    final session = QueryStreamSession(
      queryId: 'q-1',
      connect: connect.call,
      recoverAggregate: () => throw UnimplementedError(),
    );

    final updates = await session.start().toList();

    expect(connect.lastEventIds, [0]);
    final text = updates.map((u) => u.appendedText).join();
    expect(text, '员工满一年');
    expect(session.accumulatedText, '员工满一年');
    expect(updates.any((u) => u.stage?.name == 'retrieval_started'), isTrue);
    expect(updates.singleWhere((u) => u.citation != null).citation!.fileName,
        '员工手册.pdf');
    final terminal = updates.last.terminal!;
    expect(terminal.phase, QueryStreamPhase.completed);
    expect(terminal.refused, isFalse);
  });

  test('done 携带 refused 标记时完成终态保留拒答信息', () async {
    final connect = ScriptedConnect([okStream([frame(1, 'done', {'refused': true})])]);
    final session = QueryStreamSession(
      queryId: 'q-1',
      connect: connect.call,
      recoverAggregate: () => throw UnimplementedError(),
    );

    final updates = await session.start().toList();

    expect(updates.last.terminal!.refused, isTrue);
  });

  test('error 事件转失败终态并携带错误码', () async {
    final connect = ScriptedConnect([okStream([frame(1, 'error', {'code': 'GENERATION_ERROR'})])]);
    final session = QueryStreamSession(
      queryId: 'q-1',
      connect: connect.call,
      recoverAggregate: () => throw UnimplementedError(),
    );

    final updates = await session.start().toList();

    final terminal = updates.last.terminal!;
    expect(terminal.phase, QueryStreamPhase.failed);
    expect(terminal.error?.code, 'GENERATION_ERROR');
  });

  test('cancelled 事件转取消终态', () async {
    final connect = ScriptedConnect([okStream([frame(1, 'cancelled', {})])]);
    final session = QueryStreamSession(
      queryId: 'q-1',
      connect: connect.call,
      recoverAggregate: () => throw UnimplementedError(),
    );

    final updates = await session.start().toList();

    expect(updates.last.terminal!.phase, QueryStreamPhase.cancelled);
  });

  test('流未到终态即断开：立即重连并按序号去重重放事件', () async {
    final connect = ScriptedConnect([
      // 第一次连接：token 后连接中断（无终态事件，流自然结束）
      okStream([frame(1, 'tokens', {'from': 1, 'to': 2, 'text': '你'})]),
      // 重连：服务端按 Last-Event-ID 重放已发事件，随后补齐增量与终态
      okStream([
        frame(1, 'tokens', {'from': 1, 'to': 2, 'text': '你'}),
        frame(2, 'tokens', {'from': 3, 'to': 4, 'text': '好'}),
        frame(3, 'done', {}),
      ]),
    ]);
    final session = QueryStreamSession(
      queryId: 'q-1',
      connect: connect.call,
      recoverAggregate: () => throw UnimplementedError(),
    );

    final updates = await session.start().toList();

    expect(connect.lastEventIds, [0, 1]);
    expect(session.accumulatedText, '你好');
    expect(updates.last.terminal!.phase, QueryStreamPhase.completed);
  });

  test('重复断线按退避节奏重连，耗尽后经聚合轮询恢复终态', () {
    fakeAsync((async) {
      final connect = ScriptedConnect(
        List.generate(6, (_) => okStream([])),
        exhaustError: ApiException.network('连接失败'),
      );
      final aggregateCalls = <int>[];
      final session = QueryStreamSession(
        queryId: 'q-1',
        connect: connect.call,
        recoverAggregate: () {
          aggregateCalls.add(aggregateCalls.length);
          return Future.value(
            QueryAggregate(
              queryId: 'q-1',
              state: QueryRunStatus.completed,
              refused: false,
              rerankDegraded: false,
              serverTtftMs: 100,
              totalMs: 500,
              error: null,
              answer: '聚合恢复正文',
              citations: const [],
            ),
          );
        },
      );

      final updates = <QueryStreamUpdate>[];
      var done = false;
      session.start().listen(updates.add, onDone: () => done = true);

      // 首次断开立即重连，随后 1/2/4/8 秒；第 5 次重连失败后进入恢复
      async.elapse(const Duration(seconds: 16));

      expect(connect.calls, 6);
      expect(aggregateCalls, hasLength(1));
      expect(done, isTrue);
      final terminal = updates.last.terminal!;
      expect(terminal.phase, QueryStreamPhase.completed);
      expect(terminal.recoveredAggregate?.answer, '聚合恢复正文');
    });
  });

  test('连接返回不可重试的业务错误时立即失败且不重连', () async {
    final connect = ScriptedConnect([
      (
        http.StreamedResponse(
          Stream.fromIterable([
            utf8.encode(envelope404()),
          ]),
          404,
        ),
        () {},
      ),
    ]);
    final session = QueryStreamSession(
      queryId: 'q-1',
      connect: connect.call,
      recoverAggregate: () => throw UnimplementedError(),
    );

    final updates = await session.start().toList();

    expect(connect.calls, 1);
    final terminal = updates.last.terminal!;
    expect(terminal.phase, QueryStreamPhase.failed);
    expect(terminal.error?.code, 'QUERY_NOT_FOUND');
  });
}

/// 404 场景的信封错误体
String envelope404() => jsonEncode({
      'success': false,
      'request_id': 'req-1',
      'data': null,
      'error': {
        'code': 'QUERY_NOT_FOUND',
        'message': '查询不存在',
        'retryable': false,
      },
    });
