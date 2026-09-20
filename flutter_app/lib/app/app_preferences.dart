import 'dart:async';

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

/// 本地偏好存取：主题模式、最后页面与窗口尺寸
///
/// 仅保存可重建的界面偏好；业务数据一律以后端 API 为权威。
class AppPreferences {
  AppPreferences._(this._prefs);

  final SharedPreferences _prefs;

  static const _keyThemeMode = 'ui.theme_mode';
  static const _keyLastLocation = 'ui.last_location';
  static const _keyWindowWidth = 'ui.window_width';
  static const _keyWindowHeight = 'ui.window_height';
  static const _keyClientInstanceId = 'client.instance_id';
  static const _keyLastKbId = 'ui.last_kb_id';
  static const _keyLastConversationId = 'ui.last_conversation_id';

  /// 默认窗口尺寸与默认进入页面
  static const Size defaultWindowSize = Size(1280, 720);
  static const String defaultLocation = '/chat';

  /// 窗口最小尺寸（与 window_manager 初始化共用，保障紧凑档可用）
  static const Size minWindowSize = Size(720, 560);

  static Future<AppPreferences> load() async =>
      AppPreferences._(await SharedPreferences.getInstance());

  /// 主题模式；未知取值按跟随系统处理
  ThemeMode get themeMode {
    switch (_prefs.getString(_keyThemeMode)) {
      case 'light':
        return ThemeMode.light;
      case 'dark':
        return ThemeMode.dark;
      default:
        return ThemeMode.system;
    }
  }

  /// 上次停留的页面位置；无效值由路由侧回退到默认页面
  String get lastLocation =>
      _prefs.getString(_keyLastLocation) ?? defaultLocation;

  /// 上次窗口尺寸；未记录或记录残缺时返回默认尺寸
  Size get windowSize {
    final width = _prefs.getDouble(_keyWindowWidth);
    final height = _prefs.getDouble(_keyWindowHeight);
    if (width == null || height == null) return defaultWindowSize;
    return Size(width, height);
  }

  Future<void> saveThemeMode(ThemeMode mode) => _prefs.setString(
        _keyThemeMode,
        switch (mode) {
          ThemeMode.light => 'light',
          ThemeMode.dark => 'dark',
          ThemeMode.system => 'system',
        },
      );

  Future<void> saveLastLocation(String location) =>
      _prefs.setString(_keyLastLocation, location);

  Future<void> saveWindowSize(Size size) => Future.wait([
        _prefs.setDouble(_keyWindowWidth, size.width),
        _prefs.setDouble(_keyWindowHeight, size.height),
      ]);

  String? _instanceIdCache;

  /// 客户端实例标识：首次访问生成随机 UUID 并持久化（进程内缓存避免
  /// 重复生成竞态）；服务端只落 SHA-256，原文不出本机
  String get clientInstanceId {
    final existing = _instanceIdCache ?? _prefs.getString(_keyClientInstanceId);
    if (existing != null) {
      _instanceIdCache = existing;
      return existing;
    }
    final generated = const Uuid().v4();
    _instanceIdCache = generated;
    unawaited(_prefs.setString(_keyClientInstanceId, generated));
    return generated;
  }

  String? _lastKbIdCache;

  /// 最近选择的知识库（重启恢复；失效 ID 由知识库页校验后忽略）
  String? get lastKnowledgeBaseId =>
      _lastKbIdCache ?? _prefs.getString(_keyLastKbId);

  Future<void> saveLastKnowledgeBaseId(String? kbId) {
    _lastKbIdCache = kbId;
    if (kbId == null) return _prefs.remove(_keyLastKbId);
    return _prefs.setString(_keyLastKbId, kbId);
  }

  String? _lastConversationIdCache;

  /// 最近选择的会话（重启恢复；失效 ID 由问答页校验后忽略并清理）
  String? get lastConversationId =>
      _lastConversationIdCache ?? _prefs.getString(_keyLastConversationId);

  Future<void> saveLastConversationId(String? conversationId) {
    _lastConversationIdCache = conversationId;
    if (conversationId == null) return _prefs.remove(_keyLastConversationId);
    return _prefs.setString(_keyLastConversationId, conversationId);
  }
}
