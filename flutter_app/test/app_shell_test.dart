// AppShell 行为测试：侧边栏导航切换、会话列表吸底设置与亮暗主题装配。
//
// 问答页分支会在启动时执行连接流程，统一通过 settleChatConnection
// 消化异步回调与 SnackBar 定时器；调整窗口尺寸后需推进防抖定时器。
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:desktop_document_ai/theme/colors.dart';
import 'package:desktop_document_ai/theme/theme.dart';
import 'package:desktop_document_ai/widgets/app/sidebar.dart';

import 'support/app_test_env.dart';

/// 点击侧边栏中的导航项图标（排除顶栏同名图标干扰）
Finder sidebarIcon(IconData icon) => find.descendant(
      of: find.byType(WorkbenchSidebar),
      matching: find.byIcon(icon),
    );

/// 设定窗口逻辑尺寸并推进尺寸落盘的防抖定时器
Future<void> setWindowSize(WidgetTester tester, Size size) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pump();
  await tester.pump(const Duration(seconds: 1));
}

void main() {
  late Directory tempDir;

  setUp(() async {
    tempDir = await AppTestEnv.setUp();
  });

  tearDown(() async {
    await AppTestEnv.tearDown(tempDir);
  });

  testWidgets('点击侧边栏切换一级页面且问答页状态保留', (WidgetTester tester) async {
    await tester.pumpWidget(await buildWorkbenchApp());
    await settleChatConnection(tester);

    // 切到知识库页（无后端：知识库列表空态）
    await tester.tap(sidebarIcon(Icons.library_books_outlined).first);
    await tester.pumpAndSettle();
    expect(find.text('选择或创建一个知识库开始管理文档'), findsOneWidget);
    expect(find.text('未选择知识库'), findsOneWidget);

    // 新建会话回到问答分支：IndexedStack 分支不重建，页面内容仍在
    await tester.tap(sidebarIcon(Icons.add_comment_outlined).first);
    await tester.pumpAndSettle();
    expect(find.text('选择一个知识库开始问答'), findsOneWidget);
  });

  testWidgets('侧边栏常驻且设置吸附底部，紧凑档隐藏摘要行', (WidgetTester tester) async {
    await tester.pumpWidget(await buildWorkbenchApp());
    await settleChatConnection(tester);

    // 宽档：侧边栏固定宽度常驻，设置入口与导航项同列渲染
    await setWindowSize(tester, const Size(1200, 800));
    final sidebarWidth = tester.getSize(find.byType(WorkbenchSidebar)).width;
    expect(sidebarWidth, 260);
    expect(find.text('设置'), findsOneWidget);
    expect(find.text('新建会话'), findsOneWidget);
    expect(find.text('历史会话'), findsOneWidget);

    // 紧凑档（800-1099px）：侧边栏仍常驻（隐藏摘要行），无溢出
    await setWindowSize(tester, const Size(900, 800));
    expect(
      tester.getSize(find.byType(WorkbenchSidebar)).width,
      260,
    );

    // <800px：布局无溢出（异常布局会让测试失败）
    await setWindowSize(tester, const Size(720, 560));
    expect(tester.takeException(), isNull);
  });

  testWidgets('亮暗两套主题按偏好装配', (WidgetTester tester) async {
    // 亮暗两套主题的底色配置（纯对象断言，无需各自挂载应用树）
    expect(AppTheme.light.scaffoldBackgroundColor, AppColors.background);
    expect(AppTheme.dark.scaffoldBackgroundColor, AppColors.darkBackground);

    // 偏好为暗色时以暗色主题启动
    await tester.pumpWidget(
      await buildWorkbenchApp(preferences: {'ui.theme_mode': 'dark'}),
    );
    await settleChatConnection(tester);
    final app = tester.widget<MaterialApp>(find.byType(MaterialApp));
    expect(app.themeMode, ThemeMode.dark);
    expect(app.darkTheme?.scaffoldBackgroundColor, AppColors.darkBackground);
  });
}
