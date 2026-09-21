// 设置页 Widget 测试：渲染默认值、地址保存生效、非法输入提示、主题
// 切换，以及服务健康与运行配置卡片的消费展示。
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/api/ops_api_client.dart';
import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/controllers/app_shell_controller.dart';
import 'package:desktop_document_ai/controllers/theme_controller.dart';
import 'package:desktop_document_ai/pages/settings_page.dart';
import 'package:desktop_document_ai/theme/theme.dart';

Map<String, Object?> _envelope(Map<String, Object?> data) => {
      'success': true,
      'request_id': 'req-test',
      'data': data,
      'error': null,
    };

const _healthPayload = {
  'status': 'healthy',
  'components': {
    'sqlite': {'state': 'ok'},
    'chroma': {'state': 'ok', 'detail': null},
    'fts': {'state': 'ok'},
    'worker': {'state': 'stopped', 'worker_enabled': false},
    'providers': {
      'mineru': {'configured': true},
      'dashscope': {'configured': true},
    },
  },
  'queue': {
    'running': 0,
    'pending': 2,
    'capacity_running': 3,
    'capacity_pending': 50,
    'capacity_non_terminal': 53,
  },
  'degraded': <String>[],
};

const _configPayload = {
  'model_profiles': [
    {
      'role': 'embedding',
      'provider': 'dashscope',
      'model_name': 'text-embedding-v4',
    },
  ],
  'pipeline_configs': [
    {
      'config_type': 'retrieval',
      'version': 1,
      'config_summary': {'vector_top_k': 20},
    },
  ],
  'limits': {
    'max_running': 3,
    'max_pending': 50,
    'max_non_terminal': 53,
    'max_file_mb': 100,
    'max_batch_files': 50,
  },
  'features': {
    'worker_enabled': true,
    'local_debug_enabled': false,
    'cloud_parsing_available': true,
  },
};

http.Response _ok(Map<String, Object?> data) => http.Response(
      jsonEncode(_envelope(data)),
      200,
      headers: {'content-type': 'application/json'},
    );

Future<ThemeController> pumpSettings(
  WidgetTester tester, {
  Future<http.Response> Function(http.Request)? handler,
}) async {
  SharedPreferences.setMockInitialValues({});
  final preferences = await AppPreferences.load();
  final store = ServerAddressStore(preferences.serverBaseUrl);
  final themeController = ThemeController(preferences);
  final opsClient = OpsApiClient(
    client: MockClient((request) async {
      final custom = handler;
      if (custom != null) return custom(request);
      return switch (request.url.path) {
        '/api/v1/health' => _ok(_healthPayload),
        '/api/v1/config/public' => _ok(_configPayload),
        _ => http.Response('', 404),
      };
    }),
    address: store,
  );
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
              opsClient: opsClient,
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
  // 卡片超出测试视口（600px 高）后 ListView 懒加载不构建离屏子项，
  // 断言前按需滚动到目标
  Future<void> scrollUntil(WidgetTester tester, Finder finder) async {
    await tester.scrollUntilVisible(
      finder,
      200,
      scrollable: find.byType(Scrollable).first,
    );
  }

  testWidgets('渲染默认地址、主题三档与客户端信息', (tester) async {
    await pumpSettings(tester);

    // 地址输入框预填当前服务地址；hint 同为默认地址，故按 controller 断言避免双匹配
    final addressField = tester.widget<TextField>(find.byType(TextField));
    expect(addressField.controller?.text, 'http://127.0.0.1:8000');
    await scrollUntil(tester, find.text('跟随系统'));
    expect(find.text('跟随系统'), findsOneWidget);
    expect(find.text('亮色'), findsOneWidget);
    expect(find.text('暗色'), findsOneWidget);
    await scrollUntil(tester, find.text('实例标识'));
    expect(find.text('实例标识'), findsOneWidget);
    // 实例 UUID 为 36 字符（8-4-4-4-12）
    final uuid = find.byType(SelectableText).first;
    expect((tester.widget<SelectableText>(uuid).data ?? '').length, 36);
  });

  testWidgets('服务健康卡片渲染状态、组件与队列水位', (tester) async {
    await pumpSettings(tester);
    await tester.pump(); // 健康与配置请求在 MockClient 上即时完成

    expect(find.text('运行正常'), findsOneWidget);
    expect(find.text('解析任务'), findsOneWidget);
    expect(find.text('未启用'), findsOneWidget);
    expect(find.text('任务队列'), findsOneWidget);
    expect(find.text('运行 0 / 排队 2（上限 3 / 50）'), findsOneWidget);
    expect(find.text('解析凭据'), findsOneWidget);
  });

  testWidgets('运行配置卡片渲染模型身份与配置摘要', (tester) async {
    await pumpSettings(tester);
    await tester.pump();

    expect(find.text('向量化'), findsOneWidget);
    expect(find.text('dashscope / text-embedding-v4'), findsOneWidget);
    expect(find.text('配置 · retrieval v1'), findsOneWidget);
    expect(find.text('向量召回数'), findsOneWidget);
    expect(find.text('20'), findsOneWidget);
    // 调试能力未开启时开关行不展示
    expect(find.text('本地调试检索'), findsNothing);
  });

  testWidgets('健康请求失败时展示错误与重试入口', (tester) async {
    await pumpSettings(
      tester,
      handler: (request) async => switch (request.url.path) {
        '/api/v1/health' => http.Response(
            jsonEncode({
              'success': false,
              'request_id': 'req',
              'data': null,
              'error': {'code': 'INTERNAL_ERROR', 'message': '数据库不可用'},
            }),
            503,
            headers: {'content-type': 'application/json'},
          ),
        '/api/v1/config/public' => _ok(_configPayload),
        _ => http.Response('', 404),
      },
    );
    await tester.pump();

    expect(find.textContaining('无法获取服务健康状态'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);
    // 配置路不受健康失败影响
    expect(find.text('dashscope / text-embedding-v4'), findsOneWidget);
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

    // 一次性拖到列表底部（滚动量被 clamp），外观卡片完整进入视口后
    // 再点击，避免懒加载未构建或视口边缘裁剪导致 tap 落空
    await tester.drag(find.byType(ListView), const Offset(0, -1000));
    await tester.pumpAndSettle();
    await tester.tap(find.text('暗色'));
    await tester.pumpAndSettle();

    expect(themeController.mode, ThemeMode.dark);
    final context = tester.element(find.byType(SettingsPage));
    expect(Theme.of(context).brightness, Brightness.dark);
  });
}
