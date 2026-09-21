// 设置控制器测试：服务地址校验与生效、恢复默认、连接测试状态机、
// 服务健康与运行配置加载（失败隔离与防过期）。
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:desktop_document_ai/api/ops_api_client.dart';
import 'package:desktop_document_ai/app/app_preferences.dart';
import 'package:desktop_document_ai/app/server_address.dart';
import 'package:desktop_document_ai/controllers/settings_controller.dart';

Map<String, Object?> _envelope(Map<String, Object?> data) => {
      'success': true,
      'request_id': 'req-test',
      'data': data,
      'error': null,
    };

const _healthPayload = {
  'status': 'degraded',
  'components': {
    'sqlite': {'state': 'ok'},
    'chroma': {'state': 'ok', 'detail': null},
    'fts': {'state': 'error'},
    'worker': {'state': 'stopped', 'worker_enabled': false},
    'providers': {
      'mineru': {'configured': true},
      'dashscope': {'configured': false},
    },
  },
  'queue': {
    'running': 1,
    'pending': 2,
    'capacity_running': 3,
    'capacity_pending': 50,
    'capacity_non_terminal': 53,
  },
  'degraded': ['fts', 'worker'],
};

const _configPayload = {
  'model_profiles': [
    {
      'role': 'embedding',
      'provider': 'dashscope',
      'model_name': 'text-embedding-v4',
    },
    {
      'role': 'generation',
      'provider': 'dashscope',
      'model_name': 'qwen-plus',
    },
  ],
  'pipeline_configs': [
    {
      'config_type': 'retrieval',
      'version': 1,
      'config_summary': {'vector_top_k': 20, 'rrf_k': 60},
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
      opsClient: OpsApiClient(client: MockClient(handler), address: store),
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

  test('refreshHealth 解析组件状态、凭据与队列水位', () async {
    final controller = await controllerWith((request) async {
      expect(request.url.path, '/api/v1/health');
      return http.Response(
        jsonEncode(_envelope(_healthPayload)),
        200,
        headers: {'content-type': 'application/json'},
      );
    });

    await controller.refreshHealth();
    expect(controller.healthError, isNull);
    final health = controller.health!;
    expect(health.status, 'degraded');
    expect(health.degraded, ['fts', 'worker']);
    expect(
      {for (final component in health.components) component.name: component.state},
      {'sqlite': 'ok', 'chroma': 'ok', 'fts': 'error', 'worker': 'stopped'},
    );
    expect(health.providers.first.configured, isTrue);
    expect(health.providers.last.configured, isFalse);
    expect(health.queue.running, 1);
    expect(health.queue.capacityNonTerminal, 53);
  });

  test('refreshConfig 解析模型身份、配置摘要与容量开关', () async {
    final controller = await controllerWith((request) async {
      expect(request.url.path, '/api/v1/config/public');
      return http.Response(
        jsonEncode(_envelope(_configPayload)),
        200,
        headers: {'content-type': 'application/json'},
      );
    });

    await controller.refreshConfig();
    expect(controller.configError, isNull);
    final config = controller.publicConfig!;
    expect(config.modelProfiles.length, 2);
    expect(config.modelProfiles.first.modelName, 'text-embedding-v4');
    expect(config.pipelineConfigs.single.summary['vector_top_k'], 20);
    expect(config.limits.maxFileMb, 100);
    expect(config.features.localDebugEnabled, isFalse);
  });

  test('loadInitial 并行加载且单路失败不阻塞另一路', () async {
    final controller = await controllerWith((request) async {
      if (request.url.path == '/api/v1/health') {
        return http.Response(
          jsonEncode({
            'success': false,
            'request_id': 'req',
            'data': null,
            'error': {'code': 'INTERNAL_ERROR', 'message': '数据库不可用'},
          }),
          503,
          headers: {'content-type': 'application/json'},
        );
      }
      return http.Response(
        jsonEncode(_envelope(_configPayload)),
        200,
        headers: {'content-type': 'application/json'},
      );
    });

    controller.loadInitial();
    await Future<void>.delayed(const Duration(milliseconds: 10));

    // 健康路失败落错误态，配置路照常展示
    expect(controller.health, isNull);
    expect(controller.healthError, isNotNull);
    expect(controller.healthLoading, isFalse);
    expect(controller.publicConfig, isNotNull);
    expect(controller.configError, isNull);
  });

  test('并发刷新防过期：旧健康响应不覆盖新状态', () async {
    final gate = Completer<void>();
    var firstCall = true;
    final controller = await controllerWith((request) async {
      if (firstCall) {
        firstCall = false;
        await gate.future;
        return http.Response(
          jsonEncode(_envelope(_healthPayload)),
          200,
          headers: {'content-type': 'application/json'},
        );
      }
      return http.Response(
        jsonEncode(_envelope({
          ..._healthPayload,
          'status': 'healthy',
          'degraded': <String>[],
        })),
        200,
        headers: {'content-type': 'application/json'},
      );
    });

    final first = controller.refreshHealth();
    final second = controller.refreshHealth();
    gate.complete();
    await Future.wait([first, second]);

    // 后发请求先回时旧响应被丢弃，最终状态以最新请求为准
    expect(controller.health!.status, 'healthy');
    expect(controller.health!.degraded, isEmpty);
  });
}
