// 设置控制器测试：服务地址校验与生效、恢复默认、连接测试状态机。
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/controllers/settings_controller.dart';

void main() {
  Future<SettingsController> controllerWith(
    Future<http.Response> Function(http.Request) handler, {
    Map<String, Object> initialPrefs = const {},
  }) async {
    SharedPreferences.setMockInitialValues(initialPrefs);
    final preferences = await AppPreferences.load();
    final store = ServerAddressStore(preferences.serverBaseUrl);
    return SettingsController(
      preferences: preferences,
      addressStore: store,
      client: MockClient(handler),
    );
  }

  test('saveAddress 拒绝非法输入且不改变当前地址', () async {
    final controller = await controllerWith((_) async => http.Response('', 200));

    expect(controller.saveAddress('not a url'), isNotNull);
    expect(controller.saveAddress('ftp://127.0.0.1:8000'), isNotNull);
    expect(controller.saveAddress('http://'), isNotNull);
    expect(
      controller.addressStore.value.toString(),
      AppPreferences.defaultServerBaseUrl,
    );
  });

  test('saveAddress 合法输入立即生效并持久化', () async {
    final controller = await controllerWith((_) async => http.Response('', 200));

    final error = controller.saveAddress('http://192.168.1.50:9000');
    expect(error, isNull);
    expect(controller.addressStore.value.toString(), 'http://192.168.1.50:9000');

    // 持久化经异步写入完成，轮转事件循环后重读偏好验证
    await Future<void>.delayed(const Duration(milliseconds: 1));
    final reloaded = await AppPreferences.load();
    expect(reloaded.serverBaseUrl.toString(), 'http://192.168.1.50:9000');
  });

  test('restoreDefaultAddress 恢复默认并生效', () async {
    final controller = await controllerWith(
      (_) async => http.Response('', 200),
      initialPrefs: {'server.base_url': 'http://192.168.1.50:9000'},
    );
    expect(
      controller.addressStore.value.toString(),
      'http://192.168.1.50:9000',
    );

    controller.restoreDefaultAddress();
    expect(
      controller.addressStore.value.toString(),
      AppPreferences.defaultServerBaseUrl,
    );
  });

  test('testConnection 成功记录耗时并进入成功态', () async {
    final controller = await controllerWith((request) async {
      expect(request.url.path, '/ping');
      return http.Response('pong', 200);
    });

    await controller.testConnection();
    expect(controller.probeState, ConnectionProbeState.success);
    expect(controller.probeMessage, contains('连接成功'));
  });

  test('testConnection 非 200 响应归为失败态', () async {
    final controller = await controllerWith(
      (_) async => http.Response('', 503),
    );

    await controller.testConnection();
    expect(controller.probeState, ConnectionProbeState.failure);
    expect(controller.probeMessage, contains('503'));
  });

  test('testConnection 网络异常归为失败态', () async {
    final controller = await controllerWith(
      (_) async => throw http.ClientException('connection refused'),
    );

    await controller.testConnection();
    expect(controller.probeState, ConnectionProbeState.failure);
    expect(controller.probeMessage, contains('无法连接'));
  });
}
