import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../api/api_error.dart';
import '../api/dto/knowledge_dto.dart';
import '../api/knowledge_api_client.dart';

/// 文档详情控制器：详情/版本/解析块与生命周期操作
///
/// 只调用 API Client；请求序号防过期响应（UI 规范 §13）。
class DocumentDetailController extends ChangeNotifier {
  DocumentDetailController({
    required KnowledgeApiClient knowledgeClient,
    required this.documentId,
  }) : _knowledge = knowledgeClient;

  final KnowledgeApiClient _knowledge;
  final String documentId;
  int _seq = 0;

  ApiException? error;
  DocumentDetail? detail;
  List<VersionDto> versions = [];
  List<BlockPreview> blocks = [];
  bool loading = false;
  bool mutating = false;

  /// 初始加载：详情 + 版本历史 + 活动版本解析块
  Future<void> load() async {
    loading = true;
    final seq = ++_seq;
    notifyListeners();
    try {
      final detail = await _knowledge.getDocument(documentId);
      final versions = await _knowledge.listDocumentVersions(documentId);
      final blocks = await _knowledge.listDocumentBlocks(documentId, limit: 200);
      if (seq != _seq) return;
      this.detail = detail;
      this.versions = versions.items;
      this.blocks = blocks.items;
      error = null;
    } on ApiException catch (exc) {
      if (seq != _seq) return;
      error = exc;
    } finally {
      if (seq == _seq) {
        loading = false;
        notifyListeners();
      }
    }
  }

  /// 替换文档内容；成功后重载（失败时旧活动版本继续服务）
  Future<bool> replaceFile(UploadFileInput file) async {
    return _mutate(() async {
      await _knowledge.replaceDocument(
        documentId,
        file: file,
        idempotencyKey: _newKey(),
      );
      await load();
      return true;
    });
  }

  /// 重建索引（新索引激活前旧索引继续服务）
  Future<bool> rebuildIndex() async {
    return _mutate(() async {
      await _knowledge.rebuildDocument(documentId, idempotencyKey: _newKey());
      await load();
      return true;
    });
  }

  /// 删除文档（软删除 + 物理清理任务）
  ///
  /// 删除语义幂等：目标已不存在（他人/先前操作已删除）同样视为
  /// 成功，由页面导航返回列表；成功后文档不再存在，不做已删文档
  /// 的详情刷新
  Future<bool> deleteDocument() async {
    return _mutate(() async {
      try {
        await _knowledge.deleteDocument(documentId, idempotencyKey: _newKey());
      } on ApiException catch (exc) {
        if (exc.code != 'DOCUMENT_NOT_FOUND') rethrow;
      }
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

String _newKey() => const Uuid().v4();
