// 应用主界面 smoke 测试：验证 AppShell 与问答页在无真实后端的情况下
// 完成初始化并渲染骨架，同时覆盖连接失败路径（初始化异常不阻断 UI）。
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'support/app_test_env.dart';

void main() {
  late Directory tempDir;

  setUp(() async {
    tempDir = await AppTestEnv.setUp();
  });

  tearDown(() async {
    await AppTestEnv.tearDown(tempDir);
  });

  testWidgets('应用启动后渲染 AppShell 与问答页骨架', (WidgetTester tester) async {
    await tester.pumpWidget(await buildWorkbenchApp());
    await settleChatConnection(tester);

    // AppShell 顶栏与问答页内容正常渲染（连接失败时 UI 同样恢复，不卡加载页）
    expect(find.text('未选择知识库'), findsOneWidget);
    expect(find.text('智能问答'), findsOneWidget);
  });
}
