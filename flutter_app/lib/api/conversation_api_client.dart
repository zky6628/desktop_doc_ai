import 'dart:convert';

import 'package:http/http.dart' as http;

import 'api_client_base.dart';
import 'api_error.dart';
import 'dto/conversation_dto.dart';
import 'dto/knowledge_dto.dart';
import '../app/server_address.dart';

/// 会话域 API 客户端：会话列表/历史消息/删除
///
/// 信封与错误解析复用 [ApiClientBase]；删除为立即生效操作，204 无信封。
class ConversationApiClient {
  ConversationApiClient({
    http.Client? client,
    required ServerAddressStore address,
  }) : _base = ApiClientBase(client: client, address: address);

  final ApiClientBase _base;

  /// 会话列表（keyset 分页，最近活跃倒序，含最后消息摘要）
  ///
  /// kbId 可选：提供时仅返回该库会话，省略时跨库返回全部会话
  /// （问答页历史与知识库解耦，已删除知识库的会话仍可查看）
  Future<ConversationPage> listConversations({
    String? kbId,
    String? cursor,
    int? limit,
  }) async {
    final response = await _base.send(
      (client) => client.get(
        _base.uri('/api/v1/conversations').replace(
              queryParameters: {
                'kb_id': ?kbId,
                'cursor': ?cursor,
                if (limit != null) 'limit': '$limit',
              },
            ),
      ),
    );
    final data = _base.dataOf(response);
    return ApiPage(
      items: [
        for (final item in (data['items'] ?? []) as List)
          ConversationSummaryDto.fromJson(item as Map<String, dynamic>),
      ],
      nextCursor: data['next_cursor'] as String?,
    );
  }

  /// 历史消息（升序全量；已删除知识库的会话仍可读）
  Future<ConversationMessages> listMessages(String conversationId) async {
    final response = await _base.send(
      (client) =>
          client.get(_base.uri('/api/v1/conversations/$conversationId/messages')),
    );
    return ConversationMessages.fromJson(_base.dataOf(response));
  }

  /// 删除会话（立即生效；查询指标事实保留）
  Future<void> deleteConversation(String conversationId) async {
    final response = await _base.send(
      (client) =>
          client.delete(_base.uri('/api/v1/conversations/$conversationId')),
      expectsNoContent: true,
    );
    if (response.statusCode != 204) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '删除会话返回非 204 状态: ${response.statusCode}',
        statusCode: response.statusCode,
      );
    }
  }

  /// 清空全部会话（立即生效；消息级联清除，查询指标事实保留）。
  /// 返回删除的会话数
  Future<int> deleteAllConversations() async {
    final response = await _base.send(
      (client) => client.delete(_base.uri('/api/v1/conversations')),
    );
    final data = _base.dataOf(response);
    return (data['deleted'] as num).toInt();
  }

  /// 重命名会话标题（204；不影响最近活跃排序）
  Future<void> renameConversation(String conversationId, String title) async {
    final response = await _base.send(
      (client) => client.patch(
        _base.uri('/api/v1/conversations/$conversationId'),
        body: jsonEncode({'title': title}),
        headers: {'content-type': 'application/json'},
      ),
      expectsNoContent: true,
    );
    if (response.statusCode != 204) {
      throw ApiException(
        code: 'UNKNOWN',
        message: '重命名会话返回非 204 状态: ${response.statusCode}',
        statusCode: response.statusCode,
      );
    }
  }
}
