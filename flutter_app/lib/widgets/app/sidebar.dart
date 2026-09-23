import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../controllers/chat_controller.dart';
import '../../api/dto/conversation_dto.dart';
import '../../theme/colors.dart';
import 'conversation_manage_dialog.dart';

/// 侧边栏：一级导航 + 历史会话列表 + 设置入口（吸底）
///
/// 顶部为导航项组（新建会话/知识库/任务中心/评测），中部为跨库全量
/// 历史会话列表（过多时滚动并支持加载更多），底部吸附设置入口。会话
/// 数据与切换动作直接消费全局 [ChatController]；重命名与删除在 API
/// 接受后才更新列表。宽档显示最后消息摘要行，紧凑档只保留标题行。
class WorkbenchSidebar extends StatelessWidget {
  const WorkbenchSidebar({
    super.key,
    required this.currentBranch,
    required this.onGoBranch,
    required this.onNewConversation,
    required this.showSummaries,
  });

  /// 当前活跃路由分支（导航高亮；分支序号与路由表一致）
  final int currentBranch;

  /// 分支切换回调（由外壳提供，携带 lastLocation 持久化语义）
  final void Function(int index) onGoBranch;

  /// 新建会话动作回调（动作按钮而非路由项：外壳负责切换问答分支，
  /// 控制器清为草稿态；分支重置会让问答页重建并恢复旧会话，故不走
  /// 通用分支切换）
  final VoidCallback onNewConversation;

  /// 是否显示会话摘要行（宽档 true，紧凑档 false）
  final bool showSummaries;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final chat = context.watch<ChatController>();
    return SizedBox(
      width: 260,
      child: Container(
        color: theme.colorScheme.surfaceContainerLowest,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const SizedBox(height: 8),
            _NavItem(
              icon: Icons.add_comment_outlined,
              selectedIcon: Icons.add_comment,
              label: '新建会话',
              selected: currentBranch == 0,
              onTap: onNewConversation,
            ),
            _NavItem(
              icon: Icons.library_books_outlined,
              selectedIcon: Icons.library_books,
              label: '知识库',
              selected: currentBranch == 1,
              onTap: () => onGoBranch(1),
            ),
            _NavItem(
              icon: Icons.task_alt,
              selectedIcon: Icons.task_alt,
              label: '任务中心',
              selected: currentBranch == 2,
              onTap: () => onGoBranch(2),
            ),
            _NavItem(
              icon: Icons.insights_outlined,
              selectedIcon: Icons.insights,
              label: '评测',
              selected: currentBranch == 3,
              onTap: () => onGoBranch(3),
            ),
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 2, 8, 0),
              child: Row(
                children: [
                  Text(
                    '历史会话',
                    style: TextStyle(
                      fontSize: 12,
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                  const Spacer(),
                  IconButton(
                    tooltip: '批量管理会话',
                    icon: const Icon(Icons.checklist_rtl, size: 18),
                    visualDensity: VisualDensity.compact,
                    onPressed: chat.conversations.isEmpty
                        ? null
                        : () => _openManageDialog(context, chat),
                  ),
                ],
              ),
            ),
            Expanded(child: _buildConversationList(context, chat, theme)),
            const Divider(height: 1),
            const SizedBox(height: 8),
            _NavItem(
              icon: Icons.settings_outlined,
              selectedIcon: Icons.settings,
              label: '设置',
              selected: currentBranch == 4,
              onTap: () => onGoBranch(4),
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }

  Widget _buildConversationList(
    BuildContext context,
    ChatController chat,
    ThemeData theme,
  ) {
    if (chat.conversationsLoading && chat.conversations.isEmpty) {
      return const Center(
        child: SizedBox(
          width: 22,
          height: 22,
          child: CircularProgressIndicator(strokeWidth: 2.4),
        ),
      );
    }
    if (chat.listError != null && chat.conversations.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(
              chat.listError!.message,
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.center,
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 8),
            TextButton(
              onPressed: () => chat.reloadConversations(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }
    if (chat.conversations.isEmpty) {
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
    final interactionsEnabled = !chat.generating;
    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 6),
      itemCount: chat.conversations.length + (chat.hasMoreConversations ? 1 : 0),
      itemBuilder: (context, index) {
        if (index == chat.conversations.length) {
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Center(
              child: TextButton(
                onPressed: chat.conversationsLoading
                    ? null
                    : () => chat.loadMoreConversations(),
                child: const Text('加载更多'),
              ),
            ),
          );
        }
        final conversation = chat.conversations[index];
        return _ConversationTile(
          conversation: conversation,
          selected: conversation.id == chat.currentConversationId,
          enabled: interactionsEnabled,
          showSummary: showSummaries,
          onTap: () {
            onGoBranch(0);
            chat.selectConversation(conversation.id);
          },
          onRename: () => _renameConversation(context, chat, conversation),
          onDelete: () => _confirmDeleteConversation(
            context,
            chat,
            conversation,
          ),
        );
      },
    );
  }

  Future<void> _openManageDialog(
    BuildContext context,
    ChatController chat,
  ) async {
    await showDialog<void>(
      context: context,
      builder: (context) => ConversationManageDialog(
        conversations: List.of(chat.conversations),
        // 多选删除逐条走既有单删端点，返回实际删除成功的 ID 集合
        onDeleteSelected: (ids) async {
          final deleted = <String>{};
          for (final id in ids) {
            if (await chat.deleteConversation(id)) deleted.add(id);
          }
          return deleted;
        },
        onDeleteAll: chat.deleteAllConversations,
      ),
    );
  }

  Future<void> _confirmDeleteConversation(
    BuildContext context,
    ChatController chat,
    ConversationSummaryDto conversation,
  ) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('删除会话'),
        content: const Text('会话消息与引用将被删除，查询指标事实保留。此操作不可恢复。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            style: ElevatedButton.styleFrom(
              backgroundColor: AppColors.error,
              foregroundColor: Colors.white,
            ),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await chat.deleteConversation(conversation.id);
    }
  }

  Future<void> _renameConversation(
    BuildContext context,
    ChatController chat,
    ConversationSummaryDto conversation,
  ) async {
    final titleController = TextEditingController(text: conversation.title ?? '');
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('重命名会话'),
        content: TextField(
          controller: titleController,
          autofocus: true,
          decoration: const InputDecoration(labelText: '标题'),
          onSubmitted: (_) => Navigator.pop(context, true),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    if (ok == true && titleController.text.trim().isNotEmpty) {
      await chat.renameConversation(conversation.id, titleController.text);
    }
  }
}

/// 侧边栏导航项：图标 + 文字横排，选中以主题色容器高亮
class _NavItem extends StatelessWidget {
  const _NavItem({
    required this.icon,
    required this.selectedIcon,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final IconData icon;
  final IconData selectedIcon;
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      child: Material(
        // 选中高亮取主色低透明度：亮暗主题下均为浅色底，不压内容
        color: selected
            ? theme.colorScheme.primary.withValues(alpha: 0.10)
            : Colors.transparent,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(8),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Row(
              children: [
                Icon(
                  selected ? selectedIcon : icon,
                  size: 20,
                  color: selected
                      ? theme.colorScheme.primary
                      : theme.colorScheme.onSurfaceVariant,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 14,
                      fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
                      color: selected
                          ? theme.colorScheme.primary
                          : theme.colorScheme.onSurface,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// 历史会话条目：标题 + 可选摘要行与重命名/删除入口
class _ConversationTile extends StatelessWidget {
  const _ConversationTile({
    required this.conversation,
    required this.selected,
    required this.enabled,
    required this.showSummary,
    required this.onTap,
    required this.onRename,
    required this.onDelete,
  });

  final ConversationSummaryDto conversation;
  final bool selected;
  final bool enabled;
  final bool showSummary;
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
        // 选中底色与导航项同口径：主色低透明度的浅色高亮
        color: selected
            ? theme.colorScheme.primary.withValues(alpha: 0.10)
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
                      if (showSummary && lastMessage != null) ...[
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
