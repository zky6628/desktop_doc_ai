import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api/api_error.dart';
import '../api/dto/ops_dto.dart';
import '../api/ops_api_client.dart';
import '../app/app_preferences.dart';
import '../app/server_address.dart';
import '../utils/request_guard.dart';

/// 连接测试状态
enum ConnectionProbeState { idle, testing, success, failure }

/// 设置控制器：服务地址保存/恢复默认、连接测试、服务健康与运行配置
///
/// 地址写入 AppPreferences 并经 [ServerAddressStore] 对后续请求立即
/// 生效；连接测试仅探测 /ping 可达性，与顶栏状态点同语义。健康与
/// 运行配置为进入加载 + 手动刷新（无自动轮询），两路独立防过期序号，
/// 任一路失败不阻塞另一路展示。
class SettingsController extends ChangeNotifier {
  SettingsController({
    required this.preferences,
    required this.addressStore,
    required OpsApiClient opsClient,
    http.Client? client,
  }) : _ops = opsClient,
       _client = client ?? http.Client(),
       _ownsClient = client == null;

  final AppPreferences preferences;
  final ServerAddressStore addressStore;
  final OpsApiClient _ops;
  final http.Client _client;
  final bool _ownsClient;

  final _healthGuard = LatestRequestGuard();
  final _configGuard = LatestRequestGuard();

  ConnectionProbeState probeState = ConnectionProbeState.idle;

  /// 最近一次连接测试的结果说明（成功含耗时，失败含原因）
  String? probeMessage;

  // ===================== 服务健康 =====================

  ServiceHealth? health;
  bool healthLoading = false;
  ApiException? healthError;

  // ===================== 运行配置 =====================

  PublicConfig? publicConfig;
  bool configLoading = false;
  ApiException? configError;

  /// 首次进入：健康与运行配置并行加载（互不阻塞）
  void loadInitial() {
    unawaited(refreshHealth());
    unawaited(refreshConfig());
  }

  /// 手动刷新健康状态（旧响应在 await 后被新请求取代时静默放弃）
  Future<void> refreshHealth() async {
    healthLoading = true;
    healthError = null;
    notifyListeners();
    final seq = _healthGuard.begin();
    try {
      final result = await _ops.getHealth();
      if (!_healthGuard.isLatest(seq)) return;
      health = result;
    } on ApiException catch (exc) {
      if (!_healthGuard.isLatest(seq)) return;
      healthError = exc;
    } finally {
      if (_healthGuard.isLatest(seq)) {
        healthLoading = false;
        notifyListeners();
      }
    }
  }

  /// 手动刷新运行配置（防过期语义与健康一致）
  Future<void> refreshConfig() async {
    configLoading = true;
    configError = null;
    notifyListeners();
    final seq = _configGuard.begin();
    try {
      final result = await _ops.getPublicConfig();
      if (!_configGuard.isLatest(seq)) return;
      publicConfig = result;
    } on ApiException catch (exc) {
      if (!_configGuard.isLatest(seq)) return;
      configError = exc;
    } finally {
      if (_configGuard.isLatest(seq)) {
        configLoading = false;
        notifyListeners();
      }
    }
  }

  /// 校验并保存服务地址：非法输入返回错误文案，合法则持久化并立即生效
  String? saveAddress(String raw) {
    final uri = ServerAddressStore.tryParse(raw);
    if (uri == null) {
      return '请输入合法的服务地址，例如 http://127.0.0.1:8000';
    }
    unawaited(preferences.saveServerBaseUrl(uri));
    addressStore.update(uri);
    return null;
  }

  /// 恢复默认服务地址（立即生效）
  void restoreDefaultAddress() {
    final uri = Uri.parse(AppPreferences.defaultServerBaseUrl);
    unawaited(preferences.saveServerBaseUrl(uri));
    addressStore.update(uri);
  }

  /// 探测当前服务地址的 /ping 可达性并记录耗时
  Future<void> testConnection() async {
    probeState = ConnectionProbeState.testing;
    probeMessage = null;
    notifyListeners();
    final watch = Stopwatch()..start();
    try {
      final response = await _client
          .get(addressStore.value.replace(path: '/ping'))
          .timeout(const Duration(seconds: 5));
      watch.stop();
      if (response.statusCode == 200) {
        probeState = ConnectionProbeState.success;
        probeMessage = '连接成功（${watch.elapsedMilliseconds} ms）';
      } else {
        probeState = ConnectionProbeState.failure;
        probeMessage = '服务返回 ${response.statusCode}';
      }
    } on Exception {
      probeState = ConnectionProbeState.failure;
      probeMessage = '无法连接到服务';
    }
    notifyListeners();
  }

  @override
  void dispose() {
    if (_ownsClient) _client.close();
    super.dispose();
  }
}
