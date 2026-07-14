import 'package:flutter/material.dart';
import '../theme/colors.dart';
import '../models/chat_message.dart';
import 'loading_dots.dart';

/// 对话气泡组件
/// 用户消息：右对齐，蓝色背景，白色文字
/// AI 消息：左对齐，灰色背景，黑色文字
class ChatBubble extends StatelessWidget {
  final ChatMessage message;

  /// 是否为加载中的 AI 消息（显示三点动画）
  final bool isLoading;

  const ChatBubble({
    super.key,
    required this.message,
    this.isLoading = false,
  });

  @override
  Widget build(BuildContext context) {
    final isUser = message.isUser;

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        mainAxisAlignment:
            isUser ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (!isUser) _buildAvatar(isUser),
          Flexible(
            child: Container(
              constraints: BoxConstraints(
                maxWidth: MediaQuery.of(context).size.width * 0.6,
              ),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(
                color: isUser
                    ? AppColors.userBubble
                    : AppColors.aiBubble,
                borderRadius: BorderRadius.only(
                  topLeft: const Radius.circular(12),
                  topRight: const Radius.circular(12),
                  bottomLeft: Radius.circular(isUser ? 12 : 4),
                  bottomRight: Radius.circular(isUser ? 4 : 12),
                ),
              ),
              child: _buildContent(isUser),
            ),
          ),
          if (isUser) _buildAvatar(isUser),
        ],
      ),
    );
  }

  /// 构建头像图标
  Widget _buildAvatar(bool isUser) {
    return Container(
      width: 32,
      height: 32,
      margin: const EdgeInsets.symmetric(horizontal: 8),
      decoration: BoxDecoration(
        color: isUser ? AppColors.accent : AppColors.primary,
        shape: BoxShape.circle,
      ),
      child: Icon(
        isUser ? Icons.person : Icons.smart_toy,
        color: Colors.white,
        size: 18,
      ),
    );
  }

  /// 构建气泡内容
  Widget _buildContent(bool isUser) {
    if (isLoading) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 6),
        child: LoadingDots(color: AppColors.primary),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        SelectableText(
          message.content,
          style: TextStyle(
            fontSize: 14,
            height: 1.5,
            color: isUser ? AppColors.userBubbleText : AppColors.aiBubbleText,
          ),
        ),
        // AI 消息附带参考来源
        if (!isUser && message.sources.isNotEmpty) ...[
          const SizedBox(height: 10),
          _buildSources(),
        ],
        // AI 消息显示处理耗时和模型信息
        if (!isUser && message.meta != null) ...[
          const SizedBox(height: 8),
          _buildMetaInfo(),
        ],
        const SizedBox(height: 4),
        Text(
          _formatTime(message.timestamp),
          style: TextStyle(
            fontSize: 10,
            color: isUser
                ? AppColors.userBubbleText.withValues(alpha: 0.7)
                : AppColors.textHint,
          ),
        ),
      ],
    );
  }

  /// 构建参考来源
  Widget _buildSources() {
    return Container(
      padding: const EdgeInsets.all(8),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.menu_book, size: 13, color: AppColors.primary),
              const SizedBox(width: 4),
              Text(
                '参考来源',
                style: TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w600,
                  color: AppColors.primary,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          ...message.sources.asMap().entries.map((entry) {
            return Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    width: 16,
                    height: 16,
                    margin: const EdgeInsets.only(right: 6, top: 2),
                    decoration: BoxDecoration(
                      color: AppColors.primary.withValues(alpha: 0.15),
                      borderRadius: BorderRadius.circular(3),
                    ),
                    child: Center(
                      child: Text(
                        '${entry.key + 1}',
                        style: const TextStyle(
                          fontSize: 10,
                          fontWeight: FontWeight.bold,
                          color: AppColors.primary,
                        ),
                      ),
                    ),
                  ),
                  Expanded(
                    child: Text(
                      entry.value,
                      style: const TextStyle(
                        fontSize: 11,
                        color: AppColors.textSecondary,
                        height: 1.4,
                      ),
                    ),
                  ),
                ],
              ),
            );
          }),
        ],
      ),
    );
  }

  /// 构建处理耗时和模型信息
  Widget _buildMetaInfo() {
    final meta = message.meta!;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.4),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.timer_outlined,
            size: 12,
            color: AppColors.textSecondary,
          ),
          const SizedBox(width: 4),
          Text(
            '处理耗时：${meta.time.toStringAsFixed(1)}秒',
            style: TextStyle(
              fontSize: 11,
              color: AppColors.textSecondary,
            ),
          ),
          if (meta.tokens > 0) ...[
            const SizedBox(width: 12),
            Icon(
              Icons.bolt_outlined,
              size: 12,
              color: AppColors.textSecondary,
            ),
            const SizedBox(width: 4),
            Text(
              '${meta.tokens} tokens',
              style: TextStyle(
                fontSize: 11,
                color: AppColors.textSecondary,
              ),
            ),
          ],
          if (meta.model.isNotEmpty) ...[
            const SizedBox(width: 12),
            Icon(
              meta.provider == 'local'
                  ? Icons.computer_outlined
                  : Icons.cloud_outlined,
              size: 12,
              color: AppColors.textSecondary,
            ),
            const SizedBox(width: 4),
            Text(
              meta.model,
              style: TextStyle(
                fontSize: 11,
                color: AppColors.textSecondary,
              ),
            ),
          ],
        ],
      ),
    );
  }

  String _formatTime(DateTime time) {
    return '${time.hour.toString().padLeft(2, '0')}:'
        '${time.minute.toString().padLeft(2, '0')}';
  }
}
