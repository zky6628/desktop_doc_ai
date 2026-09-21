import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api/api_error.dart';
import '../api/dto/knowledge_dto.dart';
import '../api/dto/metrics_dto.dart';
import '../api/knowledge_api_client.dart';
import '../api/query_api_client.dart';
import '../utils/request_guard.dart';

/// 评测控制器：查询聚合指标与知识库筛选
///
/// 指标为聚合复盘数据，采用进入加载与手动刷新（无自动轮询）；
/// 指标与知识库筛选项各自独立防过期序号（UI 规范 §13）。
class EvaluationController extends ChangeNotifier {
  EvaluationController({
    required QueryApiClient queryClient,
    required KnowledgeApiClient knowledgeClient,
    this.kbFilter,
  }) : _query = queryClient,
       _knowledge = knowledgeClient;

  final QueryApiClient _query;
  final KnowledgeApiClient _knowledge;
  final LatestRequestGuard _metricsGuard = LatestRequestGuard();
  final LatestRequestGuard _kbGuard = LatestRequestGuard();

  /// 聚合指标（null 表示尚未加载成功）
  QueryMetricsSummary? metrics;

  /// 指标加载失败（保留旧值供错误态外的展示）
  ApiException? error;

  /// 进入页面首次加载中
  bool loading = false;

  /// 当前知识库筛选（null 为全部知识库）
  String? kbFilter;

  /// 筛选项可用的知识库列表
  List<KbDto> knowledgeBases = [];

  /// 进入页面初始化：并行加载筛选项与指标
  Future<void> loadInitial() async {
    loading = true;
    notifyListeners();
    await Future.wait([_loadKnowledgeBases(), _loadMetrics()]);
    loading = false;
    notifyListeners();
  }

  /// 手动刷新（保留当前筛选）
  Future<void> refresh() async {
    loading = true;
    notifyListeners();
    await Future.wait([_loadKnowledgeBases(), _loadMetrics()]);
    loading = false;
    notifyListeners();
  }

  /// 切换知识库筛选（仅影响指标查询）
  Future<void> setKbFilter(String? kbId) async {
    if (kbFilter == kbId) return;
    kbFilter = kbId;
    notifyListeners();
    await _loadMetrics();
    notifyListeners();
  }

  Future<void> _loadMetrics() async {
    final seq = _metricsGuard.begin();
    try {
      final summary = await _query.queryMetrics(knowledgeBaseId: kbFilter);
      if (!_metricsGuard.isLatest(seq)) return;
      metrics = summary;
      error = null;
    } on ApiException catch (exc) {
      if (!_metricsGuard.isLatest(seq)) return;
      error = exc;
    }
  }

  Future<void> _loadKnowledgeBases() async {
    final seq = _kbGuard.begin();
    try {
      final page = await _knowledge.listKnowledgeBases(limit: 200);
      if (!_kbGuard.isLatest(seq)) return;
      knowledgeBases = page.items;
    } on ApiException {
      // 筛选项加载失败不阻塞指标展示，保留已加载列表
    }
  }
}
