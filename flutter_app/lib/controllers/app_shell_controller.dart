import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api/knowledge_api_client.dart';

/// AppShell 全局状态：当前知识库、服务可达性与非终态任务数
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
    KnowledgeApiClient? knowledgeClient,
    this.taskPollInterval = const Duration(seconds: 10),
  }) : pingUrl = pingUrl ?? defaultPingUrl,
       _client = client ?? http.Client(),
       _ownsClient = client == null,
       _knowledge = knowledgeClient;

  final http.Client _client;

  /// 服务探测地址与轮询间隔（测试可注入缩短节奏或指向 mock 服务）
  final Uri pingUrl;
  final Duration probeInterval;

  /// 任务数轮询（顶栏徽标）；未注入知识库客户端时不启用
  final KnowledgeApiClient? _knowledge;
  final Duration taskPollInterval;

  final bool _ownsClient;
  Timer? _timer;
  Timer? _taskTimer;

  /// 当前知识库 ID 与名称（知识库域接入前为 null，顶栏显示占位文案）
  ///
  /// ID 供问答页感知"最近选择"变化并自动重载（IndexedStack 分支保活，
  /// 页面不会重建，需经广播同步跨页事实）
  String? currentKnowledgeBaseId;
  String? currentKnowledgeBaseName;

  /// 后端服务是否可达
  bool serviceOnline = false;

  /// 非终态任务数（null 表示尚未获取；顶栏徽标展示）
  int? nonTerminalTaskCount;

  /// 开始周期探测：立即探测一次，之后按固定间隔轮询
  void start() {
    unawaited(_probe());
    _timer = Timer.periodic(probeInterval, (_) => unawaited(_probe()));
    if (_knowledge != null) {
      unawaited(_pollTaskCount());
      _taskTimer = Timer.periodic(
        taskPollInterval,
        (_) => unawaited(_pollTaskCount()),
      );
    }
  }

  /// 切换当前知识库（ID 驱动跨页同步，名称仅用于顶栏展示）
  void setCurrentKnowledgeBase(String? id, String? name) {
    if (currentKnowledgeBaseId == id && currentKnowledgeBaseName == name) {
      return;
    }
    currentKnowledgeBaseId = id;
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

  /// 轮询非终态任务数（本地队列非终态上限 53，一页足够覆盖）
  Future<void> _pollTaskCount() async {
    final knowledge = _knowledge;
    if (knowledge == null) return;
    try {
      final page = await knowledge.listTasks(limit: 200);
      final count = page.items.where((task) => !task.state.isTerminal).length;
      if (count == nonTerminalTaskCount) return;
      nonTerminalTaskCount = count;
      notifyListeners();
    } on Exception {
      // 徽标轮询失败静默保留旧值，下一次周期重试
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    _taskTimer?.cancel();
    if (_ownsClient) _client.close();
    super.dispose();
  }
}
