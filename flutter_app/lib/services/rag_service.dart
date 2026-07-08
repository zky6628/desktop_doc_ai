// 导入 Flutter 基础包
import 'dart:convert';
import 'package:flutter/foundation.dart';
// 导入 HTTP 请求包
import 'package:http/http.dart' as http;

/// RAG 服务类
/// 通过 HTTP 协议与 Python FastAPI 后端通信
/// 替代原有的 stdin/stdout 进程通信方式
class RagService extends ChangeNotifier {
  // 后端服务基础地址
  String _baseUrl = 'http://127.0.0.1:8000';
  // 服务是否已连接（ping 通了）
  bool _isReady = false;
  // 错误信息
  String? _errorMessage;

  // 获取服务是否就绪
  bool get isReady => _isReady;
  // 获取错误信息
  String? get errorMessage => _errorMessage;
  // 获取基础地址
  String get baseUrl => _baseUrl;
  // 兼容旧接口：isRunning 与 isReady 保持一致
  bool get isRunning => _isReady;

  /// 设置后端服务地址
  void setBaseUrl(String url) {
    _baseUrl = url;
    notifyListeners();
  }

  /// 检查服务连接状态（健康检查）
  /// 返回是否连接成功
  Future<bool> connect({String? baseUrl}) async {
    if (baseUrl != null) {
      _baseUrl = baseUrl;
    }

    try {
      debugPrint('正在连接 RAG API 服务: $_baseUrl');

      // 发送 ping 请求测试连接
      final pingResult = await ping();
      if (pingResult != null && pingResult['status'] == 'pong') {
        _isReady = true;
        _errorMessage = null;
        debugPrint('RAG API 服务连接成功！');
        notifyListeners();
        return true;
      } else {
        _isReady = false;
        _errorMessage = '服务响应异常';
        notifyListeners();
        return false;
      }
    } catch (e) {
      _isReady = false;
      _errorMessage = '连接失败: $e';
      debugPrint('连接 RAG API 服务失败: $e');
      notifyListeners();
      return false;
    }
  }

  /// 发送 HTTP GET 请求
  /// [endpoint] 接口路径
  /// 返回 JSON 响应的 Map
  Future<Map<String, dynamic>?> _get(String endpoint) async {
    try {
      final url = Uri.parse('$_baseUrl$endpoint');
      final response = await http.get(
        url,
        headers: {'Content-Type': 'application/json'},
      ).timeout(const Duration(seconds: 30));

      return _parseResponse(response);
    } catch (e) {
      debugPrint('GET 请求失败 ($endpoint): $e');
      return {'error': e.toString()};
    }
  }

  /// 发送 HTTP POST 请求
  /// [endpoint] 接口路径
  /// [body] 请求体（JSON 可序列化对象）
  /// 返回 JSON 响应的 Map
  Future<Map<String, dynamic>?> _post(String endpoint, Map<String, dynamic> body) async {
    try {
      final url = Uri.parse('$_baseUrl$endpoint');
      final response = await http.post(
        url,
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ).timeout(const Duration(seconds: 30));

      return _parseResponse(response);
    } catch (e) {
      debugPrint('POST 请求失败 ($endpoint): $e');
      return {'error': e.toString()};
    }
  }

  /// 解析 HTTP 响应
  Map<String, dynamic>? _parseResponse(http.Response response) {
    try {
      // 解析响应体
      final Map<String, dynamic> data = jsonDecode(response.body);

      // 检查状态码
      if (response.statusCode >= 200 && response.statusCode < 300) {
        return data;
      } else {
        // FastAPI 的错误响应格式通常是 {"detail": "..."}
        final errorDetail = data['detail'] ?? 'HTTP ${response.statusCode}';
        return {'error': errorDetail};
      }
    } catch (e) {
      debugPrint('响应解析失败: $e');
      return {'error': '响应解析失败: $e'};
    }
  }

  // ========== 封装的便捷方法 ==========

  /// 健康检查
  Future<Map<String, dynamic>?> ping() async {
    return _get('/ping');
  }

  /// 添加文件到知识库（通过本地文件路径）
  /// [filePath] 文件的绝对路径
  Future<Map<String, dynamic>?> addFile(String filePath) async {
    return _post('/add_file', {'file_path': filePath});
  }

  /// RAG 问答查询
  /// [question] 用户问题
  /// 返回包含 answer 和 sources 的 Map
  Future<Map<String, dynamic>?> query(String question) async {
    return _post('/query', {'question': question});
  }

  /// 相似度检索
  /// [queryText] 查询文本
  /// [k] 返回结果数量
  Future<Map<String, dynamic>?> search(String queryText, {int k = 3}) async {
    return _post('/search', {'query': queryText, 'k': k});
  }

  /// 获取文档总数
  Future<Map<String, dynamic>?> getCount() async {
    return _get('/count');
  }

  /// 清空知识库
  Future<Map<String, dynamic>?> deleteAll() async {
    return _post('/delete_all', {});
  }

  /// 断开连接（HTTP 无状态，仅更新本地状态）
  Future<void> stop() async {
    _isReady = false;
    _errorMessage = null;
    notifyListeners();
    debugPrint('RAG API 服务已断开');
  }

  // 析构函数（近似），释放资源
  @override
  void dispose() {
    stop();
    super.dispose();
  }
}
