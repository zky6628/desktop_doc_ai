import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../api/dto/knowledge_dto.dart';
import '../api/knowledge_api_client.dart';
import '../controllers/document_detail_controller.dart';
import '../widgets/loading_dots.dart';
import '../widgets/knowledge/confirm_dialogs.dart';

/// 文档详情页：摘要、版本历史、解析块预览与任务历史（UI 规范 §6）
///
/// staging/retired/failed 版本只读展示并标注；解析块文本安全渲染
/// （只输出纯文本与 Markdown 表格，不执行任何 HTML）。
class DocumentDetailPage extends StatefulWidget {
  const DocumentDetailPage({
    super.key,
    required this.knowledgeClient,
    required this.documentId,
  });

  final KnowledgeApiClient knowledgeClient;
  final String documentId;

  @override
  State<DocumentDetailPage> createState() => _DocumentDetailPageState();
}

class _DocumentDetailPageState extends State<DocumentDetailPage> {
  late final DocumentDetailController _controller;

  @override
  void initState() {
    super.initState();
    _controller = DocumentDetailController(
      knowledgeClient: widget.knowledgeClient,
      documentId: widget.documentId,
    );
    _controller.load();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _controller,
      builder: (context, _) {
        final detail = _controller.detail;
        if (_controller.loading && detail == null) {
          return const Center(child: LoadingDots(radius: 6));
        }
        if (_controller.error != null && detail == null) {
          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Text('加载失败：${_controller.error!.message}'),
                const SizedBox(height: 8),
                TextButton(
                  onPressed: _controller.load,
                  child: const Text('重试'),
                ),
              ],
            ),
          );
        }
        if (detail == null) {
          return const Center(child: Text('文档不存在'));
        }
        return ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Row(
              children: [
                BackButton(onPressed: () => context.go('/knowledge-bases')),
                Expanded(
                  child: Text(
                    detail.displayName,
                    style: const TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.w600,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                const SizedBox(width: 8),
                ElevatedButton.icon(
                  onPressed: _controller.mutating ? null : _replaceFile,
                  icon: const Icon(Icons.swap_horiz, size: 18),
                  label: const Text('替换文件'),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: _controller.mutating ? null : _rebuild,
                  icon: const Icon(Icons.build_outlined, size: 18),
                  label: const Text('重建索引'),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: Theme.of(context).colorScheme.error,
                  ),
                  onPressed: _controller.mutating ? null : _delete,
                  icon: const Icon(Icons.delete_outline, size: 18),
                  label: const Text('删除'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 16,
              children: [
                Text('状态：${detail.status}', style: const TextStyle(fontSize: 13)),
                if (detail.activeVersion != null)
                  Text(
                    '活动版本：v${detail.activeVersion!.versionNo}'
                    '（${detail.activeVersion!.status}）',
                    style: const TextStyle(fontSize: 13),
                  ),
                if (detail.activeVersion?.parserMode != null)
                  Text(
                    '解析模式：${detail.activeVersion!.parserMode}',
                    style: const TextStyle(fontSize: 13),
                  ),
                if (detail.activeVersion?.parserProvider != null)
                  Text(
                    '解析器：${detail.activeVersion!.parserProvider}'
                    ' ${detail.activeVersion!.parserVersion ?? ''}',
                    style: const TextStyle(fontSize: 13),
                  ),
              ],
            ),
            if (_controller.error != null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  '操作失败：${_controller.error!.message}',
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            const SizedBox(height: 16),
            _sectionTitle('版本历史'),
            for (final version in _controller.versions)
              ListTile(
                dense: true,
                leading: Icon(
                  version.id == detail.activeVersion?.id
                      ? Icons.verified_outlined
                      : Icons.history,
                  size: 20,
                ),
                title: Text(
                  'v${version.versionNo} · ${version.status}'
                  '${version.id == detail.activeVersion?.id ? '（活动）' : ''}',
                  style: const TextStyle(fontSize: 13),
                ),
                subtitle: Text(
                  [
                    if (version.sizeBytes != null) _formatSize(version.sizeBytes!),
                    if (version.parserProvider != null)
                      '${version.parserProvider} ${version.parserVersion ?? ''}',
                    '创建于 ${version.createdAt}',
                  ].join(' · '),
                  style: const TextStyle(fontSize: 12),
                ),
              ),
            const SizedBox(height: 16),
            _sectionTitle('解析块预览（前 200 块）'),
            if (_controller.blocks.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: Text('暂无解析产物（等待解析完成）',
                    style: TextStyle(fontSize: 13)),
              )
            else
              for (final block in _controller.blocks)
                Container(
                  margin: const EdgeInsets.only(bottom: 8),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.surface,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '#${block.ordinal}'
                        '${block.pageNo != null ? ' · 第 ${block.pageNo} 页' : ''}'
                        '${block.sectionPath.isEmpty ? '' : ' · ${block.sectionPath}'}'
                        ' · ${block.blockType}',
                        style: const TextStyle(fontSize: 11),
                      ),
                      const SizedBox(height: 4),
                      SelectableText(
                        block.table != null
                            ? block.table!.rawMarkdown
                            : block.contentText,
                        style: const TextStyle(fontSize: 13),
                      ),
                    ],
                  ),
                ),
            const SizedBox(height: 16),
            _sectionTitle('任务历史（最近 5 条）'),
            if (detail.recentTasks.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: Text('暂无任务', style: TextStyle(fontSize: 13)),
              )
            else
              for (final task in detail.recentTasks)
                ListTile(
                  dense: true,
                  leading: Icon(
                    task.state.isTerminal
                        ? (task.state == TaskWireStatus.succeeded
                            ? Icons.check_circle_outline
                            : Icons.error_outline)
                        : Icons.pending_outlined,
                    size: 20,
                  ),
                  title: Text(
                    '${task.taskType} · ${task.state.wireName}'
                    '${task.stage == null ? '' : ' · ${task.stage}'}',
                    style: const TextStyle(fontSize: 13),
                  ),
                  subtitle: task.error == null
                      ? Text('创建于 ${task.createdAt}',
                          style: const TextStyle(fontSize: 12))
                      : Text(
                          '${task.error!.code}: ${task.error!.message}',
                          style: const TextStyle(fontSize: 12),
                        ),
                ),
          ],
        );
      },
    );
  }

  Widget _sectionTitle(String title) => Text(
        title,
        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
      );

  Future<void> _replaceFile() async {
    final file = await FilePicker.pickFile();
    final path = file?.path;
    final name = file?.name;
    if (path == null || name == null) return;
    if (!mounted) return;
    final detail = _controller.detail;
    final confirmed = await showDestructiveConfirmDialog(
      context,
      title: '替换文件',
      confirmVerb: '替换',
      infoLines: [
        '文档：${detail?.displayName ?? name}',
        '旧版本仍可用（版本历史保留，可回看）',
        '新文件：$name',
        '新版本完成索引验证后自动激活；期间旧活动版本继续服务',
      ],
    );
    if (!confirmed) return;
    await _controller.replaceFile(UploadFileInput(displayName: name, path: path));
  }

  Future<void> _rebuild() async {
    final detail = _controller.detail;
    if (detail == null) return;
    final confirmed = await showDestructiveConfirmDialog(
      context,
      title: '重建索引',
      confirmVerb: '重建',
      infoLines: [
        '当前活动版本：v${detail.activeVersion?.versionNo ?? '无'}',
        '配置版本：使用服务端启用的解析/切片/嵌入配置',
        '影响范围：新索引验证通过前旧索引继续服务；失败不影响现有检索',
      ],
    );
    if (confirmed) await _controller.rebuildIndex();
  }

  Future<void> _delete() async {
    final detail = _controller.detail;
    if (detail == null) return;
    final confirmed = await confirmDelete(
      context,
      targetName: detail.displayName,
    );
    if (!confirmed) return;
    final ok = await _controller.deleteDocument();
    // 删除成功即返回列表（列表路由感知刷新，删除项不再显示）
    if (ok && mounted) context.go('/knowledge-bases');
  }
}

String _formatSize(int bytes) {
  if (bytes >= 1024 * 1024) {
    return '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
  }
  if (bytes >= 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  return '$bytes B';
}
