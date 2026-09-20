// 问答控制器测试：发送/流式消费/遥测采样/取消/410/会话管理
//
// SSE 帧以真实字节流脚本注入（状态机行为已由会话测试覆盖，此处
// 聚焦控制器的状态编排与 API 编排），客户端为脚本化替身。
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/api/api_error.dart';
import 'package:desktop_document_ai/api/conversation_api_client.dart';
import 'package:desktop_document_ai/api/dto/conversation_dto.dart';
import 'package:desktop_document_ai/api/dto/knowledge_dto.dart';
import 'package:desktop_document_ai/api/dto/query_dto.dart';
import 'package:desktop_document_ai/api/knowledge_api_client.dart';
import 'package:desktop_document_ai/api/query_api_client.dart';
import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/controllers/chat_controller.dart';

// ===================== 脚本化客户端替身 =====================

class FakeQueryApiClient implements QueryApiClient {
  FakeQueryApiClient();

  QueryCreated? created;
  ApiException? createError;
  String? lastCreateConversationId;
  List<String> frames = const [];
  List<Future<List<int>>> Function()? chunks;
  QueryRunStatus? cancelResult;
  int cancelCalls = 0;
  final metrics = <Map<String, dynamic>>[];

  @override
  String? get instanceId => 'test-instance';

  @override
  Future<QueryCreated> createQuery({
    required String knowledgeBaseId,
    required String question,
    String? conversationId,
    String? idempotencyKey,
  }) async {
    lastCreateConversationId = conversationId;
    final error = createError;
    if (error != null) throw error;
    return created!;
  }

  @override
  Future<QueryAggregate> getAggregate(String queryId) async {
    throw UnimplementedError();
  }

  @override
  Future<QueryRunStatus> cancelQuery(String queryId) async {
    cancelCalls += 1;
    return cancelResult ?? QueryRunStatus.cancelRequested;
  }

  @override
  Future<void> reportClientMetrics({
    required String queryId,
    required DateTime clientSendAt,
    required DateTime firstSseTokenReceivedAt,
    required DateTime firstTokenRenderedAt,
    Map<String, String>? networkContext,
  }) async {
    metrics.add({
      'query_id': queryId,
      'send_at': clientSendAt,
      'received_at': firstSseTokenReceivedAt,
      'rendered_at': firstTokenRenderedAt,
    });
  }

  @override
  Future<(http.StreamedResponse, void Function())> openEventStream(
    String queryId,
    int lastEventId,
  ) async {
    // 分块到达：默认把全部帧放一个块；竞态敏感的用例注入带延时的分块
    final body = chunks != null
        ? chunks!()
        : [Future<List<int>>.value(utf8.encode(frames.join()))];
    final response = http.StreamedResponse(
      Stream.fromFutures(body),
      200,
      headers: {'content-type': 'text/event-stream'},
    );
    return (response, () {});
  }
}

class FakeConversationApiClient implements ConversationApiClient {
  FakeConversationApiClient();

  List<ConversationSummaryDto> conversations = const [];
  Map<String, List<ConversationMessageDto>> messagesByConversation = {};
  ApiException? listError;
  ApiException? messagesError;
  final deletedIds = <String>[];

  @override
  Future<ConversationPage> listConversations({
    required String kbId,
    String? cursor,
    int? limit,
  }) async {
    final error = listError;
    if (error != null) throw error;
    return ApiPage(items: conversations, nextCursor: null);
  }

  @override
  Future<ConversationMessages> listMessages(String conversationId) async {
    final error = messagesError;
    if (error != null) throw error;
    final items = messagesByConversation[conversationId];
    if (items == null) {
      throw ApiException(
        code: 'CONVERSATION_NOT_FOUND',
        message: '会话不存在',
        statusCode: 404,
      );
    }
    return ConversationMessages(conversationId: conversationId, items: items);
  }

  @override
  Future<void> deleteConversation(String conversationId) async {
    deletedIds.add(conversationId);
  }
}

class FakeKnowledgeApiClient extends KnowledgeApiClient {
  FakeKnowledgeApiClient();

  KbDto? kb;
  ApiException? error;
  int calls = 0;

  @override
  Future<KbDto> getKnowledgeBase(String kbId) async {
    calls += 1;
    final error = this.error;
    if (error != null) throw error;
    return kb!;
  }
}

// ===================== 构造辅助 =====================

const _kbId = 'kb-1';
const _conversationId = 'conv-1';

KbDto _kb({bool deleted = false}) => KbDto(
      id: _kbId,
      name: '测试知识库',
      description: null,
      status: deleted ? 'deleted' : 'active',
      deletedAt: deleted ? '2026-09-21T00:00:00Z' : null,
      createdAt: '2026-09-21T00:00:00Z',
      updatedAt: '2026-09-21T00:00:00Z',
    );

ConversationSummaryDto _summary(String id) => ConversationSummaryDto(
      id: id,
      knowledgeBaseId: _kbId,
      title: null,
      createdAt: '2026-09-21T00:00:00Z',
      updatedAt: '2026-09-21T00:00:00Z',
      lastMessage: null,
    );

ConversationMessageDto _message(String role, String content,
        {List<CitationSnapshot> citations = const []}) =>
    ConversationMessageDto(
      id: 'msg-$role-$content',
      role: role,
      content: content,
      createdAt: '2026-09-21T00:00:00Z',
      citations: citations,
    );

String _sseFrame(String event, Map<String, dynamic> data, int id) =>
    'id: $id\nevent: $event\ndata: ${jsonEncode(data)}\n\n';

/// 等待谓词成立（真实异步轮转，控制器回调在微任务/定时器后到达）
Future<void> _waitFor(bool Function() predicate, {int timeoutMs = 2000}) async {
  final deadline = DateTime.now().add(Duration(milliseconds: timeoutMs));
  while (!predicate()) {
    if (DateTime.now().isAfter(deadline)) {
      fail('等待条件超时');
    }
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

class Harness {
  Harness()
    : query = FakeQueryApiClient(),
      conversations = FakeConversationApiClient(),
      knowledge = FakeKnowledgeApiClient() {
    query.created = QueryCreated(
      queryId: 'query-1',
      conversationId: _conversationId,
      streamUrl: '/api/v1/queries/query-1/events',
      state: QueryRunStatus.queued,
    );
    knowledge.kb = _kb();
  }

  final FakeQueryApiClient query;
  final FakeConversationApiClient conversations;
  final FakeKnowledgeApiClient knowledge;
  AppPreferences? preferences;
  ChatController? controller;

  Future<ChatController> build({
    Map<String, Object> prefs = const {},
    String? initialKbId,
    String? initialConversationId,
  }) async {
    SharedPreferences.setMockInitialValues(prefs);
    preferences = await AppPreferences.load();
    controller = ChatController(
      queryClient: query,
      conversationClient: conversations,
      knowledgeClient: knowledge,
      preferences: preferences!,
    );
    await controller!.loadInitial(
      kbId: initialKbId ?? _kbId,
      conversationId: initialConversationId,
    );
    return controller!;
  }
}

void main() {
  test('发送后流式渲染 token 并在 done 收尾，草稿会话绑定服务端会话', () async {
    final harness = Harness();
    harness.query.frames = [
      _sseFrame('tokens', {'from': 1, 'to': 1, 'text': '你好'}, 1),
      _sseFrame('done', {}, 2),
    ];
    final controller = await harness.build();

    await controller.send('什么是年假？');
    await _waitFor(() => !controller.generating);

    expect(controller.currentConversationId, _conversationId);
    expect(controller.messages[0].role, 'user');
    expect(controller.messages[0].content, '什么是年假？');
    expect(controller.messages[1].content, '你好');
    expect(controller.messages[1].streaming, isFalse);
    expect(controller.messages[1].errorMessage, isNull);
    expect(harness.preferences!.lastConversationId, _conversationId);
  });

  test('首 token 时间戳齐全时终态后上报客户端遥测且顺序合法', () async {
    final harness = Harness();
    harness.query.chunks = () => [
          Future<List<int>>.delayed(
            const Duration(milliseconds: 30),
            () => utf8.encode(
              _sseFrame('tokens', {'from': 1, 'to': 1, 'text': '你'}, 1),
            ),
          ),
          Future<List<int>>.delayed(
            const Duration(milliseconds: 90),
            () => utf8.encode(_sseFrame('done', {}, 2)),
          ),
        ];
    final controller = await harness.build();

    await controller.send('问题');
    await _waitFor(
      () => controller.messages.length > 1 && controller.messages[1].content.isNotEmpty,
    );
    controller.markFirstTokenRendered();
    await _waitFor(() => !controller.generating);

    expect(harness.query.metrics, hasLength(1));
    final metric = harness.query.metrics.single;
    expect(metric['query_id'], 'query-1');
    expect(
      (metric['send_at'] as DateTime).isBefore(metric['received_at'] as DateTime),
      isTrue,
    );
    expect(
      (metric['received_at'] as DateTime)
          .isBefore(metric['rendered_at'] as DateTime),
      isTrue,
    );
  });

  test('无 token 的拒答终态不满足时间戳合同，跳过遥测上报', () async {
    final harness = Harness();
    harness.query.frames = [_sseFrame('done', {'refused': true}, 1)];
    final controller = await harness.build();

    await controller.send('问题');
    await _waitFor(() => !controller.generating);

    expect(controller.messages[1].refused, isTrue);
    expect(harness.query.metrics, isEmpty);
  });

  test('citation 事件按编号累积到当前回答', () async {
    final harness = Harness();
    harness.query.frames = [
      _sseFrame(
        'citation',
        {
          'citation_order': 2,
          'chunk_id': 'chunk-2',
          'file_name': 'b.pdf',
          'version_no': 1,
          'page_no': 2,
          'section_path': '第二章',
          'content': '内容二',
          'validation_state': 'validated',
        },
        1,
      ),
      _sseFrame(
        'citation',
        {
          'citation_order': 1,
          'chunk_id': 'chunk-1',
          'file_name': 'a.pdf',
          'version_no': 1,
          'page_no': 1,
          'section_path': '第一章',
          'content': '内容一',
          'validation_state': 'validated',
        },
        2,
      ),
      _sseFrame('done', {}, 3),
    ];
    final controller = await harness.build();

    await controller.send('问题');
    await _waitFor(() => !controller.generating);

    final citations = controller.messages[1].citations;
    expect(citations, hasLength(2));
    expect(citations[0].citationOrder, 1);
    expect(citations[0].fileName, 'a.pdf');
    expect(citations[1].citationOrder, 2);
  });

  test('显式取消后 cancelled 终态收尾并标记已取消', () async {
    final harness = Harness();
    final tokenCompleter = Completer<List<int>>();
    final cancelledCompleter = Completer<List<int>>();
    harness.query.chunks = () => [tokenCompleter.future, cancelledCompleter.future];
    final controller = await harness.build();

    await controller.send('问题');
    tokenCompleter.complete(
      utf8.encode(_sseFrame('tokens', {'from': 1, 'to': 1, 'text': '部分'}, 1)),
    );
    await _waitFor(() => controller.messages[1].content == '部分');

    await controller.cancel();
    expect(harness.query.cancelCalls, 1);
    // 不提前置终态：SSE cancelled 到达前仍处于生成态
    expect(controller.generating, isTrue);

    cancelledCompleter.complete(utf8.encode(_sseFrame('cancelled', {}, 2)));
    await _waitFor(() => !controller.generating);
    expect(controller.messages[1].cancelled, isTrue);
    expect(controller.messages[1].streaming, isFalse);
  });

  test('创建查询返回 410 时进入只读且不保留流式占位', () async {
    final harness = Harness();
    harness.query.createError = ApiException(
      code: 'KNOWLEDGE_BASE_DELETED',
      message: '知识库已删除',
      statusCode: 410,
    );
    final controller = await harness.build();

    await controller.send('问题');
    await _waitFor(() => !controller.generating);

    expect(controller.kbDeleted, isTrue);
    expect(controller.messages, hasLength(1));
    expect(controller.messages[0].role, 'user');
  });

  test('切换会话加载历史消息并更新缓存', () async {
    final harness = Harness();
    harness.conversations.conversations = [_summary('conv-a'), _summary('conv-b')];
    harness.conversations.messagesByConversation = {
      'conv-b': [_message('user', 'B 的问题'), _message('assistant', 'B 的回答')],
    };
    final controller = await harness.build();

    await controller.selectConversation('conv-b');
    await _waitFor(() => controller.currentConversationId == 'conv-b');

    expect(controller.messages.map((m) => m.content), ['B 的问题', 'B 的回答']);
    expect(harness.preferences!.lastConversationId, 'conv-b');
  });

  test('历史助手消息携带引用快照，可按编号定位', () async {
    final harness = Harness();
    harness.conversations.messagesByConversation = {
      'conv-c': [
        _message('user', '历史问题'),
        _message(
          'assistant',
          '根据[S1]回答。',
          citations: [
            CitationSnapshot(
              citationOrder: 1,
              chunkId: null,
              fileName: '手册.pdf',
              versionNo: 2,
              pageNo: 3,
              sectionPath: '休假/年假',
              content: '引用正文快照',
              validationState: 'validated',
            ),
          ],
        ),
      ],
    };
    final controller = await harness.build(initialConversationId: 'conv-c');

    expect(controller.currentConversationId, 'conv-c');
    expect(controller.messages[1].citations, hasLength(1));
    expect(controller.messages[1].citations.single.citationOrder, 1);
    expect(controller.messages[1].citations.single.chunkId, isNull);
  });

  test('切换到已删除会话回退到列表最新会话', () async {
    final harness = Harness();
    harness.conversations.conversations = [_summary('conv-live')];
    harness.conversations.messagesByConversation = {
      'conv-live': [_message('user', '还在的问题')],
    };
    final controller = await harness.build(initialConversationId: 'conv-gone');

    expect(controller.currentConversationId, 'conv-live');
    expect(controller.messages.map((m) => m.content), ['还在的问题']);
    expect(harness.preferences!.lastConversationId, 'conv-live');
  });

  test('删除当前会话后回退到列表下一会话', () async {
    final harness = Harness();
    harness.conversations.conversations = [_summary('conv-a'), _summary('conv-b')];
    harness.conversations.messagesByConversation = {
      'conv-a': [_message('user', 'A')],
      'conv-b': [_message('user', 'B')],
    };
    final controller = await harness.build(initialConversationId: 'conv-a');

    final deleted = await controller.deleteConversation('conv-a');

    expect(deleted, isTrue);
    expect(harness.conversations.deletedIds, ['conv-a']);
    expect(controller.currentConversationId, 'conv-b');
    expect(controller.conversations.map((c) => c.id), ['conv-b']);
  });

  test('知识库不存在时清理缓存并进入空态', () async {
    final harness = Harness();
    harness.knowledge.error = ApiException(
      code: 'KNOWLEDGE_BASE_NOT_FOUND',
      message: '知识库不存在',
      statusCode: 404,
    );

    final controller = await harness.build(
      prefs: {'ui.last_kb_id': _kbId, 'ui.last_conversation_id': _conversationId},
    );

    expect(controller.knowledgeBase, isNull);
    expect(harness.preferences!.lastKnowledgeBaseId, isNull);
    expect(harness.preferences!.lastConversationId, isNull);
  });

  test('新建会话进入草稿态，发送时不携带会话 ID', () async {
    final harness = Harness();
    harness.conversations.conversations = [_summary(_conversationId)];
    harness.conversations.messagesByConversation = {
      _conversationId: [_message('user', '历史')],
    };
    harness.query.created = QueryCreated(
      queryId: 'query-2',
      conversationId: 'conv-new',
      streamUrl: '/api/v1/queries/query-2/events',
      state: QueryRunStatus.queued,
    );
    harness.query.frames = [_sseFrame('done', {}, 1)];
    final controller = await harness.build(initialConversationId: _conversationId);

    controller.newConversation();
    expect(controller.currentConversationId, isNull);

    await controller.send('新问题');
    await _waitFor(() => !controller.generating);

    // 草稿发送不携带会话 ID，202 回带后绑定新建会话
    expect(harness.query.lastCreateConversationId, isNull);
    expect(controller.currentConversationId, 'conv-new');
  });
}
