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

  Future<bool> addFileToKnowledgeBase(String filePath) async {
    _fileStatus[filePath] = '处理中...';
    notifyListeners();

    try {
      final result = await _ragService.addFile(filePath);

      if (result != null && !result.containsKey('error')) {
        _fileStatus[filePath] = '已添加';
        notifyListeners();
        return true;
      } else {
        _fileStatus[filePath] = '失败: ${result?['error'] ?? '未知错误'}';
        notifyListeners();
        return false;
      }
    } catch (e) {
      _fileStatus[filePath] = '失败: $e';
      notifyListeners();
      return false;
    }
  }

  Future<int> addAllFilesToKnowledgeBase() async {
    int successCount = 0;
    for (final file in _files) {
      final success = await addFileToKnowledgeBase(file.path);
      if (success) successCount++;
    }
    return successCount;
  }

  // ===================== RAG 问答 =====================

  void setQueryText(String text) {
    _queryText = text;
    notifyListeners();
  }

  Future<void> askQuestion(String question) async {
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

    try {
      final result = await _ragService.query(question);

      if (result != null) {
        if (result.containsKey('error')) {
          _errorMessage = result['error'] as String;
          _chatHistory.add(ChatMessage(
            id: 'msg_${_msgIdCounter++}',
            type: MessageType.assistant,
            content: '查询出错: ${result['error']}',
          ));
        } else {
          _answer = result['answer'] as String?;
          final sourcesList = result['sources'] as List<dynamic>?;
          if (sourcesList != null) {
            _sources = sourcesList.map((e) => e.toString()).toList();
          }
          _chatHistory.add(ChatMessage(
            id: 'msg_${_msgIdCounter++}',
            type: MessageType.assistant,
            content: _answer ?? '(无回答)',
            sources: _sources,
          ));
        }
      } else {
        _errorMessage = '查询失败，无返回结果';
        _chatHistory.add(ChatMessage(
          id: 'msg_${_msgIdCounter++}',
          type: MessageType.assistant,
          content: '查询失败，无返回结果',
        ));
      }
    } catch (e) {
      _errorMessage = '查询异常: $e';
      _chatHistory.add(ChatMessage(
        id: 'msg_${_msgIdCounter++}',
        type: MessageType.assistant,
        content: '查询异常: $e',
      ));
    } finally {
      _isLoading = false;
      notifyListeners();
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
    if (result != null && !result.containsKey('error')) {
      return result['count'] as int?;
    }
    return null;
  }

  Future<bool> clearKnowledgeBase() async {
    final result = await _ragService.deleteAll();
    return result != null && !result.containsKey('error');
  }
}
