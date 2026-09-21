import 'dart:async';

import 'package:flutter/foundation.dart';

import '../api/api_error.dart';
import '../api/dto/knowledge_dto.dart';
import '../api/dto/metrics_dto.dart';
import '../api/dto/ops_dto.dart';
import '../api/knowledge_api_client.dart';
import '../api/ops_api_client.dart';
import '../api/query_api_client.dart';
import '../utils/request_guard.dart';

/// 评测控制器：查询聚合指标、知识库筛选与本地调试检索
///
/// 指标为聚合复盘数据，采用进入加载与手动刷新（无自动轮询）；
/// 指标、知识库筛选项、调试能力开关与调试检索各自独立防过期序号。
/// 调试检索由服务端运行配置的开关声明驱动显隐（关闭/未知均不渲染），
/// 单次检索不落库不计指标，仅观察各阶段候选明细。
class EvaluationController extends ChangeNotifier {
  EvaluationController({
    required QueryApiClient queryClient,
    required KnowledgeApiClient knowledgeClient,
    required OpsApiClient opsClient,
    this.kbFilter,
  }) : _query = queryClient,
       _knowledge = knowledgeClient,
       _ops = opsClient;

  final QueryApiClient _query;
  final KnowledgeApiClient _knowledge;
  final OpsApiClient _ops;
  final LatestRequestGuard _metricsGuard = LatestRequestGuard();
  final LatestRequestGuard _kbGuard = LatestRequestGuard();
  final LatestRequestGuard _featuresGuard = LatestRequestGuard();
  final LatestRequestGuard _debugGuard = LatestRequestGuard();

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

  /// 调试检索可用性（null = 服务端开关未知/配置加载失败，区域不渲染）
  bool? debugAvailable;

  // ===================== 调试检索状态 =====================

  bool debugLoading = false;
  ApiException? debugError;

  /// 最近一次调试检索结果（null 表示尚未检索）
  SearchDebugResult? debugResult;

  /// 进入页面初始化：并行加载筛选项、指标与调试能力开关
  Future<void> loadInitial() async {
    loading = true;
    notifyListeners();
    await Future.wait([_loadKnowledgeBases(), _loadMetrics(), _loadFeatures()]);
    loading = false;
    notifyListeners();
  }

  /// 手动刷新（保留当前筛选）
  Future<void> refresh() async {
    loading = true;
    notifyListeners();
    await Future.wait([_loadKnowledgeBases(), _loadMetrics(), _loadFeatures()]);
    loading = false;
    notifyListeners();
  }

  /// 切换知识库筛选（影响指标查询与调试检索目标库）
  Future<void> setKbFilter(String? kbId) async {
    if (kbFilter == kbId) return;
    kbFilter = kbId;
    notifyListeners();
    await _loadMetrics();
    notifyListeners();
  }

  /// 执行调试检索：需选定知识库与非空问题；新检索替换旧结果
  Future<void> runDebugSearch({
    required String question,
    int? vectorTopK,
    int? keywordTopK,
    int? fusedTopK,
    int? rerankTopN,
  }) async {
    final kbId = kbFilter;
    final text = question.trim();
    if (kbId == null || text.isEmpty || debugLoading) return;
    debugLoading = true;
    debugError = null;
    notifyListeners();
    final seq = _debugGuard.begin();
    try {
      final result = await _ops.debugSearch(
        knowledgeBaseId: kbId,
        question: text,
        vectorTopK: vectorTopK,
        keywordTopK: keywordTopK,
        fusedTopK: fusedTopK,
        rerankTopN: rerankTopN,
      );
      if (!_debugGuard.isLatest(seq)) return;
      debugResult = result;
    } on ApiException catch (exc) {
      if (!_debugGuard.isLatest(seq)) return;
      debugError = exc;
    } finally {
      if (_debugGuard.isLatest(seq)) {
        debugLoading = false;
        notifyListeners();
      }
    }
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

  Future<void> _loadFeatures() async {
    final seq = _featuresGuard.begin();
    try {
      final config = await _ops.getPublicConfig();
      if (!_featuresGuard.isLatest(seq)) return;
      debugAvailable = config.features.localDebugEnabled;
    } on ApiException {
      // 开关未知时保持 null，调试区域不渲染（评测页主体不受影响）
    }
  }
}
