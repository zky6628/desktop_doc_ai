import '../services/rag_service.dart';
import 'dart:convert';

/// 对话消息类型
enum MessageType { user, assistant }

/// 对话消息模型
/// 封装单条对话消息的内容和元数据
class ChatMessage {
  /// 消息唯一标识
  final String id;

  /// 所属对话 ID
  final String? conversationId;

  /// 消息类型（用户 / AI）
  final MessageType type;

  /// 消息文本内容
  final String content;

  /// 参考来源列表（仅 AI 消息可能有）
  final List<String> sources;

  /// 发送时间
  final DateTime timestamp;

  /// 问答元数据（耗时、tokens、模型等，仅 AI 消息有）
  final QueryMeta? meta;

  ChatMessage({
    required this.id,
    this.conversationId,
    required this.type,
    required this.content,
    this.sources = const [],
    DateTime? timestamp,
    this.meta,
  }) : timestamp = timestamp ?? DateTime.now();

  /// 是否为用户消息
  bool get isUser => type == MessageType.user;

  factory ChatMessage.fromMap(Map<String, dynamic> map) {
    final sourcesJson = map['sources'] as String?;
    final sourcesList = sourcesJson != null
        ? (jsonDecode(sourcesJson) as List).cast<String>()
        : <String>[];

    final metaJson = map['meta'] as String?;
    QueryMeta? meta;
    if (metaJson != null) {
      final metaMap = jsonDecode(metaJson) as Map<String, dynamic>;
      meta = QueryMeta.fromJson(metaMap);
    }

    return ChatMessage(
      id: map['id'] as String,
      conversationId: map['conversation_id'] as String?,
      type: map['role'] == 'user' ? MessageType.user : MessageType.assistant,
      content: map['content'] as String,
      sources: sourcesList,
      timestamp: DateTime.fromMillisecondsSinceEpoch(map['created_at'] as int),
      meta: meta,
    );
  }

  Map<String, dynamic> toMap() {
    return {
      'id': id,
      'conversation_id': conversationId,
      'role': type == MessageType.user ? 'user' : 'assistant',
      'content': content,
      'sources': jsonEncode(sources),
      'created_at': timestamp.millisecondsSinceEpoch,
      'meta': meta != null ? jsonEncode(metaToJson(meta!)) : null,
    };
  }

  Map<String, dynamic> metaToJson(QueryMeta m) {
    return {
      'time': m.time,
      'retrieve_time': m.retrieveTime,
      'llm_time': m.llmTime,
      'tokens': m.tokens,
      'model': m.model,
      'provider': m.provider,
    };
  }
}
