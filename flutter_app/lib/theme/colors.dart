import 'package:flutter/material.dart';

/// 应用颜色配置
/// 统一管理全局色彩，确保视觉一致性
class AppColors {
  AppColors._();

  // ===================== 核心色板 =====================

  /// 主色：深蓝色 (#1E3A5F)
  static const Color primary = Color(0xFF1E3A5F);

  /// 辅助色：浅灰 (#F5F5F5)
  static const Color secondary = Color(0xFFF5F5F5);

  /// 强调色：蓝色 (#3B82F6)
  static const Color accent = Color(0xFF3B82F6);

  // ===================== 对话气泡 =====================

  /// 用户消息背景：蓝色
  static const Color userBubble = Color(0xFF3B82F6);

  /// 用户消息文字：白色
  static const Color userBubbleText = Colors.white;

  /// AI 消息背景：灰色
  static const Color aiBubble = Color(0xFFE5E5E5);

  /// AI 消息文字：黑色
  static const Color aiBubbleText = Color(0xFF1A1A1A);

  // ===================== 功能色 =====================

  /// 成功色
  static const Color success = Color(0xFF22C55E);

  /// 警告色
  static const Color warning = Color(0xFFF59E0B);

  /// 错误色
  static const Color error = Color(0xFFEF4444);

  // ===================== 中性色 =====================

  /// 页面背景
  static const Color background = Color(0xFFFAFAFA);

  /// 卡片背景
  static const Color card = Colors.white;

  /// 分割线
  static const Color divider = Color(0xFFE0E0E0);

  /// 主文字
  static const Color textPrimary = Color(0xFF1A1A1A);

  /// 次要文字
  static const Color textSecondary = Color(0xFF6B7280);

  /// 占位文字
  static const Color textHint = Color(0xFF9CA3AF);

  // ===================== 衍生色 =====================

  /// 主色浅色变体（用于 hover / 选中背景）
  static const Color primaryLight = Color(0xFF2E5077);

  /// 强调色浅色变体（用于拖拽高亮背景）
  static const Color accentLight = Color(0xFFDBEAFE);

  /// 强调色透明变体（用于拖拽区域高亮）
  static const Color accentWithOpacity = Color(0x1A3B82F6);
}
