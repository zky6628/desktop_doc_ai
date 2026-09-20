import 'knowledge_dto.dart';
import 'query_dto.dart';

/// 会话最后消息摘要（服务端折叠空白并截断的正文摘录）
class ConversationLastMessageDto {
  const ConversationLastMessageDto({
    required this.role,
    required this.contentExcerpt,
    required this.createdAt,
  });

  factory ConversationLastMessageDto.fromJson(Map<String, dynamic> json) =>
      ConversationLastMessageDto(
        role: json['role'] as String,
        contentExcerpt: json['content_excerpt'] as String? ?? '',
        createdAt: json['created_at'] as String,
      );

  final String role;
  final String contentExcerpt;
  final String createdAt;
}

/// 会话摘要（GET /conversations 列表项；无消息时 lastMessage 为 null）
class ConversationSummaryDto {
  const ConversationSummaryDto({
    required this.id,
    required this.knowledgeBaseId,
    required this.title,
    required this.createdAt,
    required this.updatedAt,
    required this.lastMessage,
  });

  factory ConversationSummaryDto.fromJson(Map<String, dynamic> json) =>
      ConversationSummaryDto(
        id: json['id'] as String,
        knowledgeBaseId: json['knowledge_base_id'] as String,
        title: json['title'] as String?,
        createdAt: json['created_at'] as String,
        updatedAt: json['updated_at'] as String,
        lastMessage: json['last_message'] == null
            ? null
            : ConversationLastMessageDto.fromJson(
                json['last_message'] as Map<String, dynamic>,
              ),
      );

  final String id;
  final String knowledgeBaseId;
  final String? title;
  final String createdAt;
  final String updatedAt;
  final ConversationLastMessageDto? lastMessage;

  /// 列表展示标题：会话标题为空时按未命名会话呈现
  String get displayTitle =>
      (title != null && title!.trim().isNotEmpty) ? title! : '未命名会话';
}

/// 会话消息（GET /conversations/{id}/messages 列表项）
class ConversationMessageDto {
  const ConversationMessageDto({
    required this.id,
    required this.role,
    required this.content,
    required this.createdAt,
    required this.citations,
  });

  factory ConversationMessageDto.fromJson(Map<String, dynamic> json) =>
      ConversationMessageDto(
        id: json['id'] as String,
        role: json['role'] as String,
        content: json['content'] as String,
        createdAt: json['created_at'] as String,
        citations: [
          // 助手消息携带引用快照（历史 [S编号] 定位），用户消息为空列表
          for (final item in (json['citations'] ?? []) as List)
            CitationSnapshot.fromJson(item as Map<String, dynamic>),
        ],
      );

  final String id;
  final String role;
  final String content;
  final String createdAt;
  final List<CitationSnapshot> citations;
}

/// 会话消息历史（升序全量）
class ConversationMessages {
  const ConversationMessages({
    required this.conversationId,
    required this.items,
  });

  factory ConversationMessages.fromJson(Map<String, dynamic> json) =>
      ConversationMessages(
        conversationId: json['conversation_id'] as String,
        items: [
          for (final item in (json['items'] ?? []) as List)
            ConversationMessageDto.fromJson(item as Map<String, dynamic>),
        ],
      );

  final String conversationId;
  final List<ConversationMessageDto> items;
}

/// 会话分页（复用知识库域的分页信封）
typedef ConversationPage = ApiPage<ConversationSummaryDto>;
