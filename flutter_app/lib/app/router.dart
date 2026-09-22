import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../api/conversation_api_client.dart';
import '../api/knowledge_api_client.dart';
import '../api/ops_api_client.dart';
import '../api/query_api_client.dart';
import '../controllers/chat_controller.dart';
import '../controllers/theme_controller.dart';
import '../pages/chat_page.dart';
import '../pages/document_detail_page.dart';
import '../pages/evaluation_page.dart';
import '../pages/knowledge_bases_page.dart';
import '../pages/settings_page.dart';
import '../pages/task_center_page.dart';
import 'app_preferences.dart';
import 'app_shell.dart';
import 'server_address.dart';

/// 前端服务装配：路由页面消费的客户端集合
class ApiBundle {
  ApiBundle({
    required this.preferences,
    required this.addressStore,
    required this.themeController,
    required this.knowledgeClient,
    required this.queryClient,
    required this.conversationClient,
    required this.opsClient,
    required this.chatController,
  });

  final AppPreferences preferences;
  final ServerAddressStore addressStore;
  final ThemeController themeController;
  final KnowledgeApiClient knowledgeClient;
  final QueryApiClient queryClient;
  final ConversationApiClient conversationClient;
  final OpsApiClient opsClient;
  final ChatController chatController;
}

/// 工作台路由：五个一级页面以 IndexedStack 分支承载，切换互不丢状态
///
/// 页面携带的 query 参数（如 /chat?kb=&conversation=）由各页面从
/// [GoRouterState] 读取，路由表无需逐参数声明。
GoRouter createRouter({
  required String initialLocation,
  required AppPreferences preferences,
  required ApiBundle bundle,
}) {
  return GoRouter(
    initialLocation: _sanitizeLocation(initialLocation),
    // 未知/失效地址给出可恢复空态并回到问答页（UI 规范 §2 失效资源处理）
    errorBuilder: (context, state) => _RouteErrorPage(location: state.uri),
    routes: [
      StatefulShellRoute.indexedStack(
        builder: (context, state, navigationShell) =>
            AppShell(navigationShell: navigationShell, preferences: preferences),
        branches: [
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[0],
                builder: (context, state) => ChatPage(
                  initialKbId: state.uri.queryParameters['kb'],
                  initialConversationId:
                      state.uri.queryParameters['conversation'],
                ),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[1],
                builder: (context, state) => KnowledgeBasesPage(
                  knowledgeClient: bundle.knowledgeClient,
                  preferences: bundle.preferences,
                ),
                routes: [
                  // 契约路由：/knowledge-bases/<kb>/documents/<document>
                  GoRoute(
                    path: ':kbId/documents/:docId',
                    builder: (context, state) {
                      final docId = state.pathParameters['docId']!;
                      return DocumentDetailPage(
                        knowledgeClient: bundle.knowledgeClient,
                        documentId: docId,
                      );
                    },
                  ),
                ],
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[2],
                builder: (context, state) => TaskCenterPage(
                  knowledgeClient: bundle.knowledgeClient,
                  initialState: state.uri.queryParameters['state'],
                  initialTaskType: state.uri.queryParameters['task_type'],
                ),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[3],
                builder: (context, state) => EvaluationPage(
                  queryClient: bundle.queryClient,
                  knowledgeClient: bundle.knowledgeClient,
                  opsClient: bundle.opsClient,
                  initialKbId: state.uri.queryParameters['kb'],
                  chatController: bundle.chatController,
                ),
              ),
            ],
          ),
          StatefulShellBranch(
            routes: [
              GoRoute(
                path: AppShell.locations[4],
                builder: (context, state) => SettingsPage(
                  preferences: bundle.preferences,
                  addressStore: bundle.addressStore,
                  themeController: bundle.themeController,
                  opsClient: bundle.opsClient,
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

/// 路由错误页：显示原因并提供回问答页的出口
class _RouteErrorPage extends StatelessWidget {
  const _RouteErrorPage({required this.location});

  final Uri location;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.error_outline, size: 56),
            const SizedBox(height: 12),
            const Text('页面不存在或已失效'),
            const SizedBox(height: 6),
            Text(
              '$location',
              style: const TextStyle(fontSize: 12),
              overflow: TextOverflow.ellipsis,
            ),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: () => context.go(AppPreferences.defaultLocation),
              child: const Text('返回问答'),
            ),
          ],
        ),
      ),
    );
  }
}
