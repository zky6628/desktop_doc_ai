import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../theme/colors.dart';
import '../models/drop_file_model.dart';

/// 侧边栏组件
/// 宽度 220px，左侧固定，包含"文件列表"和"对话历史"两个 Tab 切换
class Sidebar extends StatefulWidget {
  /// Tab 切换回调，通知父组件当前选中的 Tab
  final void Function(int index)? onTabChanged;

  /// 添加到知识库回调
  final VoidCallback? onAddToKnowledgeBase;

  /// 选中历史对话回调
  final void Function(int index)? onHistorySelected;

  const Sidebar({
    super.key,
    this.onTabChanged,
    this.onAddToKnowledgeBase,
    this.onHistorySelected,
  });

  @override
  State<Sidebar> createState() => _SidebarState();
}

class _SidebarState extends State<Sidebar> {
  int _currentIndex = 0;

  void _switchTab(int index) {
    setState(() => _currentIndex = index);
    widget.onTabChanged?.call(index);
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 220,
      decoration: const BoxDecoration(
        color: AppColors.card,
        border: Border(
          right: BorderSide(color: AppColors.divider, width: 1),
        ),
      ),
      child: Column(
        children: [
          _buildTabBar(),
          const Divider(height: 1),
          Expanded(child: _buildContent()),
        ],
      ),
    );
  }

  /// 构建 Tab 切换栏
  Widget _buildTabBar() {
    return Container(
      padding: const EdgeInsets.all(8),
      child: Row(
        children: [
          Expanded(child: _buildTabItem('文件列表', Icons.folder_outlined, 0)),
          const SizedBox(width: 4),
          Expanded(child: _buildTabItem('对话历史', Icons.chat_outlined, 1)),
        ],
      ),
    );
  }

  /// 构建单个 Tab 按钮
  Widget _buildTabItem(String label, IconData icon, int index) {
    final isSelected = _currentIndex == index;
    return GestureDetector(
      onTap: () => _switchTab(index),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          color: isSelected ? AppColors.accent : Colors.transparent,
          borderRadius: BorderRadius.circular(6),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              size: 18,
              color: isSelected ? Colors.white : AppColors.textSecondary,
            ),
            const SizedBox(height: 4),
            Text(
              label,
              style: TextStyle(
                fontSize: 11,
                fontWeight: isSelected ? FontWeight.w600 : FontWeight.normal,
                color: isSelected ? Colors.white : AppColors.textSecondary,
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// 根据当前 Tab 构建内容
  Widget _buildContent() {
    return _currentIndex == 0 ? _buildFileList() : _buildChatHistory();
  }

  /// 构建文件列表
  Widget _buildFileList() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        return Column(
          children: [
            // 操作栏
            if (model.fileCount > 0)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                child: Row(
                  children: [
                    Expanded(
                      child: TextButton.icon(
                        onPressed: model.ragService.isReady
                            ? widget.onAddToKnowledgeBase
                            : null,
                        icon: const Icon(Icons.play_arrow, size: 14),
                        label: const Text('添加知识库', style: TextStyle(fontSize: 11)),
                        style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 4),
                        ),
                      ),
                    ),
                    TextButton.icon(
                      onPressed: () => model.clearFiles(),
                      icon: const Icon(Icons.clear_all, size: 14),
                      label: const Text('清空', style: TextStyle(fontSize: 11)),
                      style: TextButton.styleFrom(
                        padding: const EdgeInsets.symmetric(vertical: 4),
                      ),
                    ),
                  ],
                ),
              ),
            // 文件计数
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  '已添加 (${model.fileCount})',
                  style: const TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.w600,
                    color: AppColors.textSecondary,
                  ),
                ),
              ),
            ),
            const SizedBox(height: 4),
            // 文件列表
            Expanded(child: _buildFileListView(model)),
          ],
        );
      },
    );
  }

  /// 文件列表视图
  Widget _buildFileListView(DropFileModel model) {
    if (model.files.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.folder_open, size: 40, color: AppColors.textHint.withValues(alpha: 0.5)),
            const SizedBox(height: 8),
            const Text('暂无文件', style: TextStyle(fontSize: 12, color: AppColors.textHint)),
          ],
        ),
      );
    }

    return ListView.separated(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      itemCount: model.files.length,
      separatorBuilder: (_, _) => const Divider(height: 1, indent: 8, endIndent: 8),
      itemBuilder: (context, index) {
        final file = model.files[index];
        final status = model.getFileStatus(file.path);
        return _buildFileItem(file.name, file.path, status, () {
          model.removeFile(file);
        });
      },
    );
  }

  /// 单个文件项
  Widget _buildFileItem(String name, String path, String? status, VoidCallback onRemove) {
    Color statusColor = AppColors.textHint;
    if (status == '已添加') statusColor = AppColors.success;
    if (status == '处理中...') statusColor = AppColors.warning;
    if (status != null && status.startsWith('失败')) statusColor = AppColors.error;

    return ListTile(
      dense: true,
      contentPadding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      leading: const Icon(Icons.insert_drive_file, size: 18, color: AppColors.textSecondary),
      title: Text(
        name,
        overflow: TextOverflow.ellipsis,
        style: const TextStyle(fontSize: 12),
      ),
      subtitle: status != null
          ? Text(status, style: TextStyle(fontSize: 10, color: statusColor))
          : Text(
              path,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 10, color: AppColors.textHint),
            ),
      trailing: IconButton(
        icon: const Icon(Icons.close, size: 14),
        padding: EdgeInsets.zero,
        constraints: const BoxConstraints(),
        onPressed: onRemove,
      ),
    );
  }

  /// 构建对话历史
  Widget _buildChatHistory() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        final userMessages = model.chatHistory.where((m) => m.isUser).toList();

        if (userMessages.isEmpty) {
          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(Icons.chat_bubble_outline, size: 40, color: AppColors.textHint.withValues(alpha: 0.5)),
                const SizedBox(height: 8),
                const Text('暂无对话记录', style: TextStyle(fontSize: 12, color: AppColors.textHint)),
              ],
            ),
          );
        }

        return ListView.builder(
          padding: const EdgeInsets.symmetric(horizontal: 4),
          itemCount: userMessages.length,
          itemBuilder: (context, index) {
            final msg = userMessages[index];
            final originalIndex = model.chatHistory.indexOf(msg);
            final hasAnswer = originalIndex + 1 < model.chatHistory.length &&
                !model.chatHistory[originalIndex + 1].isUser;

            return ListTile(
              dense: true,
              contentPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
              leading: const Icon(Icons.question_answer, size: 16, color: AppColors.accent),
              title: Text(
                msg.content,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12),
              ),
              subtitle: Text(
                hasAnswer ? '已回答' : '待回答',
                style: TextStyle(
                  fontSize: 10,
                  color: hasAnswer ? AppColors.success : AppColors.warning,
                ),
              ),
              onTap: () => widget.onHistorySelected?.call(originalIndex),
            );
          },
        );
      },
    );
  }
}
