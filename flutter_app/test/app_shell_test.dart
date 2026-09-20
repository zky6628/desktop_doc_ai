// AppShell 行为测试：一级导航切换、三档响应式形态与亮暗主题装配。
//
// 问答页分支会在启动时执行连接流程，统一通过 settleChatConnection
// 消化异步回调与 SnackBar 定时器；调整窗口尺寸后需推进防抖定时器。
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:desktop_document_ai/theme/colors.dart';
import 'package:desktop_document_ai/theme/theme.dart';

import 'support/app_test_env.dart';

/// 点击侧导航中的目的地图标（排除顶栏同名图标干扰）
Finder railIcon(IconData icon) => find.descendant(
      of: find.byType(NavigationRail),
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

  testWidgets('点击侧导航切换一级页面且问答页状态保留', (WidgetTester tester) async {
    await tester.pumpWidget(await buildWorkbenchApp());
    await settleChatConnection(tester);

    // 切到知识库占位页
    await tester.tap(railIcon(Icons.library_books_outlined).first);
    await tester.pumpAndSettle();
    expect(find.text('知识库与文档管理将在后续任务提供'), findsOneWidget);
    expect(find.text('未选择知识库'), findsOneWidget);

    // 切回问答：IndexedStack 分支不重建，页面内容仍在
    await tester.tap(railIcon(Icons.forum_outlined).first);
    await tester.pumpAndSettle();
    expect(find.text('智能问答'), findsOneWidget);
  });

  testWidgets('宽档显示扩展导航，中档与紧凑档为图标导航且无溢出', (WidgetTester tester) async {
    await tester.pumpWidget(await buildWorkbenchApp());
    await settleChatConnection(tester);

    // >=1100px：扩展导航（带文字标签），宽度显著大于图标档
    await setWindowSize(tester, const Size(1200, 800));
    final expandedWidth = tester.getSize(find.byType(NavigationRail)).width;
    expect(expandedWidth, greaterThan(200));

    // 800-1099px：图标导航
    await setWindowSize(tester, const Size(900, 800));
    final mediumWidth = tester.getSize(find.byType(NavigationRail)).width;
    expect(mediumWidth, lessThan(100));

    // <800px：紧凑档仍可用且布局无溢出（异常布局会让测试失败）
    await setWindowSize(tester, const Size(720, 560));
    final compactWidth = tester.getSize(find.byType(NavigationRail)).width;
    expect(compactWidth, lessThan(100));
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
