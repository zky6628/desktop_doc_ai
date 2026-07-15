import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:cross_file/cross_file.dart';
import '../theme/colors.dart';
import '../models/drop_file_model.dart';
import '../models/conversation.dart';
import '../models/knowledge_file.dart';
import '../utils/file_handler.dart';

/// 侧边栏组件
/// 宽度 260px，左侧固定，包含"对话历史"和"文件管理"两个 Tab
class Sidebar extends StatefulWidget {
  final VoidCallback? onAddToKnowledgeBase;

  const Sidebar({
    super.key,
    this.onAddToKnowledgeBase,
  });

  @override
  State<Sidebar> createState() => _SidebarState();
}

class _SidebarState extends State<Sidebar> {
  int _currentIndex = 0;

  void _switchTab(int index) {
    setState(() => _currentIndex = index);
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 260,
      decoration: const BoxDecoration(
        color: AppColors.card,
        border: Border(
          right: BorderSide(color: AppColors.divider, width: 1),
        ),
      ),
      child: Column(
        children: [
          _buildHeader(),
          const Divider(height: 1),
          _buildTabBar(),
          const Divider(height: 1),
          Expanded(child: _buildContent()),
        ],
      ),
    );
  }

  /// 构建顶部头部（新建对话按钮）
  Widget _buildHeader() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        return Padding(
          padding: const EdgeInsets.all(12),
          child: SizedBox(
            width: double.infinity,
            child: ElevatedButton.icon(
              onPressed: () => model.newConversation(),
              icon: const Icon(Icons.add, size: 18),
              label: const Text('新建对话'),
              style: ElevatedButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 10),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(8),
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  /// 构建 Tab 切换栏
  Widget _buildTabBar() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      child: Row(
        children: [
          Expanded(child: _buildTabItem('对话历史', Icons.chat_outlined, 0)),
          const SizedBox(width: 4),
          Expanded(child: _buildTabItem('文件管理', Icons.folder_outlined, 1)),
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
    return _currentIndex == 0 ? _buildChatHistory() : _buildFileManager();
  }

  // ===================== 对话历史 =====================

  Widget _buildChatHistory() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        final conversations = model.conversations;

        if (conversations.isEmpty) {
          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(Icons.chat_bubble_outline,
                    size: 40, color: AppColors.textHint.withValues(alpha: 0.5)),
                const SizedBox(height: 8),
                const Text('暂无对话记录',
                    style: TextStyle(fontSize: 12, color: AppColors.textHint)),
              ],
            ),
          );
        }

        return ListView.separated(
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
          itemCount: conversations.length,
          separatorBuilder: (_, _) => const SizedBox(height: 2),
          itemBuilder: (context, index) {
            final convo = conversations[index];
            final isSelected = model.currentConversation?.id == convo.id;
            return _buildConversationItem(convo, isSelected, model);
          },
        );
      },
    );
  }

  Widget _buildConversationItem(
      Conversation convo, bool isSelected, DropFileModel model) {
    return Dismissible(
      key: Key(convo.id),
      direction: DismissDirection.endToStart,
      confirmDismiss: (direction) async {
        return await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('删除对话'),
            content: const Text('确定要删除这个对话吗？此操作不可恢复。'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('取消'),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFDC2626),
                  foregroundColor: Colors.white,
                ),
                onPressed: () => Navigator.pop(context, true),
                child: const Text('删除'),
              ),
            ],
          ),
        );
      },
      onDismissed: (_) {
        model.deleteConversation(convo.id);
      },
      background: Container(
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.only(right: 16),
        decoration: BoxDecoration(
          color: const Color(0xFFDC2626),
          borderRadius: BorderRadius.circular(6),
        ),
        child: const Icon(Icons.delete, color: Colors.white, size: 20),
      ),
      child: GestureDetector(
        onTap: () {
          model.selectConversation(convo.id);
        },
        onLongPress: () => _showConversationMenu(convo, model),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 150),
          margin: const EdgeInsets.symmetric(horizontal: 2),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
          decoration: BoxDecoration(
            color: isSelected
                ? AppColors.accent.withValues(alpha: 0.12)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(6),
            border: Border.all(
              color: isSelected
                  ? AppColors.accent.withValues(alpha: 0.3)
                  : Colors.transparent,
              width: 1,
            ),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(
                Icons.chat_bubble_outline,
                size: 16,
                color: isSelected ? AppColors.accent : AppColors.textSecondary,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      convo.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: 13,
                        fontWeight:
                            isSelected ? FontWeight.w600 : FontWeight.w500,
                        color: isSelected
                            ? AppColors.accent
                            : AppColors.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      _formatDate(convo.updatedAt),
                      style: const TextStyle(
                        fontSize: 10,
                        color: AppColors.textHint,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void _showConversationMenu(Conversation convo, DropFileModel model) {
    showModalBottomSheet(
      context: context,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.delete_outline, color: Color(0xFFDC2626)),
              title: const Text('删除对话',
                  style: TextStyle(color: Color(0xFFDC2626))),
              onTap: () {
                Navigator.pop(context);
                model.deleteConversation(convo.id);
              },
            ),
          ],
        ),
      ),
    );
  }

  String _formatDate(DateTime date) {
    final now = DateTime.now();
    final diff = now.difference(date);
    if (diff.inMinutes < 1) return '刚刚';
    if (diff.inHours < 1) return '${diff.inMinutes}分钟前';
    if (diff.inDays < 1) return '${diff.inHours}小时前';
    if (diff.inDays < 7) return '${diff.inDays}天前';
    return '${date.month}/${date.day} ${date.hour.toString().padLeft(2, '0')}:${date.minute.toString().padLeft(2, '0')}';
  }

  // ===================== 文件管理 =====================

  /// 构建文件管理面板
  ///
  /// 包含待添加文件列表和知识库文件列表，均支持滚动查看和删除操作。
  Widget _buildFileManager() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        final kbFiles = model.knowledgeFiles;
        final pendingFiles = model.pendingFiles;

        return CustomScrollView(
          slivers: [
            // 待添加文件区域
            if (pendingFiles.isNotEmpty) _buildPendingFilesSliver(model, pendingFiles),
            // 知识库文件区域
            _buildKnowledgeFilesSliver(model, kbFiles),
          ],
        );
      },
    );
  }

  /// 构建待添加文件区域（Sliver）
  ///
  /// 显示拖拽到应用但尚未添加到知识库的文件，支持删除和批量添加。
  SliverToBoxAdapter _buildPendingFilesSliver(DropFileModel model, List<XFile> files) {
    return SliverToBoxAdapter(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  '待添加 (${files.length})',
                  style: const TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.w600,
                    color: AppColors.textSecondary,
                  ),
                ),
                TextButton(
                  onPressed: model.ragService.isReady
                      ? widget.onAddToKnowledgeBase
                      : null,
                  style: TextButton.styleFrom(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    minimumSize: const Size(0, 0),
                    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                  ),
                  child: Text(
                    '全部添加',
                    style: TextStyle(
                      fontSize: 10,
                      color: model.ragService.isReady
                          ? AppColors.primary
                          : AppColors.textHint,
                    ),
                  ),
                ),
              ],
            ),
          ),
          const Divider(height: 1),
          ...files.map((file) => _buildPendingFileItem(file, model)),
          const Divider(height: 1),
        ],
      ),
    );
  }

  /// 构建单个待添加文件项
  ///
  /// 支持左滑删除文件。
  Widget _buildPendingFileItem(XFile file, DropFileModel model) {
    final typeIcon = FileHandler.getFileTypeIcon(file.name);
    final typeColor = FileHandler.getFileTypeColor(file.name);
    final typeLabel = FileHandler.getFileTypeLabel(file.name);
    final status = model.getFileStatus(file.path);

    return Dismissible(
      key: Key(file.path),
      direction: DismissDirection.endToStart,
      confirmDismiss: (direction) async {
        return await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('移除文件'),
            content: Text('确定要移除 "${file.name}" 吗？'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('取消'),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFDC2626),
                  foregroundColor: Colors.white,
                ),
                onPressed: () => Navigator.pop(context, true),
                child: const Text('移除'),
              ),
            ],
          ),
        );
      },
      onDismissed: (_) {
        model.removeFile(file);
      },
      background: Container(
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.only(right: 16),
        decoration: BoxDecoration(
          color: const Color(0xFFDC2626),
          borderRadius: BorderRadius.circular(6),
        ),
        child: const Icon(Icons.delete, color: Colors.white, size: 20),
      ),
      child: ListTile(
        dense: true,
        contentPadding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        leading: Icon(typeIcon, size: 18, color: typeColor),
        title: Row(
          children: [
            Expanded(
              child: Text(
                file.name,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12),
              ),
            ),
            const SizedBox(width: 4),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
              decoration: BoxDecoration(
                color: typeColor.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(3),
                border: Border.all(
                    color: typeColor.withValues(alpha: 0.3), width: 0.5),
              ),
              child: Text(
                typeLabel,
                style: TextStyle(
                  fontSize: 9,
                  fontWeight: FontWeight.w600,
                  color: typeColor,
                ),
              ),
            ),
          ],
        ),
        subtitle: Text(
          status ?? '待添加',
          style: TextStyle(
            fontSize: 10,
            color: status == '处理中...'
                ? AppColors.warning
                : (status != null && status.contains('失败'))
                    ? AppColors.error
                    : AppColors.textHint,
          ),
        ),
        trailing: IconButton(
          icon: const Icon(Icons.add_circle, size: 18, color: AppColors.primary),
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
          onPressed: model.ragService.isReady
              ? () => model.addFileToKnowledgeBase(file.path, file.name)
              : null,
          tooltip: '添加到知识库',
        ),
      ),
    );
  }

  /// 构建知识库文件区域（Sliver）
  ///
  /// 显示已添加到知识库的文件列表，支持滚动和删除。
  Widget _buildKnowledgeFilesSliver(DropFileModel model, List<KnowledgeFile> files) {
    return SliverList(
      delegate: SliverChildListDelegate([
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                '知识库 (${files.length})',
                style: const TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w600,
                  color: AppColors.textSecondary,
                ),
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        if (files.isEmpty)
          Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(Icons.folder_open,
                    size: 40, color: AppColors.textHint.withValues(alpha: 0.5)),
                const SizedBox(height: 8),
                const Text('暂无文件',
                    style: TextStyle(fontSize: 12, color: AppColors.textHint)),
                const SizedBox(height: 4),
                const Text('拖拽文件到右侧添加',
                    style: TextStyle(fontSize: 10, color: AppColors.textHint)),
              ],
            ),
          )
        else
          ...files.map((file) => _buildKnowledgeFileItem(file, model)),
      ]),
    );
  }

  /// 构建单个知识库文件项
  ///
  /// 支持左滑删除（同时从向量库移除）。
  Widget _buildKnowledgeFileItem(KnowledgeFile file, DropFileModel model) {
    final typeIcon = FileHandler.getFileTypeIcon(file.filename);
    final typeColor = FileHandler.getFileTypeColor(file.filename);
    final typeLabel = FileHandler.getFileTypeLabel(file.filename);

    return Dismissible(
      key: Key(file.id),
      direction: DismissDirection.endToStart,
      confirmDismiss: (direction) async {
        return await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('删除文件'),
            content: Text('确定要从知识库中移除 "${file.filename}" 吗？\n该文件的所有向量数据将被删除。'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('取消'),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFDC2626),
                  foregroundColor: Colors.white,
                ),
                onPressed: () => Navigator.pop(context, true),
                child: const Text('删除'),
              ),
            ],
          ),
        );
      },
      onDismissed: (_) {
        model.deleteKnowledgeFile(file.id);
      },
      background: Container(
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.only(right: 16),
        decoration: BoxDecoration(
          color: const Color(0xFFDC2626),
          borderRadius: BorderRadius.circular(6),
        ),
        child: const Icon(Icons.delete, color: Colors.white, size: 20),
      ),
      child: ListTile(
        dense: true,
        contentPadding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        leading: Icon(typeIcon, size: 18, color: typeColor),
        title: Row(
          children: [
            Expanded(
              child: Text(
                file.filename,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12),
              ),
            ),
            const SizedBox(width: 4),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
              decoration: BoxDecoration(
                color: typeColor.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(3),
                border: Border.all(
                    color: typeColor.withValues(alpha: 0.3), width: 0.5),
              ),
              child: Text(
                typeLabel,
                style: TextStyle(
                  fontSize: 9,
                  fontWeight: FontWeight.w600,
                  color: typeColor,
                ),
              ),
            ),
          ],
        ),
        subtitle: Text(
          _formatDate(file.createdAt),
          style: const TextStyle(fontSize: 10, color: AppColors.textHint),
        ),
        trailing: file.inKnowledgeBase
            ? const Icon(Icons.check_circle, size: 14, color: AppColors.success)
            : const Icon(Icons.sync_problem, size: 14, color: AppColors.warning),
      ),
    );
  }
}
