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
}
