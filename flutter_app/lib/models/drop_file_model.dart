// 导入 Flutter 基础包，用于 ChangeNotifier
import 'package:flutter/foundation.dart';
// 导入跨平台文件类
import 'package:cross_file/cross_file.dart';
// 导入 RAG 服务
import '../services/rag_service.dart';

/// 文件和 RAG 状态管理模型
/// 管理文件列表和 RAG 问答功能
class DropFileModel extends ChangeNotifier {
  // 内部文件列表
  final List<XFile> _files = [];
  // RAG 服务实例
  final RagService _ragService = RagService();

  // 查询输入控制器的内容
  String _queryText = '';
  // 查询回答结果
  String? _answer;
  // 参考来源列表
  List<String> _sources = [];
  // 是否正在加载（查询中）
  bool _isLoading = false;
  // 当前错误信息
  String? _errorMessage;
  // 文件处理状态映射：文件路径 -> 处理状态
  final Map<String, String> _fileStatus = {};

  // 获取不可修改的文件列表
  List<XFile> get files => List.unmodifiable(_files);
  // 获取文件数量
  int get fileCount => _files.length;
  // 获取 RAG 服务
  RagService get ragService => _ragService;
  // 获取查询文本
  String get queryText => _queryText;
  // 获取回答
  String? get answer => _answer;
  // 获取参考来源
  List<String> get sources => List.unmodifiable(_sources);
  // 获取是否加载中
  bool get isLoading => _isLoading;
  // 获取错误信息
  String? get errorMessage => _errorMessage;

  // 获取文件处理状态
  String? getFileStatus(String filePath) => _fileStatus[filePath];

  // ===================== 文件操作 =====================

  /// 添加文件
  void addFiles(List<XFile> newFiles) {
    _files.addAll(newFiles);
    notifyListeners();
  }

  /// 移除文件
  void removeFile(XFile file) {
    _files.remove(file);
    _fileStatus.remove(file.path);
    notifyListeners();
  }

  /// 清空文件列表
  void clearFiles() {
    _files.clear();
    _fileStatus.clear();
    notifyListeners();
  }

  // ===================== RAG 服务操作 =====================

  /// 初始化（连接）RAG 服务
  /// [baseUrl] 后端 API 服务地址，默认为 http://127.0.0.1:8000
  Future<bool> initRagService({
    String baseUrl = 'http://127.0.0.1:8000',
  }) async {
    final success = await _ragService.connect(
      baseUrl: baseUrl,
    );
    notifyListeners();
    return success;
  }

  /// 停止 RAG 服务
  Future<void> stopRagService() async {
    await _ragService.stop();
    notifyListeners();
  }

  // ===================== 文件添加到知识库 =====================

  /// 将文件添加到知识库
  /// [filePath] 文件的绝对路径
  Future<bool> addFileToKnowledgeBase(String filePath) async {
    // 设置文件状态为处理中
    _fileStatus[filePath] = '处理中...';
    notifyListeners();

    try {
      // 调用 RAG 服务添加文件
      final result = await _ragService.addFile(filePath);

      if (result != null && !result.containsKey('error')) {
        // 添加成功
        _fileStatus[filePath] = '已添加';
        notifyListeners();
        return true;
      } else {
        // 添加失败
        _fileStatus[filePath] = '失败: ${result?['error'] ?? '未知错误'}';
        notifyListeners();
        return false;
      }
    } catch (e) {
      // 异常
      _fileStatus[filePath] = '失败: $e';
      notifyListeners();
      return false;
    }
  }

  /// 将所有文件添加到知识库
  Future<int> addAllFilesToKnowledgeBase() async {
    int successCount = 0;
    for (final file in _files) {
      final success = await addFileToKnowledgeBase(file.path);
      if (success) successCount++;
    }
    return successCount;
  }

  // ===================== RAG 问答 =====================

  /// 设置查询文本
  void setQueryText(String text) {
    _queryText = text;
    notifyListeners();
  }

  /// 执行 RAG 查询
  /// [question] 用户问题
  Future<void> askQuestion(String question) async {
    // 设置加载状态
    _isLoading = true;
    _errorMessage = null;
    _answer = null;
    _sources = [];
    notifyListeners();

    try {
      // 调用 RAG 服务查询
      final result = await _ragService.query(question);

      if (result != null) {
        if (result.containsKey('error')) {
          // 查询出错
          _errorMessage = result['error'] as String;
        } else {
          // 查询成功，解析回答和来源
          _answer = result['answer'] as String?;
          final sourcesList = result['sources'] as List<dynamic>?;
          if (sourcesList != null) {
            _sources = sourcesList.map((e) => e.toString()).toList();
          }
        }
      } else {
        _errorMessage = '查询失败，无返回结果';
      }
    } catch (e) {
      _errorMessage = '查询异常: $e';
    } finally {
      // 关闭加载状态
      _isLoading = false;
      notifyListeners();
    }
  }

  /// 清空查询结果
  void clearQueryResult() {
    _answer = null;
    _sources = [];
    _errorMessage = null;
    notifyListeners();
  }

  // ===================== 知识库管理 =====================

  /// 获取知识库文档数量
  Future<int?> getKnowledgeBaseCount() async {
    final result = await _ragService.getCount();
    if (result != null && !result.containsKey('error')) {
      return result['count'] as int?;
    }
    return null;
  }

  /// 清空知识库
  Future<bool> clearKnowledgeBase() async {
    final result = await _ragService.deleteAll();
    return result != null && !result.containsKey('error');
  }
}
