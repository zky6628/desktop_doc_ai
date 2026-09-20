/// v1 信封错误载荷与客户端异常模型
///
/// HTTP 状态码承载错误分类，信封 error 提供稳定 code、用户可读
/// message 与可重试性；网络层故障归类为 NETWORK_ERROR（可重试）。
class ApiError {
  const ApiError({
    required this.code,
    required this.message,
    this.retryable = false,
  });

  factory ApiError.fromJson(Map<String, dynamic> json) => ApiError(
        code: json['code'] as String,
        message: (json['message'] ?? '') as String,
        retryable: (json['retryable'] ?? false) as bool,
      );

  final String code;
  final String message;
  final bool retryable;
}

/// API 调用失败异常：携带 HTTP 状态、信封错误与请求关联 ID
class ApiException implements Exception {
  ApiException({
    required this.code,
    required this.message,
    this.retryable = false,
    this.statusCode,
    this.requestId,
  });

  /// 网络层故障（连接失败/超时）；合同定义为可重试
  factory ApiException.network(String message) =>
      ApiException(code: 'NETWORK_ERROR', message: message, retryable: true);

  final String code;
  final String message;
  final bool retryable;
  final int? statusCode;
  final String? requestId;

  bool get isRetryable => retryable;

  @override
  String toString() => 'ApiException($code: $message)';
}
