import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import '../app/server_address.dart';
import 'api_error.dart';

/// 请求超时：本机服务场景下超过即按网络故障处理
const apiRequestTimeout = Duration(seconds: 15);

/// v1 客户端公共基座：URL 拼装、超时与网络故障归类、统一信封解析
///
/// 各域客户端（查询/知识库/任务）组合本基座，错误解析与信封约定
/// 只实现一次；SSE 等流式连接由域客户端自行建立（不走本基座）。
class ApiClientBase {
  ApiClientBase({http.Client? client, required this.address})
    : _client = client ?? http.Client();

  /// 服务地址单一事实源：设置页保存后对后续请求立即生效
  final ServerAddressStore address;

  final http.Client _client;

  Uri uri(String path) => address.value.replace(path: path);

  /// 执行一次请求：超时/网络故障归类，非 2xx 解析信封错误
  Future<http.Response> send(
    Future<http.Response> Function(http.Client client) action, {
    bool expectsNoContent = false,
  }) async {
    http.Response response;
    try {
      response = await action(_client).timeout(apiRequestTimeout);
    } on TimeoutException catch (error) {
      throw ApiException.network('请求超时: $error');
    } on IOException catch (error) {
      throw ApiException.network('网络请求失败: $error');
    } on http.ClientException catch (error) {
      throw ApiException.network('网络请求失败: ${error.message}');
    }
    if (expectsNoContent && response.statusCode == 204) return response;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw errorOf(response);
    }
    return response;
  }

  /// 解析信封 data；success=false 或结构违约时抛出异常
  Map<String, dynamic> dataOf(http.Response response) {
    final body = _decodeBody(response);
    if (body is! Map<String, dynamic> || !body.containsKey('success')) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '响应不符合 v1 信封结构',
        statusCode: response.statusCode,
      );
    }
    if (body['success'] != true) {
      throw _envelopeError(body, response.statusCode);
    }
    final data = body['data'];
    if (data is! Map<String, dynamic>) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '信封 data 缺失或不是对象',
        statusCode: response.statusCode,
        requestId: body['request_id'] as String?,
      );
    }
    return data;
  }

  /// 非 2xx 响应：优先解析信封错误，无法解析时按 UNKNOWN 表达
  ApiException errorOf(http.Response response) {
    final body = _tryDecodeBody(response.body);
    if (body is Map<String, dynamic> &&
        (body['success'] == false || body.containsKey('error'))) {
      return _envelopeError(body, response.statusCode);
    }
    return ApiException(
      code: 'UNKNOWN',
      message: '服务返回 ${response.statusCode} 且未提供信封错误',
      statusCode: response.statusCode,
    );
  }

  ApiException _envelopeError(Map<String, dynamic> body, int statusCode) {
    final errorJson = body['error'];
    final error = errorJson is Map<String, dynamic>
        ? ApiError.fromJson(errorJson)
        : const ApiError(code: 'UNKNOWN', message: '未提供错误信息');
    return ApiException(
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      statusCode: statusCode,
      requestId: body['request_id'] as String?,
    );
  }

  dynamic _decodeBody(http.Response response) {
    try {
      return jsonDecode(response.body);
    } on FormatException {
      throw ApiException(
        code: 'UNKNOWN',
        message: '响应不是合法 JSON',
        statusCode: response.statusCode,
      );
    }
  }

  dynamic _tryDecodeBody(String body) {
    try {
      return jsonDecode(body);
    } on FormatException {
      return null;
    }
  }
}
