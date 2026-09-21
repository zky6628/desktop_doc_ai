// 设置页 Widget 测试：渲染默认值、地址保存生效、非法输入提示与主题切换。
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/controllers/app_shell_controller.dart';
import 'package:desktop_document_ai/controllers/theme_controller.dart';
import 'package:desktop_document_ai/pages/settings_page.dart';
import 'package:desktop_document_ai/theme/theme.dart';

Future<ThemeController> pumpSettings(WidgetTester tester) async {
  SharedPreferences.setMockInitialValues({});
  final preferences = await AppPreferences.load();
  final store = ServerAddressStore(preferences.serverBaseUrl);
  final themeController = ThemeController(preferences);
  await tester.pumpWidget(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<AppShellController>(
          create: (_) => AppShellController(address: store),
        ),
      ],
      child: ListenableBuilder(
        listenable: themeController,
        builder: (context, _) => MaterialApp(
          theme: AppTheme.light,
          darkTheme: AppTheme.dark,
          themeMode: themeController.mode,
          home: Scaffold(
            body: SettingsPage(
              preferences: preferences,
              addressStore: store,
              themeController: themeController,
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  return themeController;
}

void main() {
  testWidgets('渲染默认地址、主题三档与客户端信息', (tester) async {
    await pumpSettings(tester);

    // 地址输入框预填当前服务地址；hint 同为默认地址，故按 controller 断言避免双匹配
    final addressField = tester.widget<TextField>(find.byType(TextField));
    expect(addressField.controller?.text, 'http://127.0.0.1:8000');
    expect(find.text('跟随系统'), findsOneWidget);
    expect(find.text('亮色'), findsOneWidget);
    expect(find.text('暗色'), findsOneWidget);
    expect(find.text('实例标识'), findsOneWidget);
    // 实例 UUID 为 36 字符（8-4-4-4-12）
    final uuid = find.byType(SelectableText).first;
    expect((tester.widget<SelectableText>(uuid).data ?? '').length, 36);
  });

  testWidgets('非法地址显示校验错误且不生效', (tester) async {
    await pumpSettings(tester);

    await tester.enterText(find.byType(TextField), 'not a url');
    await tester.tap(find.widgetWithText(ElevatedButton, '保存'));
    await tester.pump();

    expect(find.text('请输入合法的服务地址，例如 http://127.0.0.1:8000'), findsOneWidget);
  });

  testWidgets('保存合法地址后立即生效', (tester) async {
    await pumpSettings(tester);

    await tester.enterText(find.byType(TextField), 'http://192.168.1.50:9000');
    await tester.tap(find.widgetWithText(ElevatedButton, '保存'));
    await tester.pump();

    expect(find.text('服务地址已保存，后续请求立即生效'), findsOneWidget);
  });

  testWidgets('切换暗色主题即时生效', (tester) async {
    final themeController = await pumpSettings(tester);

    await tester.tap(find.text('暗色'));
    await tester.pumpAndSettle();

    expect(themeController.mode, ThemeMode.dark);
    final context = tester.element(find.byType(SettingsPage));
    expect(Theme.of(context).brightness, Brightness.dark);
  });
}
