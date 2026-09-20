import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

import '../../api/dto/knowledge_dto.dart';
import '../../api/knowledge_api_client.dart';

/// v1 受支持格式（05 合同 §5.1；与旧链路白名单互不影响）
const _supportedExtensions = ['.txt', '.md', '.markdown', '.docx', '.pdf'];

String _extensionOf(String fileName) {
  final dotIndex = fileName.lastIndexOf('.');
  if (dotIndex == -1) return '';
  return fileName.substring(dotIndex).toLowerCase();
}

/// 单个待上传文件的本地校验结果（上传前的客户端预检）
class _PendingFile {
  _PendingFile(this.name, this.path, this.sizeBytes);

  final String name;
  final String path;
  final int sizeBytes;

  String? get problem {
    if (!_supportedExtensions.contains(_extensionOf(name))) return '不支持的格式';
    if (sizeBytes > _maxBytes) return '超过 100MB 上限';
    return null;
  }

  static const _maxBytes = 100 * 1024 * 1024;
}

/// 上传对话框：文件选择 → 本地检查清单 → 提交 → 逐文件结果
///
/// 提交后的逐文件校验结果、建议解析器与原因来自服务端路由（权威
/// 判定）；本地清单只做扩展名/大小/数量预检。
class UploadDialog extends StatefulWidget {
  const UploadDialog({
    super.key,
    required this.onSubmit,
    required this.currentKbName,
  });

  /// 提交上传；返回逐文件结果（null 表示调用失败）
  final Future<List<UploadFileResult>?> Function(
    List<UploadFileInput> files, {
    required String parserPreference,
    required String duplicatePolicy,
  })
  onSubmit;

  final String currentKbName;

  @override
  State<UploadDialog> createState() => _UploadDialogState();
}

class _UploadDialogState extends State<UploadDialog> {
  final List<_PendingFile> _files = [];
  String _parserPreference = 'auto';
  String _duplicatePolicy = 'skip';
  bool _submitting = false;
  List<UploadFileResult>? _results;
  String? _error;

  Future<void> _pickFiles() async {
    // 取消选择返回空列表
    final picked = await FilePicker.pickFiles();
    if (picked.isEmpty) return;
    setState(() {
      for (final file in picked) {
        if (file.path == null) continue;
        // 单批上限 50 个：超出部分直接忽略并在清单标注
        if (_files.length >= 50) break;
        _files.add(
          _PendingFile(file.name, file.path!, file.lengthSync() ?? 0),
        );
      }
      _results = null;
    });
  }

  Future<void> _submit() async {
    final usable = _files.where((file) => file.problem == null).toList();
    if (usable.isEmpty) return;
    setState(() => _submitting = true);
    try {
      final results = await widget.onSubmit(
        [
          for (final file in usable)
            UploadFileInput(displayName: file.name, path: file.path),
        ],
        parserPreference: _parserPreference,
        duplicatePolicy: _duplicatePolicy,
      );
      setState(() {
        _results = results;
        _error = results == null ? '上传请求失败' : null;
      });
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text('上传文档到「${widget.currentKbName}」'),
      content: SizedBox(
        width: 560,
        child: _results == null ? _buildPending() : _buildResults(),
      ),
      actions: _buildActions(),
    );
  }

  Widget _buildPending() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            ElevatedButton.icon(
              onPressed: _pickFiles,
              icon: const Icon(Icons.file_open_outlined, size: 18),
              label: const Text('选择文件'),
            ),
            const SizedBox(width: 12),
            Text('${_files.length} / 50 个文件',
                style: const TextStyle(fontSize: 13)),
          ],
        ),
        const SizedBox(height: 8),
        Flexible(
          child: _files.isEmpty
              ? const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: Text('尚未选择文件（支持 txt/md/markdown/docx/pdf）')),
                )
              : ListView.builder(
                  shrinkWrap: true,
                  itemCount: _files.length,
                  itemBuilder: (context, index) {
                    final file = _files[index];
                    return ListTile(
                      dense: true,
                      leading: const Icon(Icons.insert_drive_file_outlined),
                      title: Text(file.name, overflow: TextOverflow.ellipsis),
                      subtitle: Text(_formatSize(file.sizeBytes),
                          style: const TextStyle(fontSize: 12)),
                      trailing: file.problem == null
                          ? IconButton(
                              tooltip: '移除',
                              icon: const Icon(Icons.close, size: 18),
                              onPressed: () =>
                                  setState(() => _files.removeAt(index)),
                            )
                          : Text(file.problem!,
                              style: TextStyle(
                                fontSize: 12,
                                color: Theme.of(context).colorScheme.error,
                              )),
                    );
                  },
                ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            _dropdown<String>(
              value: _parserPreference,
              label: '解析偏好',
              items: const {
                'auto': '自动路由',
                'local': '仅本地解析',
                'mineru': '云端解析（需确认）',
              },
              onChanged: (value) => setState(() => _parserPreference = value!),
            ),
            const SizedBox(width: 16),
            _dropdown<String>(
              value: _duplicatePolicy,
              label: '重复内容',
              items: const {'skip': '跳过', 'new_version': '建立新版本'},
              onChanged: (value) => setState(() => _duplicatePolicy = value!),
            ),
          ],
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              _error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ),
      ],
    );
  }

  Widget _buildResults() {
    final results = _results!;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text('上传完成：${results.where((r) => r.accepted).length} 接受 / '
            '${results.where((r) => !r.accepted).length} 拒绝'),
        const SizedBox(height: 8),
        Flexible(
          child: ListView.builder(
            shrinkWrap: true,
            itemCount: results.length,
            itemBuilder: (context, index) {
              final result = results[index];
              if (result.accepted) {
                final mode = result.routeMode == 'cloud' ? '云端解析' : '本地解析';
                return ListTile(
                  dense: true,
                  leading: const Icon(Icons.check_circle,
                      color: Color(0xFF22C55E), size: 20),
                  title:
                      Text(result.displayName, overflow: TextOverflow.ellipsis),
                  subtitle: Text(
                    '$mode · ${result.routeReason ?? '已创建解析任务'}',
                    style: const TextStyle(fontSize: 12),
                  ),
                );
              }
              return ListTile(
                dense: true,
                leading: const Icon(Icons.cancel,
                    color: Color(0xFFEF4444), size: 20),
                title: Text(result.displayName, overflow: TextOverflow.ellipsis),
                subtitle: Text(
                  '${result.errorCode ?? 'REJECTED'}: '
                  '${result.errorMessage ?? '被拒绝'}',
                  style: const TextStyle(fontSize: 12),
                ),
              );
            },
          ),
        ),
      ],
    );
  }

  List<Widget> _buildActions() {
    if (_results != null) {
      return [
        ElevatedButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('完成'),
        ),
      ];
    }
    final usableCount =
        _files.where((file) => file.problem == null).length;
    return [
      TextButton(
        onPressed: _submitting ? null : () => Navigator.pop(context),
        child: const Text('取消'),
      ),
      ElevatedButton.icon(
        onPressed:
            _submitting || usableCount == 0 ? null : _submit,
        icon: _submitting
            ? const SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Icon(Icons.upload_outlined, size: 18),
        label: Text(_submitting ? '上传中' : '上传 $usableCount 个文件'),
      ),
    ];
  }

  Widget _dropdown<T>({
    required T value,
    required String label,
    required Map<T, String> items,
    required ValueChanged<T?> onChanged,
  }) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(label, style: const TextStyle(fontSize: 13)),
        const SizedBox(width: 6),
        DropdownButton<T>(
          value: value,
          isDense: true,
          items: [
            for (final entry in items.entries)
              DropdownMenuItem<T>(value: entry.key, child: Text(entry.value)),
          ],
          onChanged: onChanged,
        ),
      ],
    );
  }
}

String _formatSize(int bytes) {
  if (bytes >= 1024 * 1024) return '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
  if (bytes >= 1024) return '${(bytes / 1024).toStringAsFixed(1)} KB';
  return '$bytes B';
}
