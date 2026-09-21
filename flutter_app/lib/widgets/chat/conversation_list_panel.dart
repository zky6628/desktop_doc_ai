import 'package:flutter/material.dart';

import '../../api/dto/conversation_dto.dart';

/// 会话列表面板：新建会话、最近活跃列表与重命名/删除入口
///
/// 数据以服务端为权威（最近活跃倒序，含最后消息摘要）；重命名与
/// 删除在 API 接受后才更新列表。
class ConversationListPanel extends StatelessWidget {
  const ConversationListPanel({
    super.key,
    required this.conversations,
    required this.currentConversationId,
    required this.loading,
    required this.hasMore,
    required this.enabled,
    required this.onSelect,
    required this.onNewConversation,
    required this.onRename,
    required this.onDelete,
    required this.onLoadMore,
    required this.onRetry,
    this.error,
  });

  final List<ConversationSummaryDto> conversations;
  final String? currentConversationId;
  final bool loading;
  final bool hasMore;
  final bool enabled;
  final ValueChanged<String> onSelect;
  final VoidCallback onNewConversation;
  final ValueChanged<ConversationSummaryDto> onRename;
  final ValueChanged<ConversationSummaryDto> onDelete;
  final VoidCallback onLoadMore;
  final VoidCallback onRetry;
  final String? error;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: 260,
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerLowest,
        border: Border(
          right: BorderSide(color: theme.dividerColor, width: 1),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.all(12),
            child: SizedBox(
              width: double.infinity,
              child: ElevatedButton.icon(
                onPressed: enabled ? onNewConversation : null,
                icon: const Icon(Icons.add, size: 18),
                label: const Text('新建会话'),
              ),
            ),
          ),
          const Divider(height: 1),
          Expanded(child: _buildList(context, theme)),
        ],
      ),
    );
  }

  Widget _buildList(BuildContext context, ThemeData theme) {
    if (loading && conversations.isEmpty) {
      return const Center(
        child: SizedBox(
          width: 22,
          height: 22,
          child: CircularProgressIndicator(strokeWidth: 2.4),
        ),
      );
    }
    if (error != null && conversations.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(
              error!,
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.center,
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 8),
            TextButton(onPressed: onRetry, child: const Text('重试')),
          ],
        ),
      );
    }
    if (conversations.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Text(
            '暂无历史会话\n输入问题即可开始',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 13),
          ),
        ),
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 6),
      itemCount: conversations.length + (hasMore ? 1 : 0),
      itemBuilder: (context, index) {
        if (index == conversations.length) {
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Center(
              child: TextButton(
                onPressed: loading ? null : onLoadMore,
                child: const Text('加载更多'),
              ),
            ),
          );
        }
        final conversation = conversations[index];
        return _ConversationTile(
          conversation: conversation,
          selected: conversation.id == currentConversationId,
          enabled: enabled,
          onTap: () => onSelect(conversation.id),
          onRename: () => onRename(conversation),
          onDelete: () => onDelete(conversation),
        );
      },
    );
  }
}

class _ConversationTile extends StatelessWidget {
  const _ConversationTile({
    required this.conversation,
    required this.selected,
    required this.enabled,
    required this.onTap,
    required this.onRename,
    required this.onDelete,
  });

  final ConversationSummaryDto conversation;
  final bool selected;
  final bool enabled;
  final VoidCallback onTap;
  final VoidCallback onRename;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final lastMessage = conversation.lastMessage;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8),
      child: Material(
        color: selected
            ? theme.colorScheme.primaryContainer.withValues(alpha: 0.5)
            : Colors.transparent,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          onTap: enabled ? onTap : null,
          borderRadius: BorderRadius.circular(8),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        conversation.displayTitle,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      if (lastMessage != null) ...[
                        const SizedBox(height: 2),
                        Text(
                          lastMessage.contentExcerpt,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: 12,
                            color: theme.colorScheme.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                IconButton(
                  tooltip: '重命名会话',
                  icon: const Icon(Icons.edit_outlined, size: 16),
                  visualDensity: VisualDensity.compact,
                  onPressed: enabled ? onRename : null,
                ),
                IconButton(
                  tooltip: '删除会话',
                  icon: const Icon(Icons.delete_outline, size: 16),
                  visualDensity: VisualDensity.compact,
                  onPressed: enabled ? onDelete : null,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
