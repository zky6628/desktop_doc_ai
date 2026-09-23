import 'package:flutter/material.dart';

import '../../api/dto/conversation_dto.dart';
import '../../theme/colors.dart';
import '../knowledge/confirm_dialogs.dart';

/// 历史会话批量管理对话框：多选删除与全部清空
///
/// 多选删除逐条经 [onDeleteSelected] 调既有单删端点（返回实际删除的
/// ID 集合），全部清空经 [onDeleteAll] 调批量端点；两者均要求显式
/// 确认。删除结果同步到弹窗内剩余列表，宿主列表由回调实现刷新。
class ConversationManageDialog extends StatefulWidget {
  const ConversationManageDialog({
    super.key,
    required this.conversations,
    required this.onDeleteSelected,
    required this.onDeleteAll,
  });

  /// 进入对话框时的会话快照（弹窗内自行维护剩余列表）
  final List<ConversationSummaryDto> conversations;

  /// 删除选中的会话，返回实际删除成功的 ID 集合
  final Future<Set<String>> Function(Set<String> ids) onDeleteSelected;

  /// 清空全部会话，返回是否成功
  final Future<bool> Function() onDeleteAll;

  @override
  State<ConversationManageDialog> createState() =>
      _ConversationManageDialogState();
}

class _ConversationManageDialogState extends State<ConversationManageDialog> {
  final Set<String> _selected = {};
  late List<ConversationSummaryDto> _remaining;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _remaining = List.of(widget.conversations);
  }

  bool get _allSelected =>
      _remaining.isNotEmpty && _selected.length == _remaining.length;

  void _toggleAll(bool? checked) {
    setState(() {
      if (checked == true) {
        _selected.addAll(_remaining.map((item) => item.id));
      } else {
        _selected.clear();
      }
    });
  }

  void _toggleOne(String id, bool? checked) {
    setState(() {
      if (checked == true) {
        _selected.add(id);
      } else {
        _selected.remove(id);
      }
    });
  }

  Future<void> _deleteSelected() async {
    if (_selected.isEmpty || _busy) return;
    final confirmed = await showDestructiveConfirmDialog(
      context,
      title: '确认删除',
      confirmVerb: '删除',
      infoLines: [
        '删除选中的 ${_selected.length} 个会话？',
        '会话消息与引用将被删除，查询指标事实保留。此操作不可恢复。',
      ],
    );
    if (!confirmed) return;
    setState(() => _busy = true);
    final deleted = await widget.onDeleteSelected(Set.of(_selected));
    if (!mounted) return;
    setState(() {
      _remaining.removeWhere((item) => deleted.contains(item.id));
      _selected.removeAll(deleted);
      _busy = false;
    });
  }

  Future<void> _deleteAll() async {
    if (_remaining.isEmpty || _busy) return;
    final confirmed = await showDestructiveConfirmDialog(
      context,
      title: '确认删除',
      confirmVerb: '删除',
      infoLines: [
        '清空全部 ${_remaining.length} 个会话？',
        '会话消息与引用将被删除，查询指标事实保留。此操作不可恢复。',
      ],
    );
    if (!confirmed) return;
    setState(() => _busy = true);
    final ok = await widget.onDeleteAll();
    if (!mounted) return;
    if (ok) {
      Navigator.pop(context);
      return;
    }
    setState(() => _busy = false);
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text('清空失败，请检查服务连接后重试'),
        behavior: SnackBarBehavior.floating,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Dialog(
      child: SizedBox(
        width: 460,
        height: 540,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 16, 12, 0),
              child: Row(
                children: [
                  const Expanded(
                    child: Text(
                      '管理历史会话',
                      style: TextStyle(fontSize: 17, fontWeight: FontWeight.w600),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.close, size: 20),
                    onPressed: _busy ? null : () => Navigator.pop(context),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: Row(
                children: [
                  Checkbox(
                    value: _allSelected,
                    tristate: false,
                    onChanged: _busy ? null : _toggleAll,
                  ),
                  const Text('全选', style: TextStyle(fontSize: 13)),
                  const Spacer(),
                  Text(
                    '已选 ${_selected.length} / ${_remaining.length}',
                    style: TextStyle(
                      fontSize: 12,
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            Expanded(
              child: _remaining.isEmpty
                  ? const Center(child: Text('没有可管理的会话'))
                  : ListView.builder(
                      itemCount: _remaining.length,
                      itemBuilder: (context, index) {
                        final conversation = _remaining[index];
                        return CheckboxListTile(
                          value: _selected.contains(conversation.id),
                          onChanged: _busy
                              ? null
                              : (checked) =>
                                    _toggleOne(conversation.id, checked),
                          dense: true,
                          controlAffinity: ListTileControlAffinity.leading,
                          title: Text(
                            conversation.displayTitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(fontSize: 13),
                          ),
                          // 摘要行作为标题下的第二行；不可用 secondary：
                          // 在 controlAffinity 为 leading 时它落到 trailing
                          // 位置，不限宽的文本会占满整条 tile 宽度
                          subtitle: conversation.lastMessage == null
                              ? null
                              : Text(
                                  conversation.lastMessage!.contentExcerpt,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    fontSize: 11,
                                    color: theme.colorScheme.onSurfaceVariant,
                                  ),
                                ),
                        );
                      },
                    ),
            ),
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              child: Row(
                children: [
                  OutlinedButton.icon(
                    onPressed:
                        _remaining.isEmpty || _busy ? null : _deleteAll,
                    style: OutlinedButton.styleFrom(
                      foregroundColor: AppColors.error,
                      side: const BorderSide(color: AppColors.error),
                    ),
                    icon: const Icon(Icons.delete_sweep_outlined, size: 18),
                    label: const Text('清空全部'),
                  ),
                  const Spacer(),
                  FilledButton.icon(
                    onPressed: _selected.isEmpty || _busy
                        ? null
                        : _deleteSelected,
                    style: FilledButton.styleFrom(
                      backgroundColor: AppColors.error,
                      foregroundColor: Colors.white,
                    ),
                    icon: const Icon(Icons.delete_outline, size: 18),
                    label: Text(
                      _selected.isEmpty ? '删除选中' : '删除选中(${_selected.length})',
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
