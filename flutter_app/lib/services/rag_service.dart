// 导入 Flutter 基础包
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
// 导入 HTTP 请求包
import 'package:http/http.dart' as http;

/// API 错误码定义（与 Python 后端保持一致）
class ApiErrorCode {
  static const String fileNotFound = 'FILE_NOT_FOUND';
  static const String unsupportedFormat = 'UNSUPPORTED_FORMAT';
  static const String emptyFile = 'EMPTY_FILE';
  static const String apiError = 'API_ERROR';
  static const String dbError = 'DB_ERROR';
  static const String timeout = 'TIMEOUT';
  static const String unknown = 'UNKNOWN';
  static const String invalidParam = 'INVALID_PARAM';
  static const String serviceUnavailable = 'SERVICE_UNAVAILABLE';
}

/// API 响应结果封装
class ApiResult<T> {
  final bool success;
  final T? data;
  final String? errorCode;
  final String? errorMessage;

  bool get isSuccess => success;
  bool get isError => !success;

  ApiResult._({
    required this.success,
    this.data,
    this.errorCode,
    this.errorMessage,
  });

  factory ApiResult.success(T data) {
    return ApiResult._(success: true, data: data);
  }

  factory ApiResult.error(String code, String message) {
    return ApiResult._(
      success: false,
      errorCode: code,
      errorMessage: message,
    );
  }

  /// 获取用户友好的错误文案
  String get friendlyErrorMessage {
    if (errorMessage == null || errorMessage!.isEmpty) {
      return '操作失败，请稍后重试';
    }
    switch (errorCode) {
      case ApiErrorCode.fileNotFound:
        return '文件不存在，请检查文件路径';
      case ApiErrorCode.unsupportedFormat:
        return '暂不支持该文件格式，请上传 PDF / TXT / DOCX 文件';
      case ApiErrorCode.emptyFile:
        return '文件内容为空，请检查文件';
      case ApiErrorCode.apiError:
        return 'AI 服务暂不可用，请稍后重试';
      case ApiErrorCode.dbError:
        return '数据库操作失败，请稍后重试';
      case ApiErrorCode.timeout:
        return '请求超时，请稍后重试';
      case ApiErrorCode.invalidParam:
        return errorMessage!;
      case ApiErrorCode.serviceUnavailable:
        return '服务暂不可用，请检查后端服务是否启动';
      default:
        return '操作失败: $errorMessage';
    }
  }
}

/// LLM 模型信息
class LlmModelInfo {
  final String id;
  final String name;
  final String provider;

  LlmModelInfo({
    required this.id,
    required this.name,
    required this.provider,
  });

  factory LlmModelInfo.fromJson(Map<String, dynamic> json) {
    return LlmModelInfo(
      id: json['id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      provider: json['provider'] as String? ?? '',
    );
  }

  bool get isLocal => provider == 'local';
}

/// 问答元数据（耗时、tokens 等）
class QueryMeta {
  final double time;
  final double retrieveTime;
  final double llmTime;
  final int tokens;
  final String model;
  final String provider;

  QueryMeta({
    this.time = 0,
    this.retrieveTime = 0,
    this.llmTime = 0,
    this.tokens = 0,
    this.model = '',
    this.provider = '',
  });

  factory QueryMeta.fromJson(Map<String, dynamic> json) {
    return QueryMeta(
      time: (json['time'] as num?)?.toDouble() ?? 0,
      retrieveTime: (json['retrieve_time'] as num?)?.toDouble() ?? 0,
      llmTime: (json['llm_time'] as num?)?.toDouble() ?? 0,
      tokens: (json['tokens'] as num?)?.toInt() ?? 0,
      model: json['model'] as String? ?? '',
      provider: json['provider'] as String? ?? '',
    );
  }
}

/// RAG 服务类
/// 通过 HTTP 协议与 Python FastAPI 后端通信
class RagService extends ChangeNotifier {
  // 后端服务基础地址
  String _baseUrl = 'http://127.0.0.1:8000';
  // 服务是否已连接（ping 通了）
  bool _isReady = false;
  // 错误信息
  String? _errorMessage;
  // 可用模型列表
  List<LlmModelInfo> _models = [];
  // 当前选中的模型 ID
  String? _currentModelId;

  // 获取服务是否就绪
  bool get isReady => _isReady;
  // 获取错误信息
  String? get errorMessage => _errorMessage;
  // 获取基础地址
  String get baseUrl => _baseUrl;
  // 兼容旧接口：isRunning 与 isReady 保持一致
  bool get isRunning => _isReady;
  // 可用模型列表
  List<LlmModelInfo> get models => List.unmodifiable(_models);
  // 当前选中的模型 ID
  String? get currentModelId => _currentModelId;

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
      final result = await ping();
      if (result.isSuccess && result.data != null && result.data!['status'] == 'pong') {
        _isReady = true;
        _errorMessage = null;
        debugPrint('RAG API 服务连接成功！');

        // 加载可用模型列表
        await _loadModels();

        notifyListeners();
        return true;
      } else {
        _isReady = false;
        _errorMessage = result.errorMessage ?? '服务响应异常';
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

  /// 加载可用模型列表
  Future<void> _loadModels() async {
    try {
      final result = await getModels();
      if (result.isSuccess && result.data != null) {
        final modelsList = result.data!['models'] as List<dynamic>?;
        if (modelsList != null) {
          _models = modelsList
              .map((e) => LlmModelInfo.fromJson(e as Map<String, dynamic>))
              .toList();
          // 默认选中第一个模型
          if (_models.isNotEmpty && _currentModelId == null) {
            _currentModelId = _models.first.id;
          }
          debugPrint('加载到 ${_models.length} 个可用模型');
        }
      }
    } catch (e) {
      debugPrint('加载模型列表失败: $e');
    }
  }

  /// 切换当前模型
  void setCurrentModel(String modelId) {
    _currentModelId = modelId;
    notifyListeners();
  }

  /// 发送 HTTP GET 请求
  /// [endpoint] 接口路径
  /// 返回解析后的 ApiResult
  Future<ApiResult<Map<String, dynamic>>> _get(String endpoint) async {
    try {
      final url = Uri.parse('$_baseUrl$endpoint');
      final response = await http.get(
        url,
        headers: {'Content-Type': 'application/json'},
      ).timeout(const Duration(seconds: 120));

      return _parseResponse(response);
    } on SocketException catch (e) {
      debugPrint('GET 请求网络错误 ($endpoint): $e');
      return ApiResult.error(
        ApiErrorCode.serviceUnavailable,
        '无法连接到后端服务',
      );
    } on FormatException catch (e) {
      debugPrint('GET 请求响应格式错误 ($endpoint): $e');
      return ApiResult.error(ApiErrorCode.unknown, '响应解析失败');
    } on Exception catch (e) {
      debugPrint('GET 请求失败 ($endpoint): $e');
      return ApiResult.error(ApiErrorCode.unknown, e.toString());
    }
  }

  /// 发送 HTTP POST 请求
  /// [endpoint] 接口路径
  /// [body] 请求体（JSON 可序列化对象）
  /// 返回解析后的 ApiResult
  Future<ApiResult<Map<String, dynamic>>> _post(
    String endpoint,
    Map<String, dynamic> body,
  ) async {
    try {
      final url = Uri.parse('$_baseUrl$endpoint');
      final response = await http.post(
        url,
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ).timeout(const Duration(seconds: 120));

      return _parseResponse(response);
    } on SocketException catch (e) {
      debugPrint('POST 请求网络错误 ($endpoint): $e');
      return ApiResult.error(
        ApiErrorCode.serviceUnavailable,
        '无法连接到后端服务',
      );
    } on FormatException catch (e) {
      debugPrint('POST 请求响应格式错误 ($endpoint): $e');
      return ApiResult.error(ApiErrorCode.unknown, '响应解析失败');
    } on Exception catch (e) {
      debugPrint('POST 请求失败 ($endpoint): $e');
      return ApiResult.error(ApiErrorCode.unknown, e.toString());
    }
  }

  /// 解析 HTTP 响应
  ApiResult<Map<String, dynamic>> _parseResponse(http.Response response) {
    try {
      // 解析响应体
      final Map<String, dynamic> data = jsonDecode(response.body);

      // 优先识别统一响应格式: {success, data, error}
      if (data.containsKey('success')) {
        final bool success = data['success'] as bool;
        if (success) {
          final responseData = data['data'];
          if (responseData is Map<String, dynamic>) {
            return ApiResult.success(responseData);
          } else {
            // 将非 Map 数据包装为 Map
            return ApiResult.success({'value': responseData});
          }
        } else {
          final error = data['error'];
          if (error is Map<String, dynamic>) {
            return ApiResult.error(
              error['code'] as String? ?? ApiErrorCode.unknown,
              error['message'] as String? ?? '未知错误',
            );
          } else {
            return ApiResult.error(ApiErrorCode.unknown, error?.toString() ?? '未知错误');
          }
        }
      }

      // 兼容旧格式：HTTP 状态码判断
      if (response.statusCode >= 200 && response.statusCode < 300) {
        return ApiResult.success(data);
      } else {
        final errorDetail = data['detail'] ?? 'HTTP ${response.statusCode}';
        return ApiResult.error(ApiErrorCode.unknown, errorDetail.toString());
      }
    } catch (e) {
      debugPrint('响应解析失败: $e');
      return ApiResult.error(ApiErrorCode.unknown, '响应解析失败: $e');
    }
  }

  // ========== 封装的便捷方法 ==========

  /// 健康检查
  Future<ApiResult<Map<String, dynamic>>> ping() async {
    return _get('/ping');
  }

  /// 添加文件到知识库（通过本地文件路径）
  /// [filePath] 文件的绝对路径
  Future<ApiResult<Map<String, dynamic>>> addFile(String filePath) async {
    return _post('/add_file', {'file_path': filePath});
  }

  /// RAG 问答查询
  /// [question] 用户问题
  /// [modelId] 模型 ID（可选，默认使用当前选中的模型）
  /// 返回包含 answer、sources 和 meta 的 Map
  Future<ApiResult<Map<String, dynamic>>> query(
    String question, {
    String? modelId,
  }) async {
    final body = <String, dynamic>{'question': question};
    final mid = modelId ?? _currentModelId;
    if (mid != null) {
      body['model_id'] = mid;
    }
    return _post('/query', body);
  }

  /// 获取可用模型列表
  Future<ApiResult<Map<String, dynamic>>> getModels() async {
    return _get('/models');
  }

  /// 相似度检索
  /// [queryText] 查询文本
  /// [k] 返回结果数量
  Future<ApiResult<Map<String, dynamic>>> search(
    String queryText, {
    int k = 3,
  }) async {
    return _post('/search', {'query': queryText, 'k': k});
  }

  /// 获取文档总数
  Future<ApiResult<Map<String, dynamic>>> getCount() async {
    return _get('/count');
  }

  /// 清空知识库
  Future<ApiResult<Map<String, dynamic>>> deleteAll() async {
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
