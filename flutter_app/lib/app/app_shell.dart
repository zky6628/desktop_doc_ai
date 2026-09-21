import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../controllers/app_shell_controller.dart';
import '../controllers/chat_controller.dart';
import '../theme/colors.dart';
import '../utils/breakpoints.dart';
import '../widgets/app/sidebar.dart';
import 'app_preferences.dart';

/// 应用外壳：顶栏（当前知识库 / 服务状态 / 任务数）+ 侧边栏 + 页面内容
///
/// 侧边栏为自定义结构：一级导航（新建会话/知识库/任务中心/评测）、
/// 可滚动历史会话列表与吸底设置入口；宽档显示会话摘要行，紧凑档
/// （800-1099px）只保留标题行。问答页不再是显式路由项，由"新建会话"
/// 与会话条目进入。
class AppShell extends StatelessWidget {
  const AppShell({
    super.key,
    required this.navigationShell,
    required this.preferences,
  });

  final StatefulNavigationShell navigationShell;
  final AppPreferences preferences;

  /// 一级页面路径：顺序与路由分支顺序一致（问答分支由侧边栏动作进入，
  /// 路径仍作为 lastLocation 恢复与深链的合法值保留）
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
              WorkbenchSidebar(
                currentBranch: navigationShell.currentIndex,
                onGoBranch: _onGoBranch,
                onNewConversation: () => _onNewConversation(context),
                showSummaries: breakpoint == WindowBreakpoint.expanded,
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
          if (controller.nonTerminalTaskCount != null) ...[
            const SizedBox(width: 16),
            Tooltip(
              message: '非终态任务数，点击进入任务中心',
              child: InkWell(
                onTap: () => _onGoBranch(2),
                borderRadius: BorderRadius.circular(10),
                child: Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 4,
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.task_alt, size: 16),
                      const SizedBox(width: 6),
                      Text(
                        '任务 ${controller.nonTerminalTaskCount}',
                        style: const TextStyle(fontSize: 13),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }

  /// 分支切换：记录最后停留页面，重启后恢复；分支内容保持各自状态
  void _onGoBranch(int index) {
    unawaited(preferences.saveLastLocation(locations[index]));
    navigationShell.goBranch(
      index,
      initialLocation: index == navigationShell.currentIndex,
    );
  }

  /// 新建会话动作：切换到问答分支（不重置分支——重置会让问答页重建
  /// 并按最近会话恢复，与草稿态互斥），再由控制器清为草稿等待输入
  void _onNewConversation(BuildContext context) {
    unawaited(preferences.saveLastLocation(locations[0]));
    navigationShell.goBranch(0, initialLocation: false);
    context.read<ChatController>().newConversation();
  }
}
