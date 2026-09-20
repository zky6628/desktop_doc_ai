import 'package:flutter/material.dart';

import '../../api/dto/knowledge_dto.dart';

/// 破坏性操作确认对话框（UI 规范 §14：明确动词与二次确认）
///
/// [infoLines] 按操作类型展示必须告知的信息（替换/重建/删除各自的
/// 信息表由调用方传入），确认按钮使用明确动词而非"确定"。
Future<bool> showDestructiveConfirmDialog(
  BuildContext context, {
  required String title,
  required String confirmVerb,
  required List<String> infoLines,
}) async {
  final result = await showDialog<bool>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(title),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final line in infoLines)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Text(line, style: const TextStyle(fontSize: 13)),
            ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, false),
          child: const Text('取消'),
        ),
        ElevatedButton(
          style: ElevatedButton.styleFrom(
            backgroundColor: Theme.of(context).colorScheme.error,
          ),
          onPressed: () => Navigator.pop(context, true),
          child: Text(confirmVerb),
        ),
      ],
    ),
  );
  return result ?? false;
}

/// 重建确认信息（当前版本、服务端配置版本、影响范围）
Future<bool> confirmRebuild(
  BuildContext context, {
  required DocumentSummary document,
}) => showDestructiveConfirmDialog(
      context,
      title: '重建索引',
      confirmVerb: '重建',
      infoLines: [
        '文档：${document.displayName}',
        '当前活动版本：${document.activeVersion?.versionNo ?? '无'}',
        '配置版本：使用服务端启用的解析/切片/嵌入配置',
        '影响范围：新索引验证通过前旧索引继续服务；失败不影响现有检索',
      ],
    );

/// 删除确认信息（软删除、物理清理、历史引用保留、不可恢复提示）
Future<bool> confirmDelete(
  BuildContext context, {
  required String targetName,
}) => showDestructiveConfirmDialog(
      context,
      title: '删除',
      confirmVerb: '删除',
      infoLines: [
        '目标：$targetName',
        '先软删除并创建物理清理任务：源文件、解析产物、切片与派生索引将被清理',
        '历史问答与引用快照按保留策略继续可查',
        '原文件删除后不可恢复',
      ],
    );
