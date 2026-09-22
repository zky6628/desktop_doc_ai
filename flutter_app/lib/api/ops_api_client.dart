import 'dart:convert';

import 'package:http/http.dart' as http;

import 'api_client_base.dart';
import 'dto/ops_dto.dart';
import '../app/server_address.dart';

/// 系统域 API 客户端：聚合健康探针、无密钥运行配置与本地调试检索
///
/// 服务端组件降级以数据表达（200 + degraded 列表），仅数据库不可达
/// 走 503 错误信封；信封与错误解析复用 [ApiClientBase]。
class OpsApiClient {
  OpsApiClient({
    http.Client? client,
    required ServerAddressStore address,
  }) : _base = ApiClientBase(client: client, address: address);

  final ApiClientBase _base;

  /// 聚合健康：组件状态、队列水位与凭据配置状态
  Future<ServiceHealth> getHealth() async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/health')),
    );
    return ServiceHealth.fromJson(_base.dataOf(response));
  }

  /// 无密钥运行配置：模型身份、在役配置摘要、容量与功能开关
  Future<PublicConfig> getPublicConfig() async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/config/public')),
    );
    return PublicConfig.fromJson(_base.dataOf(response));
  }

  /// 本地调试检索：Top-K 覆盖为 null 时沿用服务端配置值；服务端在
  /// 调试开关未开启时整体拒绝（422），此处不预判
  Future<SearchDebugResult> debugSearch({
    required String knowledgeBaseId,
    required String question,
    int? vectorTopK,
    int? keywordTopK,
    int? fusedTopK,
    int? rerankTopN,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/search'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'knowledge_base_id': knowledgeBaseId,
          'question': question,
          'vector_top_k': ?vectorTopK,
          'keyword_top_k': ?keywordTopK,
          'fused_top_k': ?fusedTopK,
          'rerank_top_n': ?rerankTopN,
        }),
      ),
    );
    return SearchDebugResult.fromJson(_base.dataOf(response));
  }

  /// 在役切片参数（当前生效值与是否为默认预算）
  Future<ChunkingConfig> getChunkingConfig() async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/config/chunking')),
    );
    return ChunkingConfig.fromJson(_base.dataOf(response));
  }

  /// 写入在役切片参数（写入即对后续导入/重建生效；非法值 422）
  Future<ChunkingConfig> putChunkingConfig({
    required int parentChunkChars,
    required int childChunkChars,
  }) async {
    final response = await _base.send(
      (client) => client.put(
        _base.uri('/api/v1/config/chunking'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'parent_chunk_chars': parentChunkChars,
          'child_chunk_chars': childChunkChars,
        }),
      ),
    );
    return ChunkingConfig.fromJson(_base.dataOf(response));
  }

  /// 创建切片参数对比评测运行（服务端独占校验，冲突 409）
  Future<EvaluationRun> createEvaluationRun({
    required String knowledgeBaseId,
    required List<String> questions,
    required List<ChunkingParams> paramGroups,
  }) async {
    final response = await _base.send(
      (client) => client.post(
        _base.uri('/api/v1/evaluation-runs'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'knowledge_base_id': knowledgeBaseId,
          'questions': questions,
          'param_groups': [
            for (final group in paramGroups)
              {
                'parent_chunk_chars': group.parentChunkChars,
                'child_chunk_chars': group.childChunkChars,
              },
          ],
        }),
      ),
    );
    return EvaluationRun.fromJson(_base.dataOf(response));
  }

  /// 读取评测运行（状态、进度与对比结果）
  Future<EvaluationRun> getEvaluationRun(String runId) async {
    final response = await _base.send(
      (client) => client.get(_base.uri('/api/v1/evaluation-runs/$runId')),
    );
    return EvaluationRun.fromJson(_base.dataOf(response));
  }

  /// 请求取消评测运行（幂等）
  Future<EvaluationRun> cancelEvaluationRun(String runId) async {
    final response = await _base.send(
      (client) =>
          client.post(_base.uri('/api/v1/evaluation-runs/$runId/cancel')),
    );
    return EvaluationRun.fromJson(_base.dataOf(response));
  }
}
