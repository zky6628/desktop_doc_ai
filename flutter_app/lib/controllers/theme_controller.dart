import 'package:flutter/material.dart';

import '../app/app_preferences.dart';

/// 主题模式运行时状态：设置页切换即时生效并持久化
class ThemeController extends ChangeNotifier {
  ThemeController(this._preferences) : mode = _preferences.themeMode;

  final AppPreferences _preferences;

  /// 当前主题模式（启动时取自偏好，重启保持）
  ThemeMode mode;

  /// 切换主题模式：通知全局重建，随后写回偏好
  Future<void> setMode(ThemeMode value) async {
    if (value == mode) return;
    mode = value;
    notifyListeners();
    await _preferences.saveThemeMode(value);
  }
}
