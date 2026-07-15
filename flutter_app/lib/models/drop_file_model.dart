import 'package:flutter/foundation.dart';
import 'package:cross_file/cross_file.dart';
import '../services/rag_service.dart';
import '../services/database_service.dart';
import 'chat_message.dart';
import 'conversation.dart';
import 'knowledge_file.dart';

/// 文件拖拽与对话状态管理模型
///
/// 负责管理待导入文件、对话历史、知识库文件以及 RAG 问答交互。
/// 通过 [ChangeNotifier] 通知 UI 更新状态。
class DropFileModel extends ChangeNotifier {
  /// 待导入的文件列表（用户拖入但尚未添加到知识库的文件）
  final List<XFile> _pendingFiles = [];

  /// RAG 服务实例，负责与后端 API 通信
  final RagService _ragService = RagService();

  /// 数据库服务实例，负责本地数据持久化
  final DatabaseService _db = DatabaseService();

  /// 当前输入的问题文本
  String _queryText = '';

  /// 当前回答内容
  String? _answer;

  /// 当前回答的参考来源列表
  List<String> _sources = [];

  /// 是否正在加载（等待回答）
  bool _isLoading = false;

  /// 错误信息
  String? _errorMessage;

  /// 文件处理状态缓存（key: 文件路径, value: 状态描述）
  final Map<String, String> _fileStatus = {};

  /// 对话历史列表
  List<Conversation> _conversations = [];

  /// 当前选中的对话
  Conversation? _currentConversation;

  /// 当前对话的消息历史
  List<ChatMessage> _chatHistory = [];

  /// 知识库文件列表（已导入的文件）
  List<KnowledgeFile> _knowledgeFiles = [];

  /// 待导入文件的只读视图
  List<XFile> get pendingFiles => List.unmodifiable(_pendingFiles);

  /// 待导入文件数量
  int get fileCount => _pendingFiles.length;

  /// RAG 服务实例
  RagService get ragService => _ragService;

  /// 当前问题文本
  String get queryText => _queryText;

  /// 当前回答内容
  String? get answer => _answer;

  /// 参考来源的只读视图
  List<String> get sources => List.unmodifiable(_sources);

  /// 是否正在加载
  bool get isLoading => _isLoading;

  /// 错误信息
  String? get errorMessage => _errorMessage;

  /// 聊天历史的只读视图
  List<ChatMessage> get chatHistory => List.unmodifiable(_chatHistory);

  /// 对话列表的只读视图
  List<Conversation> get conversations => List.unmodifiable(_conversations);

  /// 当前选中的对话
  Conversation? get currentConversation => _currentConversation;

  /// 知识库文件的只读视图
  List<KnowledgeFile> get knowledgeFiles => List.unmodifiable(_knowledgeFiles);

  /// 获取指定文件的处理状态
  ///
  /// [filePath] 文件完整路径
  String? getFileStatus(String filePath) => _fileStatus[filePath];

  // ===================== 初始化 =====================

  /// 初始化数据库并加载历史数据
  ///
  /// 会加载对话历史和知识库文件列表。
  Future<void> initDatabase() async {
    await _db.database;
    await loadConversations();
    await loadKnowledgeFiles();
    notifyListeners();
  }

  // ===================== 对话管理 =====================

  /// 从数据库加载所有对话
  ///
  /// 若当前没有选中的对话，会默认选中最新的一条。
  Future<void> loadConversations() async {
    _conversations = await _db.getConversations();
    if (_conversations.isNotEmpty && _currentConversation == null) {
      await selectConversation(_conversations.first.id);
    }
    notifyListeners();
  }

  /// 切换到指定对话
  ///
  /// [id] 对话 ID，会加载该对话的所有历史消息。
  Future<void> selectConversation(String id) async {
    _currentConversation = await _db.getConversation(id);
    if (_currentConversation != null) {
      _chatHistory = await _db.getMessages(id);
      _answer = null;
      _sources = [];
      _errorMessage = null;
    }
    notifyListeners();
  }

  /// 创建新对话并切换到该对话
  Future<void> newConversation() async {
    final convo = await _db.createConversation(title: '新对话');
    _currentConversation = convo;
    _chatHistory = [];
    _answer = null;
    _sources = [];
    _errorMessage = null;
    _conversations.insert(0, convo);
    notifyListeners();
  }

  /// 删除指定对话
  ///
  /// [id] 对话 ID。若删除的是当前对话，会自动切换到第一条对话。
  Future<void> deleteConversation(String id) async {
    await _db.deleteConversation(id);
    _conversations.removeWhere((c) => c.id == id);
    if (_currentConversation?.id == id) {
      if (_conversations.isNotEmpty) {
        await selectConversation(_conversations.first.id);
      } else {
        _currentConversation = null;
        _chatHistory = [];
      }
    }
    notifyListeners();
  }

  /// 更新当前对话的标题
  ///
  /// [title] 新标题
  Future<void> updateCurrentConversationTitle(String title) async {
    if (_currentConversation == null) return;
    await _db.updateConversationTitle(_currentConversation!.id, title);
    _currentConversation = _currentConversation!.copyWith(title: title);
    final idx = _conversations.indexWhere((c) => c.id == _currentConversation!.id);
    if (idx != -1) {
      _conversations[idx] = _currentConversation!;
    }
    notifyListeners();
  }

  // ===================== 文件操作 =====================

  /// 添加待导入文件
  void addFiles(List<XFile> newFiles) {
    _pendingFiles.addAll(newFiles);
    notifyListeners();
  }

  /// 从待导入列表移除指定文件
  void removeFile(XFile file) {
    _pendingFiles.remove(file);
    _fileStatus.remove(file.path);
    notifyListeners();
  }

  /// 清空待导入文件列表
  void clearFiles() {
    _pendingFiles.clear();
    _fileStatus.clear();
    notifyListeners();
  }

  /// 从数据库加载知识库文件列表
  Future<void> loadKnowledgeFiles() async {
    _knowledgeFiles = await _db.getFiles();
    notifyListeners();
  }

  // ===================== RAG 服务操作 =====================

  /// 初始化并连接 RAG 服务
  ///
  /// [baseUrl] 后端服务地址，默认 http://127.0.0.1:8000
  /// 返回连接是否成功
  Future<bool> initRagService({
    String baseUrl = 'http://127.0.0.1:8000',
  }) async {
    final success = await _ragService.connect(
      baseUrl: baseUrl,
    );
    await initDatabase();
    notifyListeners();
    return success;
  }

  /// 停止 RAG 服务并关闭数据库
  Future<void> stopRagService() async {
    await _ragService.stop();
    await _db.close();
    notifyListeners();
  }

  // ===================== 文件添加到知识库 =====================

  /// 将单个文件添加到知识库
  ///
  /// [filePath] 文件完整路径
  /// [fileName] 文件名
  /// 返回错误信息，成功时返回 null
  Future<String?> addFileToKnowledgeBase(String filePath, String fileName) async {
    _fileStatus[filePath] = '处理中...';
    notifyListeners();

    final result = await _ragService.addFile(filePath);

    if (result.isSuccess) {
      _fileStatus[filePath] = '已添加';
      final kf = await _db.addFile(fileName, filePath);
      await _db.updateFileInKB(kf.id, true);
      await loadKnowledgeFiles();
      notifyListeners();
      return null;
    } else {
      final friendlyMsg = result.friendlyErrorMessage;
      _fileStatus[filePath] = '失败: $friendlyMsg';
      notifyListeners();
      return friendlyMsg;
    }
  }

  /// 批量将所有待导入文件添加到知识库
  ///
  /// 返回 (成功数量, 错误信息列表)
  Future<(int, List<String>)> addAllFilesToKnowledgeBase() async {
    int successCount = 0;
    final List<String> errors = [];
    for (final file in _pendingFiles) {
      final error = await addFileToKnowledgeBase(file.path, file.name);
      if (error == null) {
        successCount++;
      } else {
        errors.add('${file.name}: $error');
      }
    }
    _pendingFiles.clear();
    notifyListeners();
    return (successCount, errors);
  }

  /// 删除知识库文件记录（同时从向量库移除）
  ///
  /// [id] 文件 ID
  /// 返回操作是否成功
  Future<bool> deleteKnowledgeFile(String id) async {
    final file = _knowledgeFiles.firstWhere((f) => f.id == id, orElse: () => _knowledgeFiles.first);
    if (file.inKnowledgeBase) {
      final result = await _ragService.deleteBySource(file.filepath);
      if (!result.isSuccess) {
        return false;
      }
    }
    await _db.deleteFile(id);
    await loadKnowledgeFiles();
    return true;
  }

  // ===================== RAG 问答 =====================

  /// 设置当前问题文本
  void setQueryText(String text) {
    _queryText = text;
    notifyListeners();
  }

  /// 提问并获取回答
  ///
  /// [question] 用户问题
  /// 返回错误信息，成功时返回 null。
  /// 回答和消息会自动存入当前对话，若没有对话则新建一个。
  Future<String?> askQuestion(String question) async {
    if (_currentConversation == null) {
      await newConversation();
    }

    final userMsg = ChatMessage(
      id: DateTime.now().millisecondsSinceEpoch.toString(),
      conversationId: _currentConversation!.id,
      type: MessageType.user,
      content: question,
    );
    _chatHistory.add(userMsg);
    await _db.addMessage(userMsg);

    _isLoading = true;
    _errorMessage = null;
    _answer = null;
    _sources = [];
    notifyListeners();

    final result = await _ragService.query(question);

    if (result.isSuccess) {
      final data = result.data ?? {};
      _answer = data['answer'] as String?;
      final sourcesList = data['sources'] as List<dynamic>?;
      if (sourcesList != null) {
        _sources = sourcesList.map((e) => e.toString()).toList();
      }

      QueryMeta? meta;
      final metaJson = data['meta'] as Map<String, dynamic>?;
      if (metaJson != null) {
        meta = QueryMeta.fromJson(metaJson);
      }

      final assistantMsg = ChatMessage(
        id: DateTime.now().millisecondsSinceEpoch.toString(),
        conversationId: _currentConversation!.id,
        type: MessageType.assistant,
        content: _answer ?? '(无回答)',
        sources: _sources,
        meta: meta,
      );
      _chatHistory.add(assistantMsg);
      await _db.addMessage(assistantMsg);

      if (_chatHistory.where((m) => m.isUser).length == 1) {
        final title = question.length > 20 ? '${question.substring(0, 20)}...' : question;
        await updateCurrentConversationTitle(title);
      }

      _isLoading = false;
      notifyListeners();
      return null;
    } else {
      final friendlyMsg = result.friendlyErrorMessage;
      _errorMessage = friendlyMsg;
      final assistantMsg = ChatMessage(
        id: DateTime.now().millisecondsSinceEpoch.toString(),
        conversationId: _currentConversation!.id,
        type: MessageType.assistant,
        content: '服务异常: $friendlyMsg',
      );
      _chatHistory.add(assistantMsg);
      await _db.addMessage(assistantMsg);
      _isLoading = false;
      notifyListeners();
      return friendlyMsg;
    }
  }

  /// 清空当前查询结果
  void clearQueryResult() {
    _answer = null;
    _sources = [];
    _errorMessage = null;
    notifyListeners();
  }

  /// 清空聊天历史并删除当前对话
  void clearChatHistory() {
    _chatHistory.clear();
    _answer = null;
    _sources = [];
    _errorMessage = null;
    if (_currentConversation != null) {
      _db.deleteConversation(_currentConversation!.id);
    }
    notifyListeners();
  }

  // ===================== 知识库管理 =====================

  /// 获取知识库中文档总数
  Future<int?> getKnowledgeBaseCount() async {
    final result = await _ragService.getCount();
    if (result.isSuccess) {
      return result.data?['count'] as int?;
    }
    return null;
  }

  /// 清空整个知识库
  ///
  /// 会将所有本地文件的知识库状态标记为未加入。
  /// 返回操作是否成功
  Future<bool> clearKnowledgeBase() async {
    final result = await _ragService.deleteAll();
    if (result.isSuccess) {
      for (final f in _knowledgeFiles) {
        await _db.updateFileInKB(f.id, false);
      }
      await loadKnowledgeFiles();
    }
    return result.isSuccess;
  }
}
