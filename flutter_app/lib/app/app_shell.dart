import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../controllers/app_shell_controller.dart';
import '../theme/colors.dart';
import '../utils/breakpoints.dart';
import 'app_preferences.dart';

/// 应用外壳：顶栏（当前知识库 / 服务状态 / 设置）+ 侧导航 + 页面内容
///
/// 响应式形态：>=1100px 导航带文字标签；800-1099px 与 <800px 为图标
/// 导航（窗口已受最小尺寸约束，紧凑档保证可用）。
class AppShell extends StatelessWidget {
  const AppShell({
    super.key,
    required this.navigationShell,
    required this.preferences,
  });

  final StatefulNavigationShell navigationShell;
  final AppPreferences preferences;

  /// 一级导航定义：顺序与路由分支顺序一致
  static const _destinations = [
    (icon: Icons.forum_outlined, selectedIcon: Icons.forum, label: '问答'),
    (
      icon: Icons.library_books_outlined,
      selectedIcon: Icons.library_books,
      label: '知识库',
    ),
    (icon: Icons.task_alt, selectedIcon: Icons.task_alt, label: '任务中心'),
    (icon: Icons.insights_outlined, selectedIcon: Icons.insights, label: '评测'),
    (
      icon: Icons.settings_outlined,
      selectedIcon: Icons.settings,
      label: '设置',
    ),
  ];

  /// 一级页面路径：顺序与导航定义及路由分支顺序一致
  static const locations = [
    '/chat',
    '/knowledge-bases',
    '/tasks',
    '/evaluation',
    '/settings',
  ];

  @override
  Widget build(BuildContext context) {
    final controller = context.watch<AppShellController>();
    return Scaffold(
      appBar: _buildAppBar(context, controller),
      body: LayoutBuilder(
        builder: (context, constraints) {
          final breakpoint = Breakpoints.of(constraints.maxWidth);
          return Row(
            children: [
              NavigationRail(
                selectedIndex: navigationShell.currentIndex,
                onDestinationSelected: _onDestinationSelected,
                extended: breakpoint == WindowBreakpoint.expanded,
                labelType: NavigationRailLabelType.none,
                destinations: [
                  for (final destination in _destinations)
                    NavigationRailDestination(
                      icon: Icon(destination.icon),
                      selectedIcon: Icon(destination.selectedIcon),
                      label: Text(destination.label),
                    ),
                ],
              ),
              const VerticalDivider(thickness: 1, width: 1),
              Expanded(child: navigationShell),
            ],
          );
        },
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(
    BuildContext context,
    AppShellController controller,
  ) {
    return AppBar(
      title: Row(
        children: [
          const Icon(Icons.library_books_outlined, size: 18),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              controller.currentKnowledgeBaseName ?? '未选择知识库',
              overflow: TextOverflow.ellipsis,
            ),
          ),
          const SizedBox(width: 16),
          Tooltip(
            message: controller.serviceOnline ? '服务已连接' : '服务未连接',
            child: Icon(
              Icons.circle,
              size: 12,
              color: controller.serviceOnline
                  ? AppColors.success
                  : AppColors.textHint,
            ),
          ),
        ],
      ),
      actions: [
        IconButton(
          tooltip: '设置',
          icon: const Icon(Icons.settings_outlined),
          onPressed: () => context.go('/settings'),
        ),
        const SizedBox(width: 8),
      ],
    );
  }

  void _onDestinationSelected(int index) {
    // 记录最后停留页面，重启后恢复；分支内容保持各自状态
    unawaited(preferences.saveLastLocation(locations[index]));
    navigationShell.goBranch(
      index,
      initialLocation: index == navigationShell.currentIndex,
    );
  }
}
