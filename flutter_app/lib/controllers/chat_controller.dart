import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/api_error.dart';
import '../api/conversation_api_client.dart';
import '../api/dto/conversation_dto.dart';
import '../api/dto/knowledge_dto.dart';
import '../api/dto/query_dto.dart';
import '../api/knowledge_api_client.dart';
import '../api/query_api_client.dart';
import '../api/sse/query_stream_session.dart';
import '../app/app_preferences.dart';
import '../utils/request_guard.dart';

/// 问答消息视图：会话历史与流式生成共用的渲染事实
class ChatMessageView {
  ChatMessageView({
    required this.id,
    required this.role,
    required this.content,
    this.citations = const [],
    this.streaming = false,
    this.refused = false,
    this.degraded = false,
    this.cancelled = false,
    this.errorMessage,
  });

  /// 服务端消息 ID（历史/落库后）或本地占位 ID（生成中）
  final String id;
  final String role;
  String content;
  List<CitationSnapshot> citations;
  bool streaming;
  bool refused;
  bool degraded;
  bool cancelled;
  String? errorMessage;
}

/// 问答控制器：会话列表（服务端）、历史消息、流式问答与引用累积
///
/// 只调 API Client（UI 规范 §13）；会话列表与消息加载各自独立防过期
/// 序号，发送通道单飞（生成中禁发）。SSE 连接状态机由
/// [QueryStreamSession] 承担，本控制器消费其更新流并采样客户端遥测
/// 时间戳；410 知识库已删除按只读表达，不进入网络错误重试。
class ChatController extends ChangeNotifier {
  ChatController({
    required QueryApiClient queryClient,
    required ConversationApiClient conversationClient,
    required KnowledgeApiClient knowledgeClient,
    required this._preferences,
    this.onKnowledgeBaseResolved,
  }) : _query = queryClient,
       _conversations = conversationClient,
       _knowledge = knowledgeClient;

  /// 知识库解析完成回调（页面桥接到 AppShellController 实现跨页同步）
  final void Function(String? kbId, String? name)? onKnowledgeBaseResolved;

  final QueryApiClient _query;
  final ConversationApiClient _conversations;
  final KnowledgeApiClient _knowledge;
  final AppPreferences _preferences;

  final _loadGuard = LatestRequestGuard();
  final _listGuard = LatestRequestGuard();
  final _messagesGuard = LatestRequestGuard();

  // ===================== 知识库上下文 =====================

  /// 当前知识库（null 表示未选择或校验失败后的空态）
  KbDto? knowledgeBase;

  /// 当前知识库是否已删除（只读横幅；禁输入/新建/重发）
  bool kbDeleted = false;

  bool kbLoading = false;

  // ===================== 会话列表 =====================

  List<ConversationSummaryDto> conversations = [];
  bool conversationsLoading = false;
  ApiException? listError;
  String? _listCursor;
  bool hasMoreConversations = false;

  // ===================== 当前会话与消息 =====================

  /// 当前会话 ID（null 表示草稿：首次发送时由服务端创建）
  String? currentConversationId;
  List<ChatMessageView> messages = [];
  bool messagesLoading = false;
  ApiException? messagesError;

  // ===================== 生成状态 =====================

  /// 是否有查询在生成（含建查/连接/接收/重连全过程）
  bool generating = false;

  /// 生成状态文案（建查/连接/阶段/重连）
  String? statusText;

  /// 当前活跃查询 ID（取消用）
  String? _activeQueryId;
  StreamSubscription<QueryStreamUpdate>? _streamSubscription;

  // 客户端遥测时间戳采样
  DateTime? _clientSendAt;
  DateTime? _firstSseTokenReceivedAt;
  DateTime? _firstTokenRenderedAt;

  /// 最近一次生成的客户端遥测快照（评测模式读取）：
  /// (queryId, 发送时刻, SSE 首 token 时刻, 首帧渲染时刻)；
  /// 三时间戳齐全才有值，缺一为 null
  (String, DateTime, DateTime, DateTime)? lastTelemetry;

  ApiException? actionError;

  /// 生成期间知识库页切库的待补载 KB（生成结束自动重载）
  String? _pendingKbId;

  // ===================== 初始化与恢复 =====================

  /// 初始化：路由 query 参数优先，其次最近 KB+会话，失效即清理缓存
  ///
  /// 可重入（IndexedStack 分支保活，知识库页切库后经广播重新进入）：
  /// 旧序列在 await 后被新序列取代时静默放弃；生成中先记下待切换
  /// KB，生成结束自动补载，避免切库请求丢失
  Future<void> loadInitial({String? kbId, String? conversationId}) async {
    if (generating) {
      _pendingKbId = kbId;
      return;
    }
    final seq = _loadGuard.begin();
    kbId ??= _preferences.lastKnowledgeBaseId;
    if (kbId == null) {
      onKnowledgeBaseResolved?.call(null, null);
      return;
    }
    kbLoading = true;
    notifyListeners();
    final (kb, error, notFound) = await _resolveKnowledgeBase(kbId);
    // 旧序列在任何 await 后被新序列取代时静默放弃，不写任何共享状态
    if (!_loadGuard.isLatest(seq)) return;
    kbLoading = false;
    if (kb != null) {
      knowledgeBase = kb;
      kbDeleted = kb.deletedAt != null;
      unawaited(_preferences.saveLastKnowledgeBaseId(kb.id));
      onKnowledgeBaseResolved?.call(kb.id, kb.name);
    } else if (notFound) {
      // 失效 KB：清理缓存回退空态（UI 规范 §2）
      knowledgeBase = null;
      kbDeleted = false;
      unawaited(_preferences.saveLastKnowledgeBaseId(null));
      unawaited(_preferences.saveLastConversationId(null));
      onKnowledgeBaseResolved?.call(null, null);
    } else {
      actionError = error;
    }
    notifyListeners();
    // 会话列表为跨库全量历史（与知识库解耦）：空态下也加载，
    // 已删除知识库的会话仍可查看
    await reloadConversations();
    if (kb == null) return;
    if (!_loadGuard.isLatest(seq)) return;
    await _restoreConversation(conversationId);
  }

  /// 知识库详情取数（纯查询，不写控制器状态）：成功/404/其他异常三态
  ///
  /// 共享状态（knowledgeBase/缓存/回调）由调用方在防过期检查通过后
  /// 统一落盘，避免慢响应覆盖并发切库的最新状态
  Future<(KbDto?, ApiException?, bool)> _resolveKnowledgeBase(
    String kbId,
  ) async {
    try {
      return (await _knowledge.getKnowledgeBase(kbId), null, false);
    } on ApiException catch (exc) {
      return (null, exc, exc.statusCode == 404);
    }
  }

  Future<void> _restoreConversation(String? conversationId) async {
    // 最近会话缓存全局持久化：仅当缓存在会话列表中可见时才恢复
    // （列表跨库全量，已删除知识库的会话同样可恢复），否则回退列表
    // 最新或草稿
    final remembered = _preferences.lastConversationId;
    final rememberedInKb =
        remembered != null && conversations.any((item) => item.id == remembered)
        ? remembered
        : null;
    final target =
        conversationId ??
        rememberedInKb ??
        (conversations.isNotEmpty ? conversations.first.id : null);
    if (target == null) {
      _resetToDraft();
      return;
    }
    try {
      final seq = _messagesGuard.begin();
      final loaded = await _conversations.listMessages(target);
      if (!_messagesGuard.isLatest(seq)) return;
      _adoptConversation(target, loaded.items);
    } on ApiException catch (exc) {
      if (exc.statusCode == 404) {
        // 失效会话：清理缓存并回退到列表最新会话或草稿
        unawaited(_preferences.saveLastConversationId(null));
        final fallback = conversations.isNotEmpty ? conversations.first.id : null;
        if (fallback == null) {
          _resetToDraft();
          notifyListeners();
          return;
        }
        await selectConversation(fallback);
        return;
      }
      actionError = exc;
      notifyListeners();
    }
  }

  // ===================== 会话列表操作 =====================

  /// 会话列表（跨库全量，与当前知识库上下文解耦）
  Future<void> reloadConversations() async {
    conversationsLoading = true;
    notifyListeners();
    try {
      final seq = _listGuard.begin();
      final page = await _conversations.listConversations();
      if (!_listGuard.isLatest(seq)) return;
      conversations = page.items;
      _listCursor = page.nextCursor;
      hasMoreConversations = page.hasMore;
      listError = null;
    } on ApiException catch (exc) {
      listError = exc;
    } finally {
      conversationsLoading = false;
      notifyListeners();
    }
  }

  /// 加载更多（keyset 续页）
  Future<void> loadMoreConversations() async {
    if (!hasMoreConversations || conversationsLoading) return;
    conversationsLoading = true;
    notifyListeners();
    try {
      final seq = _listGuard.begin();
      final page = await _conversations.listConversations(cursor: _listCursor);
      if (!_listGuard.isLatest(seq)) return;
      conversations = [...conversations, ...page.items];
      _listCursor = page.nextCursor;
      hasMoreConversations = page.hasMore;
    } on ApiException catch (exc) {
      listError = exc;
    } finally {
      conversationsLoading = false;
      notifyListeners();
    }
  }

  // ===================== 会话切换与删除 =====================

  /// 切换会话（生成中由页面禁用入口；此处防御性直接返回）
  Future<void> selectConversation(String conversationId) async {
    if (generating || conversationId == currentConversationId) return;
    messagesLoading = true;
    messagesError = null;
    notifyListeners();
    try {
      final seq = _messagesGuard.begin();
      final loaded = await _conversations.listMessages(conversationId);
      if (!_messagesGuard.isLatest(seq)) return;
      _adoptConversation(conversationId, loaded.items);
    } on ApiException catch (exc) {
      if (exc.statusCode == 404) {
        // 会话已被删除：从列表移除并回退
        conversations.removeWhere((item) => item.id == conversationId);
        unawaited(_preferences.saveLastConversationId(null));
        final fallback = conversations.isNotEmpty ? conversations.first.id : null;
        if (fallback == null) {
          _resetToDraft();
        } else {
          messagesLoading = false;
          notifyListeners();
          await selectConversation(fallback);
          return;
        }
      } else {
        messagesError = exc;
      }
    } finally {
      if (messagesLoading) {
        messagesLoading = false;
        notifyListeners();
      }
    }
  }

  /// 新建会话：进入草稿态（会话由首次发送在服务端创建）
  void newConversation() {
    if (generating) return;
    _resetToDraft();
    notifyListeners();
  }

  /// 删除会话（API 接受后才更新本地列表，不伪造成功）
  Future<bool> deleteConversation(String conversationId) async {
    try {
      await _conversations.deleteConversation(conversationId);
      actionError = null;
    } on ApiException catch (exc) {
      actionError = exc;
      notifyListeners();
      return false;
    }
    conversations.removeWhere((item) => item.id == conversationId);
    if (currentConversationId == conversationId) {
      final fallback = conversations.isNotEmpty ? conversations.first.id : null;
      if (fallback == null) {
        _resetToDraft();
        unawaited(_preferences.saveLastConversationId(null));
        notifyListeners();
      } else {
        await selectConversation(fallback);
        return true;
      }
    }
    notifyListeners();
    return true;
  }

  /// 重命名会话（API 接受后才更新本地列表项标题）
  Future<bool> renameConversation(String conversationId, String title) async {
    final trimmed = title.trim();
    if (trimmed.isEmpty) return false;
    try {
      await _conversations.renameConversation(conversationId, trimmed);
      actionError = null;
    } on ApiException catch (exc) {
      actionError = exc;
      notifyListeners();
      return false;
    }
    conversations = [
      for (final item in conversations)
        if (item.id == conversationId)
          ConversationSummaryDto(
            id: item.id,
            knowledgeBaseId: item.knowledgeBaseId,
            title: trimmed,
            createdAt: item.createdAt,
            updatedAt: item.updatedAt,
            lastMessage: item.lastMessage,
          )
        else
          item,
    ];
    notifyListeners();
    return true;
  }

  void _adoptConversation(
    String conversationId,
    List<ConversationMessageDto> items,
  ) {
    currentConversationId = conversationId;
    messages = [
      for (final item in items)
        ChatMessageView(
          id: item.id,
          role: item.role,
          content: item.content,
          citations: item.citations,
        ),
    ];
    messagesLoading = false;
    messagesError = null;
    unawaited(_preferences.saveLastConversationId(conversationId));
    notifyListeners();
  }

  void _resetToDraft() {
    currentConversationId = null;
    messages = [];
    messagesLoading = false;
    messagesError = null;
  }

  // ===================== 问答与流式消费 =====================

  /// 发送问题（生成中禁发；410 转只读不重试）
  Future<void> send(String question) async {
    final kb = knowledgeBase;
    if (kb == null || kbDeleted || generating) return;
    final text = question.trim();
    if (text.isEmpty) return;

    actionError = null;
    _clientSendAt = DateTime.now();
    _firstSseTokenReceivedAt = null;
    _firstTokenRenderedAt = null;
    generating = true;
    statusText = '正在创建查询';
    messages = [
      ...messages,
      ChatMessageView(id: _localId(), role: 'user', content: text),
      ChatMessageView(id: _localId(), role: 'assistant', content: '', streaming: true),
    ];
    notifyListeners();

    try {
      final created = await _query.createQuery(
        knowledgeBaseId: kb.id,
        question: text,
        conversationId: currentConversationId,
        idempotencyKey: const Uuid().v4(),
      );
      if (created.conversationId != null &&
          created.conversationId != currentConversationId) {
        // 草稿会话落地：绑定服务端新建的会话
        currentConversationId = created.conversationId;
        unawaited(_preferences.saveLastConversationId(currentConversationId));
      }
      _activeQueryId = created.queryId;
      _listenStream(
        QueryStreamSession(
          queryId: created.queryId,
          connect:
              (lastEventId) => _query.openEventStream(created.queryId, lastEventId),
          recoverAggregate: () => _query.getAggregate(created.queryId),
        ),
      );
    } on ApiException catch (exc) {
      _finishGenerationWithError(exc);
    }
  }

  void _listenStream(QueryStreamSession session) {
    _streamSubscription?.cancel();
    _streamSubscription = session.start().listen(
      _onStreamUpdate,
      onError: (Object error) {
        _finishGenerationWithError(
          error is ApiException
              ? error
              : ApiException(code: 'UNKNOWN', message: '$error'),
        );
      },
    );
  }

  /// 当前流式占位（生成中的助手消息；无则忽略本次更新）
  ChatMessageView? get _streamingAssistant {
    for (final message in messages.reversed) {
      if (message.role == 'assistant' && message.streaming) return message;
    }
    return null;
  }

  void _onStreamUpdate(QueryStreamUpdate update) {
    if (!generating) return;
    final assistant = _streamingAssistant;
    if (assistant == null) return;
    switch (update.phase) {
      case QueryStreamPhase.connectingStream:
        statusText = '正在连接事件流';
      case QueryStreamPhase.receiving:
        statusText = '检索中';
      case QueryStreamPhase.reconnecting:
        statusText = '连接中断，正在重连';
      case QueryStreamPhase.completed:
      case QueryStreamPhase.failed:
      case QueryStreamPhase.cancelled:
        break;
    }
    if (update.stage != null) {
      _applyStage(assistant, update.stage!);
    }
    if (update.citation != null) {
      _applyCitation(assistant, update.citation!);
    }
    if (update.appendedText.isNotEmpty) {
      _firstSseTokenReceivedAt ??= DateTime.now();
      assistant.content += update.appendedText;
      statusText = '生成中';
    }
    notifyListeners();

    final terminal = update.terminal;
    if (terminal != null) {
      _onTerminal(assistant, terminal);
    }
  }

  void _applyStage(ChatMessageView assistant, QueryStageEvent stage) {
    switch (stage.name) {
      case 'retrieval_started':
        statusText = '检索中';
      case 'retrieval_completed':
        statusText = '检索完成';
      case 'rerank_degraded':
        assistant.degraded = true;
      case 'rerank_completed':
        statusText = '重排完成';
      case 'generation_started':
        statusText = '生成中';
    }
  }

  void _applyCitation(ChatMessageView assistant, CitationSnapshot citation) {
    final order = citation.citationOrder;
    if (order == null) return;
    final existing = assistant.citations
        .indexWhere((item) => item.citationOrder == order);
    if (existing >= 0) {
      final next = [...assistant.citations];
      next[existing] = citation;
      assistant.citations = next;
    } else {
      assistant.citations = [...assistant.citations, citation]
        ..sort(
          (a, b) => (a.citationOrder ?? 0).compareTo(b.citationOrder ?? 0),
        );
    }
  }

  /// 首帧渲染回调（页面在首个 token 渲染完成的帧后调用）
  void markFirstTokenRendered() {
    if (_firstSseTokenReceivedAt == null) return;
    _firstTokenRenderedAt ??= DateTime.now();
  }

  void _onTerminal(ChatMessageView assistant, QueryTerminal terminal) {
    final recovered = terminal.recoveredAggregate;
    if (recovered != null) {
      // 聚合恢复以服务端权威正文与引用为准（去重保留已收文本语义）
      assistant.content = recovered.answer ?? assistant.content;
      assistant.citations = recovered.citations;
      assistant.refused = recovered.refused;
      assistant.degraded = recovered.rerankDegraded;
    }
    switch (terminal.phase) {
      case QueryStreamPhase.completed:
        assistant.refused = assistant.refused || terminal.refused;
      case QueryStreamPhase.failed:
        assistant.errorMessage =
            terminal.error?.message ?? '生成失败，请稍后重试';
      case QueryStreamPhase.cancelled:
        assistant.cancelled = true;
      case QueryStreamPhase.connectingStream:
      case QueryStreamPhase.receiving:
      case QueryStreamPhase.reconnecting:
        break;
    }
    assistant.streaming = false;
    _finishGeneration();
  }

  void _finishGenerationWithError(ApiException exc) {
    if (exc.statusCode == 410 || exc.code == 'KNOWLEDGE_BASE_DELETED') {
      // 知识库已删除：置只读并移除流式占位，不作为网络错误重试
      kbDeleted = true;
      messages.removeWhere((message) => message.streaming);
    } else {
      final assistant = _streamingAssistant;
      if (assistant != null) {
        assistant.streaming = false;
        assistant.errorMessage = exc.message;
      }
    }
    actionError = exc;
    _finishGeneration();
  }

  void _finishGeneration() {
    final queryId = _activeQueryId;
    generating = false;
    statusText = null;
    _activeQueryId = null;
    _reportClientMetrics(queryId);
    notifyListeners();
    // 会话列表静默刷新：新草稿会话落地/活跃会话顶置
    unawaited(reloadConversations());
    // 生成期间知识库页发生的切库在此补载
    final pending = _pendingKbId;
    _pendingKbId = null;
    if (pending != null) {
      unawaited(loadInitial(kbId: pending));
    }
  }

  /// 终态后上报客户端遥测（时间戳齐全才上报；上报失败不影响界面）
  Future<void> _reportClientMetrics(String? queryId) async {
    final sendAt = _clientSendAt;
    final receivedAt = _firstSseTokenReceivedAt;
    final renderedAt = _firstTokenRenderedAt;
    if (queryId == null ||
        sendAt == null ||
        receivedAt == null ||
        renderedAt == null) {
      return;
    }
    lastTelemetry = (queryId, sendAt, receivedAt, renderedAt);
    try {
      await _query.reportClientMetrics(
        queryId: queryId,
        clientSendAt: sendAt,
        firstSseTokenReceivedAt: receivedAt,
        firstTokenRenderedAt: renderedAt,
      );
    } on ApiException {
      // 遥测非关键路径：失败静默，不打扰问答界面
    }
  }

  /// 请求取消（显式操作；SSE cancelled 事件到达后收尾，不提前置终态）
  Future<void> cancel() async {
    final queryId = _activeQueryId;
    if (queryId == null || !generating) return;
    try {
      await _query.cancelQuery(queryId);
      actionError = null;
    } on ApiException catch (exc) {
      actionError = exc;
      notifyListeners();
    }
  }

  /// 重发最近的问题（失败答案的恢复入口；新建 QueryRun 不覆盖旧答案）
  Future<void> retryLast() async {
    if (generating) return;
    for (final message in messages.reversed) {
      if (message.role == 'user') {
        await send(message.content);
        return;
      }
    }
  }

  String _localId() => 'local-${const Uuid().v4()}';

  // 全局生命周期控制器：测试或热重载卸载宿主树时，仍在途的异步
  // 收尾（列表/消息加载的 notifyListeners）不允许触碰已释放的通知器
  bool _disposed = false;

  @override
  void notifyListeners() {
    if (_disposed) return;
    super.notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _streamSubscription?.cancel();
    super.dispose();
  }
}
