import 'package:flutter/foundation.dart';
import 'package:cross_file/cross_file.dart';
import '../services/rag_service.dart';
import 'chat_message.dart';

class DropFileModel extends ChangeNotifier {
  final List<XFile> _files = [];
  final RagService _ragService = RagService();

  String _queryText = '';
  String? _answer;
  List<String> _sources = [];
  bool _isLoading = false;
  String? _errorMessage;
  final Map<String, String> _fileStatus = {};
  final List<ChatMessage> _chatHistory = [];
  int _msgIdCounter = 0;

  List<XFile> get files => List.unmodifiable(_files);
  int get fileCount => _files.length;
  RagService get ragService => _ragService;
  String get queryText => _queryText;
  String? get answer => _answer;
  List<String> get sources => List.unmodifiable(_sources);
  bool get isLoading => _isLoading;
  String? get errorMessage => _errorMessage;
  List<ChatMessage> get chatHistory => List.unmodifiable(_chatHistory);

  String? getFileStatus(String filePath) => _fileStatus[filePath];

  // ===================== 文件操作 =====================

  void addFiles(List<XFile> newFiles) {
    _files.addAll(newFiles);
    notifyListeners();
  }

  void removeFile(XFile file) {
    _files.remove(file);
    _fileStatus.remove(file.path);
    notifyListeners();
  }

  void clearFiles() {
    _files.clear();
    _fileStatus.clear();
    notifyListeners();
  }

  // ===================== RAG 服务操作 =====================

  Future<bool> initRagService({
    String baseUrl = 'http://127.0.0.1:8000',
  }) async {
    final success = await _ragService.connect(
      baseUrl: baseUrl,
    );
    notifyListeners();
    return success;
  }

  Future<void> stopRagService() async {
    await _ragService.stop();
    notifyListeners();
  }

  // ===================== 文件添加到知识库 =====================

  /// 将单个文件添加到知识库，返回用户友好的结果
  /// [filePath] 文件路径
  /// 返回 null 表示成功，否则返回错误文案
  Future<String?> addFileToKnowledgeBase(String filePath) async {
    _fileStatus[filePath] = '处理中...';
    notifyListeners();

    final result = await _ragService.addFile(filePath);

    if (result.isSuccess) {
      _fileStatus[filePath] = '已添加';
      notifyListeners();
      return null;
    } else {
      final friendlyMsg = result.friendlyErrorMessage;
      _fileStatus[filePath] = '失败: $friendlyMsg';
      notifyListeners();
      return friendlyMsg;
    }
  }

  /// 批量添加所有文件到知识库
  /// 返回 (成功数量, 失败列表)
  Future<(int, List<String>)> addAllFilesToKnowledgeBase() async {
    int successCount = 0;
    final List<String> errors = [];
    for (final file in _files) {
      final error = await addFileToKnowledgeBase(file.path);
      if (error == null) {
        successCount++;
      } else {
        errors.add('${file.name}: $error');
      }
    }
    return (successCount, errors);
  }

  // ===================== RAG 问答 =====================

  void setQueryText(String text) {
    _queryText = text;
    notifyListeners();
  }

  /// 提问并获取回答
  /// 返回 null 表示成功，否则返回用户友好的错误文案
  Future<String?> askQuestion(String question) async {
    _chatHistory.add(ChatMessage(
      id: 'msg_${_msgIdCounter++}',
      type: MessageType.user,
      content: question,
    ));

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
      _chatHistory.add(ChatMessage(
        id: 'msg_${_msgIdCounter++}',
        type: MessageType.assistant,
        content: _answer ?? '(无回答)',
        sources: _sources,
      ));
      _isLoading = false;
      notifyListeners();
      return null;
    } else {
      final friendlyMsg = result.friendlyErrorMessage;
      _errorMessage = friendlyMsg;
      _chatHistory.add(ChatMessage(
        id: 'msg_${_msgIdCounter++}',
        type: MessageType.assistant,
        content: '服务异常: $friendlyMsg',
      ));
      _isLoading = false;
      notifyListeners();
      return friendlyMsg;
    }
  }

  void clearQueryResult() {
    _answer = null;
    _sources = [];
    _errorMessage = null;
    notifyListeners();
  }

  void clearChatHistory() {
    _chatHistory.clear();
    _answer = null;
    _sources = [];
    _errorMessage = null;
    notifyListeners();
  }

  // ===================== 知识库管理 =====================

  Future<int?> getKnowledgeBaseCount() async {
    final result = await _ragService.getCount();
    if (result.isSuccess) {
      return result.data?['count'] as int?;
    }
    return null;
  }

  Future<bool> clearKnowledgeBase() async {
    final result = await _ragService.deleteAll();
    return result.isSuccess;
  }
}
