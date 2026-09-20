import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

/// AppShell 全局状态：当前知识库与服务可达性
///
/// 仅承载壳层展示所需的最小事实；页面级状态由各页面控制器管理。
/// 服务探测只反映 /ping 可达性，不介入页面内部的连接流程。
class AppShellController extends ChangeNotifier {
  /// 后端服务地址仅在设置页可配置（后续任务接入），默认本机服务
  static final Uri defaultPingUrl = Uri.parse('http://127.0.0.1:8000/ping');

  AppShellController({
    http.Client? client,
    Uri? pingUrl,
    this.probeInterval = const Duration(seconds: 30),
  }) : pingUrl = pingUrl ?? defaultPingUrl,
       _client = client ?? http.Client(),
       _ownsClient = client == null;

  final http.Client _client;

  /// 服务探测地址与轮询间隔（测试可注入缩短节奏或指向 mock 服务）
  final Uri pingUrl;
  final Duration probeInterval;

  final bool _ownsClient;
  Timer? _timer;

  /// 当前知识库名称（知识库域接入前为 null，顶栏显示占位文案）
  String? currentKnowledgeBaseName;

  /// 后端服务是否可达
  bool serviceOnline = false;

  /// 开始周期探测：立即探测一次，之后按固定间隔轮询
  void start() {
    unawaited(_probe());
    _timer = Timer.periodic(probeInterval, (_) => unawaited(_probe()));
  }

  /// 切换当前知识库（名称仅用于顶栏展示）
  void setCurrentKnowledgeBase(String? name) {
    if (currentKnowledgeBaseName == name) return;
    currentKnowledgeBaseName = name;
    notifyListeners();
  }

  Future<void> _probe() async {
    bool online;
    try {
      final response = await _client
          .get(pingUrl)
          .timeout(const Duration(seconds: 5));
      online = response.statusCode == 200;
    } on Exception {
      online = false;
    }
    if (online == serviceOnline) return;
    serviceOnline = online;
    notifyListeners();
  }

  @override
  void dispose() {
    _timer?.cancel();
    if (_ownsClient) _client.close();
    super.dispose();
  }
}
