// 测试环境支撑：mock 偏好与应用装配的公共入口。
//
// 说明：widget test 的 FakeAsync 环境会拦截全部 HTTP 请求（固定返回
// 400），因此请求必然走失败路径；问答页在无缓存知识库时直接渲染空态，
// 不发起网络请求。
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/api/conversation_api_client.dart';
import 'package:desktop_document_ai/api/knowledge_api_client.dart';
import 'package:desktop_document_ai/api/query_api_client.dart';
import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/app/router.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/controllers/app_shell_controller.dart';
import 'package:desktop_document_ai/controllers/theme_controller.dart';
import 'package:desktop_document_ai/main.dart';

class AppTestEnv {
  AppTestEnv._();

  /// 保留临时目录生命周期形状（历史测试签名）；业务路径已不再依赖
  static Future<Directory> setUp() =>
      Directory.systemTemp.createTemp('workbench_test');

  /// 清理临时目录；文件占用时允许残留
  static Future<void> tearDown(Directory tempDir) async {
    try {
      await tempDir.delete(recursive: true);
    } on FileSystemException {
      // 测试临时目录允许残留
    }
  }
}

/// 以与 main() 相同的装配方式构建应用。
///
/// 服务探测控制器不调用 start()，避免测试结束后残留周期定时器；
/// 客户端为真实实现但无后端可达，页面按失败路径渲染。
Future<Widget> buildWorkbenchApp({
  Map<String, Object> preferences = const {},
}) async {
  SharedPreferences.setMockInitialValues(preferences);
  final appPreferences = await AppPreferences.load();
  final addressStore = ServerAddressStore(appPreferences.serverBaseUrl);
  return _assemble(
    appPreferences,
    addressStore: addressStore,
    knowledgeClient: KnowledgeApiClient(address: addressStore),
  );
}

/// 构建带指定知识库域客户端的应用（路由注入 ApiBundle）
///
/// 注入的客户端由用例自建（通常绑定 MockClient），地址取值不影响
/// mock 响应；装配层使用偏好派生的独立地址源驱动壳层探测。
Future<Widget> buildWorkbenchAppWithKnowledge({
  Map<String, Object> preferences = const {},
  required KnowledgeApiClient knowledgeClient,
}) async {
  SharedPreferences.setMockInitialValues(preferences);
  final appPreferences = await AppPreferences.load();
  return _assemble(
    appPreferences,
    addressStore: ServerAddressStore(appPreferences.serverBaseUrl),
    knowledgeClient: knowledgeClient,
  );
}

Future<Widget> _assemble(
  AppPreferences appPreferences, {
  required ServerAddressStore addressStore,
  required KnowledgeApiClient knowledgeClient,
}) async {
  final themeController = ThemeController(appPreferences);
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<AppShellController>(
        create: (context) => AppShellController(address: addressStore),
      ),
      ChangeNotifierProvider<ThemeController>.value(value: themeController),
    ],
    child: WorkbenchApp(
      router: createRouter(
        initialLocation: appPreferences.lastLocation,
        preferences: appPreferences,
        bundle: ApiBundle(
          preferences: appPreferences,
          addressStore: addressStore,
          themeController: themeController,
          knowledgeClient: knowledgeClient,
          queryClient: QueryApiClient(
            address: addressStore,
            instanceId: appPreferences.clientInstanceId,
          ),
          conversationClient: ConversationApiClient(address: addressStore),
        ),
      ),
      themeController: themeController,
      preferences: appPreferences,
    ),
  );
}

/// 等待问答页渲染稳定并清除错误 SnackBar 定时器，避免测试结束后残留
Future<void> settleChatConnection(WidgetTester tester) async {
  await tester.pump();

  // 页面初始化（含失败路径）的完成回调需要真实事件循环轮转，分批
  // 等待最多约 2 秒，直到问答页空态或骨架渲染出现
  for (var i = 0; i < 20; i++) {
    await tester.runAsync(() async {
      await Future<void>.delayed(const Duration(milliseconds: 100));
    });
    await tester.pump();
    final settled =
        find.text('选择一个知识库开始问答').evaluate().isNotEmpty ||
            find.text('输入问题，开始与知识库对话').evaluate().isNotEmpty ||
            find.text('前往知识库').evaluate().isNotEmpty;
    if (settled) break;
  }

  // 推进虚拟时间：清除错误路径下 SnackBar 的 4 秒展示 timer 及退场动画
  await tester.pump(const Duration(seconds: 6));
  await tester.pump();
}
