import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../pages/legacy_chat_page.dart';
import '../pages/placeholder_page.dart';
import 'app_preferences.dart';
import 'app_shell.dart';

/// 工作台路由：五个一级页面以 IndexedStack 分支承载，切换互不丢状态
///
/// 页面携带的 query 参数（如 /chat?kb=&conversation=）由各页面在
/// 对应任务接入时从 [GoRouterState] 读取，路由表无需逐参数声明。
GoRouter createRouter({
  required String initialLocation,
  required AppPreferences preferences,
}) {
  return GoRouter(
    initialLocation: _sanitizeLocation(initialLocation),
    routes: [
      StatefulShellRoute.indexedStack(
        builder: (context, state, navigationShell) =>
            AppShell(navigationShell: navigationShell, preferences: preferences),
        branches: [
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[0],
                builder: (context, state) => const LegacyChatPage(),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[1],
                builder: (context, state) => const PlaceholderPage(
                  icon: Icons.library_books_outlined,
                  title: '知识库',
                  message: '知识库与文档管理将在后续任务提供',
                ),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[2],
                builder: (context, state) => const PlaceholderPage(
                  icon: Icons.task_alt,
                  title: '任务中心',
                  message: '任务队列与事件时间线将在后续任务提供',
                ),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[3],
                builder: (context, state) => const PlaceholderPage(
                  icon: Icons.insights_outlined,
                  title: '评测',
                  message: '查询指标与评测数据将在后续任务提供',
                ),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[4],
                builder: (context, state) => const PlaceholderPage(
                  icon: Icons.settings_outlined,
                  title: '设置',
                  message: '服务连接与界面设置将在后续任务提供',
                ),
              ),
            ],
          ),
        ],
      ),
    ],
  );
}

/// 无效的缓存位置回退到默认页面，避免恢复到已失效地址
String _sanitizeLocation(String location) =>
    AppShell.locations.contains(location)
        ? location
        : AppPreferences.defaultLocation;
