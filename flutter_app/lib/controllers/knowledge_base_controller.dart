import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/api_error.dart';
import '../api/dto/knowledge_dto.dart';
import '../api/knowledge_api_client.dart';
import '../app/app_preferences.dart';

/// 请求序号防过期响应守卫：每类请求独立递增，回调时比对是否仍是
/// 该类最新（并发刷新互不作废；同类仅保留最后一次响应）
class _LatestRequestGuard {
  int _seq = 0;
  int begin() => ++_seq;
  bool isLatest(int seq) => seq == _seq;
}

/// 知识库页控制器：KB 列表/选择、文档列表分页与生命周期操作
///
/// 只调用 API Client；每类请求带独立序号并忽略过期响应（UI 规范
/// §13）。失败以 [ApiException] 形式经 [error] 事件暴露给页面展示。
class KnowledgeBaseController extends ChangeNotifier {
  KnowledgeBaseController({
    required KnowledgeApiClient knowledgeClient,
    required this.preferences,
    this.onCurrentKnowledgeBase,
  }) : _knowledge = knowledgeClient;

  final KnowledgeApiClient _knowledge;
  final AppPreferences preferences;
  final void Function(String? kbName)? onCurrentKnowledgeBase;

  // 三类列表请求各自独立防过期（并发刷新互不作废）
  final _kbsGuard = _LatestRequestGuard();
  final _documentsGuard = _LatestRequestGuard();
  final _confirmationsGuard = _LatestRequestGuard();

  /// 最近一次失败的用户可见信息（页面据此展示错误条）
  ApiException? error;

  /// 知识库列表（默认隐藏已删除）
  List<KbDto> knowledgeBases = [];
  bool kbsLoading = false;

  /// 当前选中知识库（null 未选择）
  KbDto? currentKb;

  /// 当前知识库的文档列表
  List<DocumentSummary> documents = [];
  bool documentsLoading = false;
  String? _documentsCursor;
  bool documentsHasMore = false;

  /// 等待云端确认的任务（MinerU 确认横幅）
  List<TaskDto> pendingConfirmations = [];

  /// 生命周期操作进行中（按钮禁用）
  bool mutating = false;

  /// 初始化：载入 KB 列表并恢复最近选择
  Future<void> loadInitial() async {
    await reloadKnowledgeBases();
    final restoredId = preferences.lastKnowledgeBaseId;
    if (restoredId != null) {
      final match = knowledgeBases.where((kb) => kb.id == restoredId).toList();
      if (match.isNotEmpty) {
        await selectKnowledgeBase(match.first);
      }
    }
  }

  /// 重新载入知识库列表；最近选择失效时清空缓存 ID
  Future<void> reloadKnowledgeBases() async {
    kbsLoading = true;
    notifyListeners();
    try {
      final seq = _kbsGuard.begin();
      final page = await _knowledge.listKnowledgeBases(limit: 200);
      if (!_kbsGuard.isLatest(seq)) return;
      knowledgeBases = page.items;
      error = null;
    } on ApiException catch (exc) {
      error = exc;
    } finally {
      kbsLoading = false;
      notifyListeners();
    }
  }

  /// 选择知识库并载入其文档；同步顶栏与最近选择持久化
  Future<void> selectKnowledgeBase(KbDto kb) async {
    currentKb = kb;
    documents = [];
    _documentsCursor = null;
    documentsHasMore = false;
    onCurrentKnowledgeBase?.call(kb.name);
    unawaited(preferences.saveLastKnowledgeBaseId(kb.id));
    notifyListeners();
    await reloadDocuments();
    await reloadPendingConfirmations();
  }

  /// 重新载入当前知识库的文档首页
  Future<void> reloadDocuments() async {
    final kb = currentKb;
    if (kb == null) return;
    documentsLoading = true;
    notifyListeners();
    try {
      final seq = _documentsGuard.begin();
      final page = await _knowledge.listDocuments(kb.id, limit: 50);
      if (!_documentsGuard.isLatest(seq) || currentKb?.id != kb.id) return;
      documents = page.items;
      _documentsCursor = page.nextCursor;
      documentsHasMore = page.hasMore;
      error = null;
    } on ApiException catch (exc) {
      error = exc;
    } finally {
      documentsLoading = false;
      notifyListeners();
    }
    // 存在处理中任务时安排自动刷新（解析完成列表状态自动更新）
    _scheduleAutoRefresh();
  }

  Timer? _autoRefreshTimer;

  /// 存在非终态任务的文档时按固定间隔自动刷新；全部终态即停，
  /// 不产生无谓请求
  void _scheduleAutoRefresh() {
    _autoRefreshTimer?.cancel();
    _autoRefreshTimer = null;
    final pending = documents.any(
      (d) => d.latestTask != null && !d.latestTask!.state.isTerminal,
    );
    if (!pending) return;
    _autoRefreshTimer = Timer(const Duration(seconds: 5), () {
      unawaited(reloadDocuments());
    });
  }

  @override
  void dispose() {
    _autoRefreshTimer?.cancel();
    _autoRefreshTimer = null;
    super.dispose();
  }

  /// 加载更多文档（keyset 续页）
  Future<void> loadMoreDocuments() async {
    final kb = currentKb;
    if (kb == null || !documentsHasMore || documentsLoading) return;
    documentsLoading = true;
    notifyListeners();
    try {
      final seq = _documentsGuard.begin();
      final page = await _knowledge.listDocuments(
        kb.id,
        limit: 50,
        cursor: _documentsCursor,
      );
      if (!_documentsGuard.isLatest(seq) || currentKb?.id != kb.id) return;
      documents = [...documents, ...page.items];
      _documentsCursor = page.nextCursor;
      documentsHasMore = page.hasMore;
    } on ApiException catch (exc) {
      error = exc;
    } finally {
      documentsLoading = false;
      notifyListeners();
    }
  }

  /// 创建知识库；成功后刷新列表并选中
  Future<bool> createKnowledgeBase(String name, String? description) async {
    return _mutate(() async {
      final kb = await _knowledge.createKnowledgeBase(name, description: description);
      await reloadKnowledgeBases();
      await selectKnowledgeBase(kb);
      return true;
    });
  }

  /// 重命名当前知识库
  Future<bool> renameCurrentKnowledgeBase(
    String name,
    String? description,
  ) async {
    final kb = currentKb;
    if (kb == null) return false;
    return _mutate(() async {
      final updated = await _knowledge.renameKnowledgeBase(
        kb.id,
        name: name,
        description: description,
      );
      await reloadKnowledgeBases();
      if (currentKb?.id == kb.id) {
        currentKb = updated;
        onCurrentKnowledgeBase?.call(updated.name);
      }
      notifyListeners();
      return true;
    });
  }

  /// 删除当前知识库（软删除 + 清理任务）；成功后清空选择
  Future<bool> deleteCurrentKnowledgeBase() async {
    final kb = currentKb;
    if (kb == null) return false;
    return _mutate(() async {
      await _knowledge.deleteKnowledgeBase(kb.id, idempotencyKey: _newKey());
      currentKb = null;
      documents = [];
      onCurrentKnowledgeBase?.call(null);
      unawaited(preferences.saveLastKnowledgeBaseId(null));
      await reloadKnowledgeBases();
      return true;
    });
  }

  /// 创建健康检查任务（进度在任务中心可见）
  Future<bool> startHealthCheck() async {
    final kb = currentKb;
    if (kb == null) return false;
    return _mutate(() async {
      await _knowledge.createKnowledgeBaseHealthCheck(
        kb.id,
        idempotencyKey: _newKey(),
      );
      return true;
    });
  }

  /// 批量上传文档；返回逐文件结果供检查清单展示
  Future<List<UploadFileResult>?> uploadDocuments(
    List<UploadFileInput> files, {
    required String parserPreference,
    String duplicatePolicy = 'skip',
  }) async {
    final kb = currentKb;
    if (kb == null || files.isEmpty) return null;
    mutating = true;
    notifyListeners();
    try {
      final results = await _knowledge.uploadDocuments(
        kb.id,
        files: files,
        parserPreference: parserPreference,
        duplicatePolicy: duplicatePolicy,
        idempotencyKey: _newKey(),
      );
      await reloadDocuments();
      error = null;
      return results;
    } on ApiException catch (exc) {
      error = exc;
      return null;
    } finally {
      mutating = false;
      notifyListeners();
    }
  }

  /// 刷新等待云端确认的任务列表
  Future<void> reloadPendingConfirmations() async {
    try {
      final seq = _confirmationsGuard.begin();
      final page = await _knowledge.listTasks(
        state: TaskWireStatus.waitingUser.wireName,
        limit: 50,
      );
      if (!_confirmationsGuard.isLatest(seq)) return;
      pendingConfirmations = page.items;
      error = null;
    } on ApiException catch (exc) {
      error = exc;
    }
    notifyListeners();
  }

  /// 批准/拒绝单个云端确认；成功后刷新待办与文档列表
  Future<bool> resolveConfirmation(TaskDto task, {required bool approve}) async {
    return _mutate(() async {
      await _knowledge.updateCloudConfirmation(
        task.id,
        decision: approve ? 'approve' : 'reject',
      );
      await reloadPendingConfirmations();
      await reloadDocuments();
      return true;
    });
  }

  /// 重建文档索引（新索引激活前旧索引继续服务）
  Future<bool> rebuildDocument(DocumentSummary document) async {
    return _mutate(() async {
      await _knowledge.rebuildDocument(
        document.id,
        idempotencyKey: _newKey(),
      );
      await reloadDocuments();
      return true;
    });
  }

  /// 删除文档（软删除 + 物理清理任务）；删除语义幂等：目标已不
  /// 存在同样视为成功（可能被详情页或先前操作先行删除）
  Future<bool> deleteDocument(DocumentSummary document) async {
    return _mutate(() async {
      try {
        await _knowledge.deleteDocument(
          document.id,
          idempotencyKey: _newKey(),
        );
      } on ApiException catch (exc) {
        if (exc.code != 'DOCUMENT_NOT_FOUND') rethrow;
      }
      await reloadDocuments();
      return true;
    });
  }

  Future<bool> _mutate(Future<bool> Function() action) async {
    mutating = true;
    notifyListeners();
    try {
      final result = await action();
      error = null;
      return result;
    } on ApiException catch (exc) {
      error = exc;
      return false;
    } finally {
      mutating = false;
      notifyListeners();
    }
  }
}

/// 上传幂等键：每次批量一个随机 UUID（合同：批次键 + 文件序号派生）
String _newKey() => const Uuid().v4();



