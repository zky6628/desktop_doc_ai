import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/api_error.dart';
import '../api/dto/knowledge_dto.dart';
import '../api/knowledge_api_client.dart';

/// 任务中心控制器：任务列表（筛选/分页）、详情事件与取消/重试操作
///
/// 只调用 API Client；列表与详情各自独立防过期序号（UI 规范 §13）。
/// 存在非终态任务时按固定间隔自动刷新，全部终态即停。
class TaskCenterController extends ChangeNotifier {
  TaskCenterController({required KnowledgeApiClient knowledgeClient})
    : _knowledge = knowledgeClient;

  final KnowledgeApiClient _knowledge;
  final _listGuard = _LatestRequestGuard();
  final _detailGuard = _LatestRequestGuard();

  ApiException? error;

  List<TaskDto> tasks = [];
  bool loading = false;
  String? _cursor;
  bool hasMore = false;

  String? stateFilter;
  String? taskTypeFilter;

  /// 当前展开详情的任务与事件时间线
  String? expandedTaskId;
  TaskDetail? detail;
  bool detailLoading = false;

  /// 初始化：由路由 query 恢复筛选（UI 规范 §8 筛选写入 URL）
  Future<void> loadInitial({String? state, String? taskType}) async {
    stateFilter = state;
    taskTypeFilter = taskType;
    await reload();
  }

  /// 应用筛选（重置分页）
  Future<void> applyFilters({String? state, String? taskType}) async {
    if (stateFilter == state && taskTypeFilter == taskType) return;
    stateFilter = state;
    taskTypeFilter = taskType;
    collapseDetail();
    await reload();
  }

  /// 重新载入任务列表首页
  Future<void> reload() async {
    loading = true;
    notifyListeners();
    try {
      final seq = _listGuard.begin();
      final page = await _knowledge.listTasks(
        limit: 50,
        state: stateFilter,
        taskType: taskTypeFilter,
      );
      if (!_listGuard.isLatest(seq)) return;
      tasks = page.items;
      _cursor = page.nextCursor;
      hasMore = page.hasMore;
      error = null;
    } on ApiException catch (exc) {
      error = exc;
    } finally {
      loading = false;
      notifyListeners();
    }
    _scheduleAutoRefresh();
  }

  /// 加载更多（keyset 续页）
  Future<void> loadMore() async {
    if (!hasMore || loading) return;
    loading = true;
    notifyListeners();
    try {
      final seq = _listGuard.begin();
      final page = await _knowledge.listTasks(
        limit: 50,
        state: stateFilter,
        taskType: taskTypeFilter,
        cursor: _cursor,
      );
      if (!_listGuard.isLatest(seq)) return;
      tasks = [...tasks, ...page.items];
      _cursor = page.nextCursor;
      hasMore = page.hasMore;
    } on ApiException catch (exc) {
      error = exc;
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  /// 展开任务详情（加载事件时间线）；再点同一行收起
  Future<void> toggleDetail(String taskId) async {
    if (expandedTaskId == taskId) {
      collapseDetail();
      return;
    }
    expandedTaskId = taskId;
    detail = null;
    detailLoading = true;
    final seq = _detailGuard.begin();
    notifyListeners();
    try {
      final loaded = await _knowledge.getTask(taskId);
      if (!_detailGuard.isLatest(seq) || expandedTaskId != taskId) return;
      detail = loaded;
      error = null;
    } on ApiException catch (exc) {
      if (!_detailGuard.isLatest(seq) || expandedTaskId != taskId) return;
      error = exc;
    } finally {
      if (_detailGuard.isLatest(seq)) {
        detailLoading = false;
        notifyListeners();
      }
    }
  }

  void collapseDetail() {
    expandedTaskId = null;
    detail = null;
    detailLoading = false;
  }

  /// 请求取消任务（幂等）；执行中转等待取消，完成后刷新列表与详情
  Future<bool> cancelTask(String taskId) async {
    return _mutate(() async {
      await _knowledge.cancelTask(taskId);
      await reload();
      if (expandedTaskId == taskId) await _reloadDetailKeepingOpen(taskId);
      return true;
    });
  }

  /// 手动重试失败任务：创建携带父关系的新任务并展开
  Future<bool> retryTask(String taskId) async {
    return _mutate(() async {
      final created = await _knowledge.retryTask(
        taskId,
        idempotencyKey: _newKey(),
      );
      collapseDetail();
      await reload();
      await toggleDetail(created.id);
      return true;
    });
  }

  Future<void> _reloadDetailKeepingOpen(String taskId) async {
    try {
      final seq = _detailGuard.begin();
      final loaded = await _knowledge.getTask(taskId);
      if (!_detailGuard.isLatest(seq) || expandedTaskId != taskId) return;
      detail = loaded;
    } on ApiException {
      // 详情刷新失败不影响列表结果展示
    }
  }

  Timer? _autoRefreshTimer;

  /// 存在非终态任务时按固定间隔自动刷新；全部终态即停
  void _scheduleAutoRefresh() {
    _autoRefreshTimer?.cancel();
    _autoRefreshTimer = null;
    final pending = tasks.any((task) => !task.state.isTerminal);
    if (!pending) return;
    _autoRefreshTimer = Timer(const Duration(seconds: 5), () {
      unawaited(reload());
      if (expandedTaskId != null) {
        unawaited(_reloadDetailKeepingOpen(expandedTaskId!));
      }
    });
  }

  Future<bool> _mutate(Future<bool> Function() action) async {
    try {
      final result = await action();
      error = null;
      return result;
    } on ApiException catch (exc) {
      error = exc;
      notifyListeners();
      return false;
    }
  }

  @override
  void dispose() {
    _autoRefreshTimer?.cancel();
    _autoRefreshTimer = null;
    super.dispose();
  }
}

String _newKey() => const Uuid().v4();

/// 请求序号防过期响应守卫
class _LatestRequestGuard {
  int _seq = 0;
  int begin() => ++_seq;
  bool isLatest(int seq) => seq == _seq;
}
