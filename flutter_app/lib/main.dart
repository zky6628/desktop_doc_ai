import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:window_manager/window_manager.dart';

import 'app/app_preferences.dart';
import 'app/router.dart';
import 'controllers/app_shell_controller.dart';
import 'models/drop_file_model.dart';
import 'theme/theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final preferences = await AppPreferences.load();
  await _initWindow(preferences);

  final shellController = AppShellController()..start();

  runApp(
    MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (context) => DropFileModel()),
        ChangeNotifierProvider<AppShellController>.value(value: shellController),
      ],
      child: WorkbenchApp(
        router: createRouter(
          initialLocation: preferences.lastLocation,
          preferences: preferences,
        ),
        themeMode: preferences.themeMode,
        preferences: preferences,
      ),
    ),
  );
}

/// 窗口初始化：恢复上次尺寸并施加最小尺寸约束
Future<void> _initWindow(AppPreferences preferences) async {
  await windowManager.ensureInitialized();
  await windowManager.waitUntilReadyToShow(
    WindowOptions(
      size: preferences.windowSize,
      minimumSize: AppPreferences.minWindowSize,
    ),
    () async {
      await windowManager.show();
      await windowManager.focus();
    },
  );
}

/// 应用根组件：路由与主题在此装配
///
/// 主题模式在启动时读取一次，运行时切换在设置任务接入。
class WorkbenchApp extends StatefulWidget {
  const WorkbenchApp({
    super.key,
    required this.router,
    required this.themeMode,
    required this.preferences,
  });

  final GoRouter router;
  final ThemeMode themeMode;
  final AppPreferences preferences;

  @override
  State<WorkbenchApp> createState() => _WorkbenchAppState();
}

class _WorkbenchAppState extends State<WorkbenchApp> with WidgetsBindingObserver {
  Timer? _sizeSaveDebounce;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeMetrics() {
    // 拖动调整窗口期间高频触发，防抖后再落盘窗口尺寸
    _sizeSaveDebounce?.cancel();
    _sizeSaveDebounce = Timer(const Duration(milliseconds: 800), () {
      final view = WidgetsBinding.instance.platformDispatcher.views.first;
      final size = view.physicalSize / view.devicePixelRatio;
      if (size.width <= 0 || size.height <= 0) return;
      unawaited(widget.preferences.saveWindowSize(size));
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _sizeSaveDebounce?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'Document AI - RAG 文档助手',
      theme: AppTheme.light,
      darkTheme: AppTheme.dark,
      themeMode: widget.themeMode,
      routerConfig: widget.router,
    );
  }
}
