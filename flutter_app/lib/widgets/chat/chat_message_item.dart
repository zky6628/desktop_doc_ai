import 'package:flutter/material.dart';

import '../../api/dto/query_dto.dart';
import '../../controllers/chat_controller.dart';
import '../../theme/colors.dart';

/// [S编号] 引用编号的正则（与生成合同的引用标记一致）
final RegExp _citationMarker = RegExp(r'\[S(\d+)\]');

/// 单条消息气泡：角色布局、引用编号内联片段与状态横幅
///
/// 回答中的 [S编号] 渲染为可点击片段，点击打开引用抽屉定位对应
/// 引用卡；回答正文可选中复制（无障碍要求）。
class ChatMessageItem extends StatelessWidget {
  const ChatMessageItem({
    super.key,
    required this.message,
    required this.onCitationTap,
  });

  final ChatMessageView message;

  /// 点击 [S编号]（参数为引用编号）
  final void Function(int order) onCitationTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isUser = message.role == 'user';
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 6, horizontal: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        constraints: const BoxConstraints(maxWidth: 720),
        decoration: BoxDecoration(
          color: isUser
              ? theme.colorScheme.primaryContainer.withValues(alpha: 0.6)
              : theme.colorScheme.surfaceContainerLowest,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(12),
            topRight: const Radius.circular(12),
            bottomLeft: Radius.circular(isUser ? 12 : 2),
            bottomRight: Radius.circular(isUser ? 2 : 12),
          ),
          border: Border.all(color: theme.dividerColor, width: 1),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            _buildBody(context),
            if (message.degraded) ...[
              const SizedBox(height: 8),
              _banner(
                context,
                icon: Icons.swap_vert_circle_outlined,
                color: AppColors.warning,
                text: '重排服务暂不可用，已使用融合排序前 5 候选回答',
              ),
            ],
            if (message.refused) ...[
              const SizedBox(height: 8),
              _banner(
                context,
                icon: Icons.info_outline,
                color: theme.colorScheme.onSurfaceVariant,
                text: '当前知识库中没有足够依据，已拒答回答',
              ),
            ],
            if (message.cancelled) ...[
              const SizedBox(height: 8),
              _banner(
                context,
                icon: Icons.stop_circle_outlined,
                color: theme.colorScheme.onSurfaceVariant,
                text: '已停止生成，保留已生成的正文与引用',
              ),
            ],
            if (message.errorMessage != null) ...[
              const SizedBox(height: 8),
              _banner(
                context,
                icon: Icons.error_outline,
                color: AppColors.error,
                text: message.errorMessage!,
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildBody(BuildContext context) {
    final empty = message.content.isEmpty;
    if (message.role == 'user') {
      return Text(message.content, style: const TextStyle(fontSize: 14));
    }
    if (empty && message.streaming) {
      return Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const SizedBox(
            width: 14,
            height: 14,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
          const SizedBox(width: 8),
          Text(
            '正在思考',
            style: TextStyle(
              fontSize: 13,
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      );
    }
    return _buildAnswerText(context);
  }

  /// 回答正文：[S编号] 片段可点击定位引用
  Widget _buildAnswerText(BuildContext context) {
    final theme = Theme.of(context);
    final spans = <InlineSpan>[];
    var cursor = 0;
    for (final match in _citationMarker.allMatches(message.content)) {
      if (match.start > cursor) {
        spans.add(TextSpan(text: message.content.substring(cursor, match.start)));
      }
      final order = int.tryParse(match.group(1)!);
      spans.add(
        WidgetSpan(
          alignment: PlaceholderAlignment.middle,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 2),
            child: InkWell(
              borderRadius: BorderRadius.circular(6),
              onTap: order == null ? null : () => onCitationTap(order),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                decoration: BoxDecoration(
                  color: theme.colorScheme.primaryContainer,
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  '[S${match.group(1)}]',
                  style: TextStyle(
                    fontSize: 12,
                    height: 1.4,
                    color: theme.colorScheme.onPrimaryContainer,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          ),
        ),
      );
      cursor = match.end;
    }
    if (cursor < message.content.length) {
      spans.add(TextSpan(text: message.content.substring(cursor)));
    }
    return SelectableText.rich(
      TextSpan(
        style: const TextStyle(fontSize: 14, height: 1.5),
        children: spans,
      ),
    );
  }

  Widget _banner(
    BuildContext context, {
    required IconData icon,
    required Color color,
    required String text,
  }) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 15, color: color),
        const SizedBox(width: 6),
        Expanded(
          child: Text(
            text,
            style: TextStyle(fontSize: 12, color: color),
          ),
        ),
      ],
    );
  }
}

/// 引用卡：快照事实（文件名/版本/页码/章节/正文/分数）
///
/// chunk_id 为空表示源切片已清理，标注"原始来源已清理，显示快照"。
class CitationCard extends StatelessWidget {
  const CitationCard({super.key, required this.citation, required this.highlighted});

  final CitationSnapshot citation;
  final bool highlighted;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scores = [
      ('向量', citation.vectorScore),
      ('关键词', citation.keywordScore),
      ('融合', citation.fusionScore),
      ('重排', citation.rerankScore),
    ]
        .where((entry) => entry.$2 != null)
        .map((entry) => '${entry.$1} ${entry.$2!.toStringAsFixed(3)}')
        .join(' · ');
    final location = [
      if (citation.pageNo != null) '第 ${citation.pageNo} 页',
      if (citation.sectionPath.isNotEmpty) citation.sectionPath,
    ].join(' / ');
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: highlighted
            ? theme.colorScheme.primaryContainer.withValues(alpha: 0.55)
            : theme.colorScheme.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          width: highlighted ? 2 : 1,
          color: highlighted ? theme.colorScheme.primary : theme.dividerColor,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                decoration: BoxDecoration(
                  color: theme.colorScheme.primaryContainer,
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  '[S${citation.citationOrder ?? '-'}]',
                  style: TextStyle(
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    color: theme.colorScheme.onPrimaryContainer,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  citation.fileName.isEmpty ? '未知来源' : citation.fileName,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              Text(
                'v${citation.versionNo}',
                style: TextStyle(
                  fontSize: 12,
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
            ],
          ),
          if (location.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(
              location,
              style: TextStyle(
                fontSize: 12,
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
          ],
          if (citation.chunkId == null) ...[
            const SizedBox(height: 6),
            Row(
              children: [
                Icon(
                  Icons.history_toggle_off,
                  size: 13,
                  color: AppColors.warning,
                ),
                const SizedBox(width: 5),
                Text(
                  '原始来源已清理，显示快照',
                  style: TextStyle(fontSize: 11, color: AppColors.warning),
                ),
              ],
            ),
          ],
          const SizedBox(height: 8),
          SelectableText(
            citation.content,
            style: const TextStyle(fontSize: 13, height: 1.5),
          ),
          if (scores.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              scores,
              style: TextStyle(
                fontSize: 11,
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
