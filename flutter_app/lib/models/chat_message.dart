import '../services/rag_service.dart';

/// 对话消息类型
enum MessageType { user, assistant }

/// 对话消息模型
/// 封装单条对话消息的内容和元数据
class ChatMessage {
  /// 消息唯一标识
  final String id;

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
    required this.type,
    required this.content,
    this.sources = const [],
    DateTime? timestamp,
    this.meta,
  }) : timestamp = timestamp ?? DateTime.now();

  /// 是否为用户消息
  bool get isUser => type == MessageType.user;
}
