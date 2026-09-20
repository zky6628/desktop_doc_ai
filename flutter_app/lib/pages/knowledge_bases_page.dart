import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../api/dto/knowledge_dto.dart';
import '../api/knowledge_api_client.dart';
import '../app/app_preferences.dart';
import '../controllers/app_shell_controller.dart';
import '../controllers/knowledge_base_controller.dart';
import '../widgets/loading_dots.dart';
import '../widgets/knowledge/confirm_dialogs.dart';
import '../widgets/knowledge/mineru_confirm_dialog.dart';
import '../widgets/knowledge/upload_dialog.dart';

/// 知识库页：左侧知识库列表（选择/创建/重命名/删除），右侧文档表格
/// （上传/重建/删除/健康检查/云端确认横幅）
///
/// 数据以 API 为权威；操作在 API 接受后才更新本地列表，不伪造成功。
class KnowledgeBasesPage extends StatefulWidget {
  const KnowledgeBasesPage({
    super.key,
    required this.knowledgeClient,
    required this.preferences,
  });

  final KnowledgeApiClient knowledgeClient;
  final AppPreferences preferences;

  @override
  State<KnowledgeBasesPage> createState() => _KnowledgeBasesPageState();
}

class _KnowledgeBasesPageState extends State<KnowledgeBasesPage> {
  late final KnowledgeBaseController _controller;
  GoRouter? _router;

  @override
  void initState() {
    super.initState();
    _controller = KnowledgeBaseController(
      knowledgeClient: widget.knowledgeClient,
      preferences: widget.preferences,
      onCurrentKnowledgeBase: (name) {
        final shell = context.read<AppShellController>();
        shell.setCurrentKnowledgeBase(name);
      },
    );
    _controller.addListener(_onControllerChanged);
    _controller.loadInitial();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // 路由感知刷新：从文档详情返回列表时重新拉取（IndexedStack
    // 分支保活，页面不会重建，需自行感知回列表时机）
    final router = GoRouter.of(context);
    if (!identical(router, _router)) {
      _router?.routerDelegate.removeListener(_onRouteChanged);
      _router = router;
      router.routerDelegate.addListener(_onRouteChanged);
    }
  }

  /// 路由回到列表页时刷新文档与确认待办
  void _onRouteChanged() {
    final location =
        _router?.routerDelegate.currentConfiguration.uri.toString() ?? '';
    if (mounted &&
        location == '/knowledge-bases' &&
        _controller.currentKb != null) {
      _controller.reloadDocuments();
      _controller.reloadPendingConfirmations();
    }
  }

  /// 控制器错误出现时弹一次提示（SnackBar），不阻塞列表展示
  String? _lastErrorText;

  void _onControllerChanged() {
    final message = _controller.error?.message;
    if (message != null && message != _lastErrorText && mounted) {
      _lastErrorText = message;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(message, maxLines: 3, overflow: TextOverflow.ellipsis),
          behavior: SnackBarBehavior.floating,
          duration: const Duration(seconds: 4),
        ),
      );
    } else if (message == null) {
      _lastErrorText = null;
    }
  }

  @override
  void dispose() {
    _router?.routerDelegate.removeListener(_onRouteChanged);
    _controller.removeListener(_onControllerChanged);
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _controller,
      builder: (context, _) {
        final kb = _controller.currentKb;
        return Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _buildKbPanel(context),
            const VerticalDivider(thickness: 1, width: 1),
            Expanded(
              child: kb == null
                  ? _buildEmpty(context, Icons.library_books_outlined,
                      '选择或创建一个知识库开始管理文档')
                  : _buildDocumentArea(context, kb),
            ),
          ],
        );
      },
    );
  }

  // ===================== 知识库列表 =====================

  Widget _buildKbPanel(BuildContext context) {
    final controller = _controller;
    return SizedBox(
      width: 240,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 8, 4),
            child: Row(
              children: [
                const Expanded(
                  child: Text('知识库', style: TextStyle(fontWeight: FontWeight.w600)),
                ),
                IconButton(
                  tooltip: '新建知识库',
                  icon: const Icon(Icons.add_circle_outline, size: 20),
                  onPressed: controller.mutating ? null : _createDialog,
                ),
              ],
            ),
          ),
          Expanded(
            child: controller.kbsLoading && controller.knowledgeBases.isEmpty
                ? const Center(child: LoadingDots(radius: 6))
                : controller.knowledgeBases.isEmpty
                    ? const Center(
                        child: Text('暂无知识库', style: TextStyle(fontSize: 13)),
                      )
                    : ListView.builder(
                        itemCount: controller.knowledgeBases.length,
                        itemBuilder: (context, index) {
                          final kb = controller.knowledgeBases[index];
                          final selected = controller.currentKb?.id == kb.id;
                          return ListTile(
                            dense: true,
                            selected: selected,
                            leading: const Icon(Icons.library_books_outlined,
                                size: 20),
                            title: Text(kb.name, overflow: TextOverflow.ellipsis),
                            trailing: PopupMenuButton<String>(
                              tooltip: '知识库操作',
                              icon: const Icon(Icons.more_vert, size: 18),
                              onSelected: (action) {
                                if (action == 'rename') _renameDialog(kb);
                                if (action == 'delete') _deleteKbDialog(kb);
                              },
                              itemBuilder: (context) => const [
                                PopupMenuItem(
                                  value: 'rename',
                                  child: Text('重命名'),
                                ),
                                PopupMenuItem(
                                  value: 'delete',
                                  child: Text('删除'),
                                ),
                              ],
                            ),
                            onTap: () => controller.selectKnowledgeBase(kb),
                          );
                        },
                      ),
          ),
        ],
      ),
    );
  }

  // ===================== 文档区 =====================

  Widget _buildDocumentArea(BuildContext context, KbDto kb) {
    final controller = _controller;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 8),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  kb.name,
                  style: const TextStyle(
                      fontSize: 16, fontWeight: FontWeight.w600),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (controller.pendingConfirmations.isNotEmpty)
                Flexible(
                  child: Tooltip(
                    message: '有文件等待云端解析确认，点击处理',
                    child: TextButton.icon(
                      style: TextButton.styleFrom(
                        padding: const EdgeInsets.symmetric(horizontal: 8),
                        visualDensity: VisualDensity.compact,
                      ),
                      onPressed: _openMineruConfirm,
                      icon: Icon(
                        Icons.cloud_sync_outlined,
                        size: 18,
                        color: Theme.of(context).colorScheme.primary,
                      ),
                      label: Text(
                        '${controller.pendingConfirmations.length} 待确认',
                        overflow: TextOverflow.ellipsis,
                        maxLines: 1,
                      ),
                    ),
                  ),
                ),
              const SizedBox(width: 8),
              Tooltip(
                message: '健康检查（创建检查任务，进度见任务中心）',
                child: IconButton(
                  icon: const Icon(Icons.health_and_safety_outlined, size: 20),
                  onPressed: controller.mutating
                      ? null
                      : () async {
                          final ok = await controller.startHealthCheck();
                          if (!mounted) return;
                          if (ok) {
                            ScaffoldMessenger.of(this.context).showSnackBar(
                              const SnackBar(
                                content: Text('健康检查任务已创建'),
                                behavior: SnackBarBehavior.floating,
                              ),
                            );
                          }
                        },
                ),
              ),
              Tooltip(
                message: '刷新',
                child: IconButton(
                  icon: const Icon(Icons.refresh, size: 20),
                  onPressed: controller.mutating
                      ? null
                      : () {
                          controller.reloadKnowledgeBases();
                          controller.reloadDocuments();
                          controller.reloadPendingConfirmations();
                        },
                ),
              ),
              const SizedBox(width: 8),
              ElevatedButton.icon(
                onPressed:
                    controller.mutating ? null : () => _uploadDialog(context),
                icon: const Icon(Icons.upload_outlined, size: 18),
                label: const Text('上传文档'),
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: controller.documentsLoading && controller.documents.isEmpty
              ? const Center(child: LoadingDots(radius: 6))
              : controller.documents.isEmpty
                  ? _buildEmpty(context, Icons.note_add_outlined,
                      '暂无文档，点击右上角「上传文档」导入文件')
                  : _buildDocumentList(context, controller),
        ),
      ],
    );
  }

  Widget _buildDocumentList(
    BuildContext context,
    KnowledgeBaseController controller,
  ) {
    final documents = controller.documents;
    return Column(
      children: [
        // 表头：列宽与数据行严格对齐
        const Padding(
          padding: EdgeInsets.symmetric(horizontal: 20, vertical: 8),
          child: Row(
            children: [
              SizedBox(width: 30),
              Expanded(
                flex: 4,
                child: Text('文件名', style: _headerStyle),
              ),
              SizedBox(
                width: 150,
                child: Text('状态', style: _headerStyle),
              ),
              SizedBox(
                width: 80,
                child: Text('版本', style: _headerStyle),
              ),
              SizedBox(
                width: 80,
                child: Text('解析块数', style: _headerStyle),
              ),
              Expanded(
                flex: 3,
                child: Text('最近任务', style: _headerStyle),
              ),
              SizedBox(width: 42),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: ListView.builder(
            itemCount: documents.length + (controller.documentsHasMore ? 1 : 0),
            itemBuilder: (context, index) {
              if (index == documents.length) {
                return TextButton(
                  onPressed: controller.documentsLoading
                      ? null
                      : controller.loadMoreDocuments,
                  child: const Text('加载更多'),
                );
              }
              return _buildDocumentRow(context, controller, documents[index]);
            },
          ),
        ),
      ],
    );
  }

  static const _headerStyle = TextStyle(
    fontSize: 12,
    fontWeight: FontWeight.w600,
  );

  Widget _buildDocumentRow(
    BuildContext context,
    KnowledgeBaseController controller,
    DocumentSummary document,
  ) {
    final hasPending = document.latestTask != null &&
        !document.latestTask!.state.isTerminal;
    return InkWell(
      onTap: () => context.go(
        '/knowledge-bases/${document.knowledgeBaseId}/documents/${document.id}',
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
        child: Row(
          children: [
            const Icon(Icons.insert_drive_file_outlined, size: 20),
            const SizedBox(width: 10),
            Expanded(
              flex: 4,
              child: Text(
                document.displayName,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 14),
              ),
            ),
            SizedBox(
              width: 150,
              child: _statusChip(context, document),
            ),
            SizedBox(
              width: 80,
              child: Text(
                document.activeVersion == null
                    ? '-'
                    : 'v${document.activeVersion!.versionNo}',
                style: const TextStyle(fontSize: 13),
              ),
            ),
            SizedBox(
              width: 80,
              child: Text(
                document.chunkCount?.toString() ?? '-',
                style: const TextStyle(fontSize: 13),
              ),
            ),
            Expanded(
              flex: 3,
              child: hasPending
                  ? Text(
                      '${document.latestTask!.state.wireName}'
                      '${document.latestTask!.stage == null ? '' : ' · ${document.latestTask!.stage}'}',
                      style: const TextStyle(fontSize: 12),
                      overflow: TextOverflow.ellipsis,
                    )
                  : const Text('-', style: TextStyle(fontSize: 12)),
            ),
            const SizedBox(width: 8),
            PopupMenuButton<String>(
              tooltip: '文档操作',
              icon: const Icon(Icons.more_vert, size: 18),
              onSelected: (action) =>
                  unawaited(_documentAction(action, document)),
              itemBuilder: (context) => [
                const PopupMenuItem(value: 'rebuild', child: Text('重建索引')),
                const PopupMenuItem(value: 'delete', child: Text('删除')),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _documentAction(String action, DocumentSummary document) async {
    if (action == 'rebuild') {
      await _rebuildDocumentFlow(document);
    }
    if (action == 'delete') {
      await _deleteDocumentFlow(document);
    }
  }

  Future<void> _rebuildDocumentFlow(DocumentSummary document) async {
    if (!mounted) return;
    final confirmed = await confirmRebuild(context, document: document);
    if (!mounted) return;
    if (confirmed) await _controller.rebuildDocument(document);
  }

  Future<void> _deleteDocumentFlow(DocumentSummary document) async {
    if (!mounted) return;
    final confirmed = await confirmDelete(
      context,
      targetName: document.displayName,
    );
    if (!mounted) return;
    if (confirmed) await _controller.deleteDocument(document);
  }

  Widget _statusChip(BuildContext context, DocumentSummary document) {
    final status = document.status;
    final (label, color) = switch (status) {
      'ready' => ('就绪', const Color(0xFF22C55E)),
      'failed' => ('失败', const Color(0xFFEF4444)),
      'deleted' => ('已删除', Colors.grey),
      'queued' || 'processing' || 'updating' => ('处理中', const Color(0xFFF59E0B)),
      _ => (status, Colors.grey),
    };
    return Row(
      children: [
        Icon(Icons.circle, size: 8, color: color),
        const SizedBox(width: 6),
        Text(label, style: const TextStyle(fontSize: 13)),
      ],
    );
  }

  Widget _buildEmpty(BuildContext context, IconData icon, String message) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            icon,
            size: 56,
            color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.25),
          ),
          const SizedBox(height: 12),
          Text(message, style: const TextStyle(fontSize: 14)),
        ],
      ),
    );
  }

  // ===================== 对话框 =====================

  Future<void> _createDialog() async {
    final nameController = TextEditingController();
    final descController = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('新建知识库'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: nameController,
              autofocus: true,
              decoration: const InputDecoration(labelText: '名称'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: descController,
              decoration: const InputDecoration(labelText: '描述（可选）'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('创建'),
          ),
        ],
      ),
    );
    nameController.dispose();
    descController.dispose();
    if (ok == true && nameController.text.trim().isNotEmpty) {
      await _controller.createKnowledgeBase(
        nameController.text.trim(),
        descController.text.trim().isEmpty ? null : descController.text.trim(),
      );
    }
  }

  Future<void> _renameDialog(KbDto kb) async {
    final nameController = TextEditingController(text: kb.name);
    final descController = TextEditingController(text: kb.description);
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('重命名知识库'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: nameController,
              autofocus: true,
              decoration: const InputDecoration(labelText: '名称'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: descController,
              decoration: const InputDecoration(labelText: '描述'),
            ),
          ],
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
    if (ok == true && nameController.text.trim().isNotEmpty) {
      await _controller.renameCurrentKnowledgeBase(
        nameController.text.trim(),
        descController.text.trim(),
      );
    }
  }

  Future<void> _deleteKbDialog(KbDto kb) async {
    final confirmed = await confirmDelete(context, targetName: kb.name);
    if (!confirmed) return;
    if (_controller.currentKb?.id == kb.id) {
      await _controller.deleteCurrentKnowledgeBase();
      return;
    }
    // 非选中知识库删除后仅刷新列表
    await _controller.reloadKnowledgeBases();
  }

  Future<void> _uploadDialog(BuildContext context) async {
    await showDialog<void>(
      context: context,
      builder: (context) => UploadDialog(
        currentKbName: _controller.currentKb?.name ?? '',
        onSubmit: (
          files, {
          required parserPreference,
          required duplicatePolicy,
        }) =>
            _controller.uploadDocuments(
          files,
          parserPreference: parserPreference,
          duplicatePolicy: duplicatePolicy,
        ),
      ),
    );
  }

  Future<void> _openMineruConfirm() async {
    await showDialog<void>(
      context: context,
      builder: (context) => MineruConfirmDialog(
        tasks: _controller.pendingConfirmations,
        onResolve: (task, {required approve}) =>
            _controller.resolveConfirmation(task, approve: approve),
      ),
    );
    _controller.reloadPendingConfirmations();
  }
}
