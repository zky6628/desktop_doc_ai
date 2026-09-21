import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:window_manager/window_manager.dart';

import 'api/conversation_api_client.dart';
import 'api/knowledge_api_client.dart';
import 'api/query_api_client.dart';
import 'app/app_preferences.dart';
import 'app/router.dart';
import 'app/server_address.dart';
import 'controllers/app_shell_controller.dart';
import 'controllers/chat_controller.dart';
import 'controllers/theme_controller.dart';
import 'theme/theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final preferences = await AppPreferences.load();
  await _initWindow(preferences);

  final addressStore = ServerAddressStore(preferences.serverBaseUrl);
  final knowledgeClient = KnowledgeApiClient(address: addressStore);
  final shellController = AppShellController(
    address: addressStore,
    knowledgeClient: knowledgeClient,
  )..start();
  final themeController = ThemeController(preferences);
  final queryClient = QueryApiClient(
    address: addressStore,
    instanceId: preferences.clientInstanceId,
  );
  final conversationClient = ConversationApiClient(address: addressStore);
  final bundle = ApiBundle(
    preferences: preferences,
    addressStore: addressStore,
    themeController: themeController,
    knowledgeClient: knowledgeClient,
    queryClient: queryClient,
    conversationClient: conversationClient,
  );
  // 问答控制器为全局单例：外壳侧边栏（新建会话/会话列表）与问答页共享
  // 同一实例；知识库解析经壳层控制器广播实现跨页同步。会话列表为跨库
  // 全量历史，创建即预载供侧边栏展示
  final chatController = ChatController(
    queryClient: queryClient,
    conversationClient: conversationClient,
    knowledgeClient: knowledgeClient,
    preferences: preferences,
    onKnowledgeBaseResolved: shellController.setCurrentKnowledgeBase,
  );
  unawaited(chatController.reloadConversations());

  runApp(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<AppShellController>.value(value: shellController),
        ChangeNotifierProvider<ThemeController>.value(value: themeController),
        ChangeNotifierProvider<ChatController>.value(value: chatController),
      ],
      child: WorkbenchApp(
        router: createRouter(
          initialLocation: preferences.lastLocation,
          preferences: preferences,
          bundle: bundle,
        ),
        themeController: themeController,
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
/// 主题模式经 [ThemeController] 运行时切换（设置页入口），切换即时生效。
class WorkbenchApp extends StatefulWidget {
  const WorkbenchApp({
    super.key,
    required this.router,
    required this.themeController,
    required this.preferences,
  });

  final GoRouter router;
  final ThemeController themeController;
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
    return ListenableBuilder(
      listenable: widget.themeController,
      builder: (context, _) {
        return MaterialApp.router(
          title: 'Document AI - RAG 文档助手',
          theme: AppTheme.light,
          darkTheme: AppTheme.dark,
          themeMode: widget.themeController.mode,
          routerConfig: widget.router,
        );
      },
    );
  }
}
