// 测试环境支撑：临时 Hive 目录、mock 偏好与应用装配的公共入口。
//
// 说明：widget test 的 FakeAsync 环境会拦截全部 HTTP 请求（固定返回 400），
// 因此连接必然走失败路径；Hive boxes 在真实异步环境中预打开，
// 避免真实文件 IO 在 FakeAsync 中无法完成。
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:hive/hive.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/app/router.dart';
import 'package:desktop_document_ai/controllers/app_shell_controller.dart';
import 'package:desktop_document_ai/main.dart';
import 'package:desktop_document_ai/models/drop_file_model.dart';

/// path_provider 平台接口 mock
/// 将应用文档目录指向测试专用临时目录，避免测试触碰用户真实 Hive 数据
class _MockPathProviderPlatform extends PathProviderPlatform {
  _MockPathProviderPlatform(this.tempPath);

  final String tempPath;

  @override
  Future<String?> getApplicationDocumentsPath() async => tempPath;
}

class AppTestEnv {
  AppTestEnv._();

  /// 初始化临时 Hive 目录并预打开应用使用的 boxes
  static Future<Directory> setUp() async {
    final tempDir = await Directory.systemTemp.createTemp('rag_chat_db_test');
    PathProviderPlatform.instance = _MockPathProviderPlatform(tempDir.path);

    // Hive 为全局单例，openBox 幂等；预打开使被测代码在 FakeAsync
    // 环境中 openBox 时直接命中已打开的实例
    Hive.init(tempDir.path);
    await Hive.openBox<Map>('conversations');
    await Hive.openBox<Map>('messages');
    await Hive.openBox<Map>('files');
    return tempDir;
  }

  /// 清理临时目录；Hive 关闭是异步的，文件占用时允许残留
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
/// 服务探测控制器不调用 start()，避免测试结束后残留周期定时器。
Future<Widget> buildWorkbenchApp({
  Map<String, Object> preferences = const {},
}) async {
  SharedPreferences.setMockInitialValues(preferences);
  final appPreferences = await AppPreferences.load();
  return MultiProvider(
    providers: [
      ChangeNotifierProvider(create: (context) => DropFileModel()),
      ChangeNotifierProvider<AppShellController>(
        create: (context) => AppShellController(),
      ),
    ],
    child: WorkbenchApp(
      router: createRouter(
        initialLocation: appPreferences.lastLocation,
        preferences: appPreferences,
      ),
      themeMode: appPreferences.themeMode,
      preferences: appPreferences,
    ),
  );
}

/// 等待问答页连接流程走完并清除 SnackBar 定时器，避免测试结束后残留
Future<void> settleChatConnection(WidgetTester tester) async {
  await tester.pump();

  // 连接与初始化的完成回调需要真实事件循环轮转，分批等待最多约 2 秒，
  // 直到问答页状态稳定
  for (var i = 0; i < 20; i++) {
    await tester.runAsync(() async {
      await Future<void>.delayed(const Duration(milliseconds: 100));
    });
    await tester.pump();
    if (find.text('智能问答').evaluate().isNotEmpty) break;
  }

  // 推进虚拟时间：清除连接失败路径下 SnackBar 的 4 秒展示 timer 及退场动画
  await tester.pump(const Duration(seconds: 6));
  await tester.pump();
}
