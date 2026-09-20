import 'package:flutter/material.dart';

import '../../api/dto/knowledge_dto.dart';

/// MinerU 云端解析确认对话框（UI 规范 §7）
///
/// 只展示 state=waiting_user 的任务：每文件显示文件名、大小、路由
/// 原因与隐私提示，支持逐文件批准/拒绝与全部批准/全部拒绝；批准只
/// 影响对应任务。确认后待办与文档列表由调用方刷新。
class MineruConfirmDialog extends StatefulWidget {
  const MineruConfirmDialog({
    super.key,
    required this.tasks,
    required this.onResolve,
  });

  final List<TaskDto> tasks;

  /// 返回是否全部处理成功（部分失败时保持对话框打开由用户重试）
  final Future<bool> Function(TaskDto task, {required bool approve}) onResolve;

  @override
  State<MineruConfirmDialog> createState() => _MineruConfirmDialogState();
}

class _MineruConfirmDialogState extends State<MineruConfirmDialog> {
  final Set<String> _resolvedIds = {};
  bool _batchWorking = false;
  String? _error;

  List<TaskDto> get _remaining =>
      widget.tasks.where((task) => !_resolvedIds.contains(task.id)).toList();

  Future<void> _resolve(TaskDto task, bool approve) async {
    final ok = await widget.onResolve(task, approve: approve);
    if (!mounted) return;
    setState(() {
      if (ok) {
        _resolvedIds.add(task.id);
        _error = null;
      } else {
        _error = '操作失败，请重试';
      }
    });
  }

  Future<void> _resolveAll(bool approve) async {
    setState(() => _batchWorking = true);
    for (final task in List.of(_remaining)) {
      await _resolve(task, approve);
    }
    if (mounted) setState(() => _batchWorking = false);
  }

  @override
  Widget build(BuildContext context) {
    final remaining = _remaining;
    return AlertDialog(
      title: const Text('云端解析确认'),
      content: SizedBox(
        width: 560,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.surface,
                borderRadius: BorderRadius.circular(8),
              ),
              child: const Text(
                '以下文件建议使用 MinerU 云端解析。批准后文件将上传至'
                '服务商 MinerU 处理（仅解析用途）；拒绝则取消对应任务，'
                '文件不会上传。批准或拒绝只影响对应文件。',
                style: TextStyle(fontSize: 12),
              ),
            ),
            const SizedBox(height: 8),
            Flexible(
              child: remaining.isEmpty
                  ? const Padding(
                      padding: EdgeInsets.symmetric(vertical: 16),
                      child: Center(child: Text('已全部处理完成')),
                    )
                  : ListView.builder(
                      shrinkWrap: true,
                      itemCount: remaining.length,
                      itemBuilder: (context, index) =>
                          _buildRow(context, remaining[index]),
                    ),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  _error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: remaining.isEmpty || _batchWorking
              ? null
              : () => _resolveAll(false),
          child: const Text('全部拒绝'),
        ),
        ElevatedButton(
          onPressed: remaining.isEmpty || _batchWorking
              ? null
              : () => _resolveAll(true),
          child: const Text('全部批准'),
        ),
      ],
    );
  }

  Widget _buildRow(BuildContext context, TaskDto task) {
    final size = task.documentSizeBytes;
    return ListTile(
      dense: true,
      leading: const Icon(Icons.cloud_upload_outlined),
      title: Text(
        task.documentDisplayName ?? task.id,
        overflow: TextOverflow.ellipsis,
      ),
      subtitle: Text(
        [
          if (size != null) _formatSize(size),
          if (task.routeReason != null) '路由原因：${task.routeReason}',
        ].join(' · '),
        style: const TextStyle(fontSize: 12),
        overflow: TextOverflow.ellipsis,
      ),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextButton(
            onPressed: () => _resolve(task, false),
            child: const Text('拒绝'),
          ),
          ElevatedButton(
            onPressed: () => _resolve(task, true),
            child: const Text('批准'),
          ),
        ],
      ),
    );
  }
}

String _formatSize(int bytes) {
  if (bytes >= 1024 * 1024) return '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
  if (bytes >= 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  return '$bytes B';
}
