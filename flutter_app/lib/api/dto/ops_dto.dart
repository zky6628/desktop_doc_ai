// 服务健康与运行配置（/health、/config/public 响应 data）
//
// 服务端健康为轻量聚合探针：status 为三态（healthy/degraded/
// unavailable 由错误信封表达），组件降级是数据而非错误；运行配置
// 只含无密钥的展示事实。

/// 单组件健康行（state：ok/error/stopped；provider 行为凭据存在性）
class HealthComponent {
  const HealthComponent({
    required this.name,
    required this.state,
    this.detail,
  });

  final String name;

  /// ok / error / stopped（worker 专属）；凭据组件固定 ok
  final String state;

  /// 补充说明（当前恒为 null，保留展示位）
  final String? detail;

  factory HealthComponent.fromJson(String name, Map<String, dynamic> json) {
    final state = json['state'];
    return HealthComponent(
      name: name,
      state: state is String ? state : 'ok',
      detail: json['detail'] is String ? json['detail'] as String : null,
    );
  }
}

/// 凭据配置行：只表达 configured/unconfigured，不验证有效性
class ProviderCredential {
  const ProviderCredential({required this.name, required this.configured});

  final String name;
  final bool configured;
}

/// 队列水位与容量常量
class HealthQueue {
  const HealthQueue({
    required this.running,
    required this.pending,
    required this.capacityRunning,
    required this.capacityPending,
    required this.capacityNonTerminal,
  });

  final int running;
  final int pending;
  final int capacityRunning;
  final int capacityPending;
  final int capacityNonTerminal;

  factory HealthQueue.fromJson(Map<String, dynamic> json) => HealthQueue(
        running: _intOrZero(json['running']),
        pending: _intOrZero(json['pending']),
        capacityRunning: _intOrZero(json['capacity_running']),
        capacityPending: _intOrZero(json['capacity_pending']),
        capacityNonTerminal: _intOrZero(json['capacity_non_terminal']),
      );

  static int _intOrZero(dynamic value) =>
      value == null ? 0 : (value as num).toInt();
}

/// 聚合健康快照
class ServiceHealth {
  const ServiceHealth({
    required this.status,
    required this.components,
    required this.providers,
    required this.queue,
    required this.degraded,
  });

  /// healthy / degraded（组件降级集合见 [degraded]）
  final String status;
  final List<HealthComponent> components;
  final List<ProviderCredential> providers;
  final HealthQueue queue;

  /// 降级组件名列表（服务端推导，客户端直接展示）
  final List<String> degraded;

  factory ServiceHealth.fromJson(Map<String, dynamic> json) {
    final rawComponents = json['components'];
    final components = <HealthComponent>[];
    final providers = <ProviderCredential>[];
    if (rawComponents is Map<String, dynamic>) {
      for (final entry in rawComponents.entries) {
        if (entry.key == 'providers') {
          final rawProviders = entry.value;
          if (rawProviders is Map<String, dynamic>) {
            for (final provider in rawProviders.entries) {
              final configured =
                  provider.value is Map<String, dynamic>
                      ? provider.value['configured']
                      : null;
              providers.add(
                ProviderCredential(
                  name: provider.key,
                  configured: configured == true,
                ),
              );
            }
          }
          continue;
        }
        final value = entry.value;
        if (value is Map<String, dynamic>) {
          components.add(HealthComponent.fromJson(entry.key, value));
        }
      }
    }
    final rawDegraded = json['degraded'];
    return ServiceHealth(
      status: json['status'] is String ? json['status'] as String : 'healthy',
      components: components,
      providers: providers,
      queue: HealthQueue.fromJson(
        json['queue'] is Map<String, dynamic>
            ? json['queue'] as Map<String, dynamic>
            : const {},
      ),
      degraded: [
        if (rawDegraded is List)
          for (final item in rawDegraded)
            if (item is String) item,
      ],
    );
  }
}

/// 在役模型身份行（无密钥：角色/供应方/模型名）
class ModelProfileView {
  const ModelProfileView({
    required this.role,
    required this.provider,
    required this.modelName,
  });

  final String role;
  final String provider;
  final String modelName;

  factory ModelProfileView.fromJson(Map<String, dynamic> json) =>
      ModelProfileView(
        role: json['role'] is String ? json['role'] as String : '',
        provider: json['provider'] is String ? json['provider'] as String : '',
        modelName: json['model_name'] is String
            ? json['model_name'] as String
            : '',
      );
}

/// 在役配置摘要行（config_summary 为服务端白名单键值，键集随类型固定）
class PipelineConfigView {
  const PipelineConfigView({
    required this.configType,
    required this.version,
    required this.summary,
  });

  final String configType;
  final int version;

  /// 白名单摘要键值对（展示层直接遍历，键集变化免前端改动）
  final Map<String, Object> summary;

  factory PipelineConfigView.fromJson(Map<String, dynamic> json) =>
      PipelineConfigView(
        configType:
            json['config_type'] is String ? json['config_type'] as String : '',
        version: json['version'] is int ? json['version'] as int : 0,
        summary: {
          if (json['config_summary'] is Map<String, dynamic>)
            for (final entry
                in (json['config_summary'] as Map<String, dynamic>).entries)
              if (entry.value is num || entry.value is String)
                entry.key: entry.value as Object,
        },
      );
}

/// 容量与功能开关
class RuntimeLimits {
  const RuntimeLimits({
    required this.maxRunning,
    required this.maxPending,
    required this.maxNonTerminal,
    required this.maxFileMb,
    required this.maxBatchFiles,
  });

  final int maxRunning;
  final int maxPending;
  final int maxNonTerminal;
  final int maxFileMb;
  final int maxBatchFiles;

  factory RuntimeLimits.fromJson(Map<String, dynamic> json) => RuntimeLimits(
        maxRunning: _intOrZero(json['max_running']),
        maxPending: _intOrZero(json['max_pending']),
        maxNonTerminal: _intOrZero(json['max_non_terminal']),
        maxFileMb: _intOrZero(json['max_file_mb']),
        maxBatchFiles: _intOrZero(json['max_batch_files']),
      );

  static int _intOrZero(dynamic value) =>
      value == null ? 0 : (value as num).toInt();
}

/// 功能开关（服务端运行形态声明，客户端据此显隐调试入口）
class RuntimeFeatures {
  const RuntimeFeatures({
    required this.workerEnabled,
    required this.localDebugEnabled,
    required this.cloudParsingAvailable,
  });

  final bool workerEnabled;
  final bool localDebugEnabled;
  final bool cloudParsingAvailable;

  factory RuntimeFeatures.fromJson(Map<String, dynamic> json) =>
      RuntimeFeatures(
        workerEnabled: json['worker_enabled'] == true,
        localDebugEnabled: json['local_debug_enabled'] == true,
        cloudParsingAvailable: json['cloud_parsing_available'] == true,
      );
}

/// 无密钥运行配置概览
class PublicConfig {
  const PublicConfig({
    required this.modelProfiles,
    required this.pipelineConfigs,
    required this.limits,
    required this.features,
  });

  final List<ModelProfileView> modelProfiles;
  final List<PipelineConfigView> pipelineConfigs;
  final RuntimeLimits limits;
  final RuntimeFeatures features;

  factory PublicConfig.fromJson(Map<String, dynamic> json) => PublicConfig(
        modelProfiles: [
          if (json['model_profiles'] is List)
            for (final item in json['model_profiles'] as List)
              if (item is Map<String, dynamic>) ModelProfileView.fromJson(item),
        ],
        pipelineConfigs: [
          if (json['pipeline_configs'] is List)
            for (final item in json['pipeline_configs'] as List)
              if (item is Map<String, dynamic>)
                PipelineConfigView.fromJson(item),
        ],
        limits: RuntimeLimits.fromJson(
          json['limits'] is Map<String, dynamic>
              ? json['limits'] as Map<String, dynamic>
              : const {},
        ),
        features: RuntimeFeatures.fromJson(
          json['features'] is Map<String, dynamic>
              ? json['features'] as Map<String, dynamic>
              : const {},
        ),
      );
}

/// 调试检索候选：定位事实与各阶段分数（无正文，正文走文档块端点）
class SearchDebugCandidate {
  const SearchDebugCandidate({
    required this.chunkId,
    required this.documentVersionId,
    required this.rrfRank,
    required this.rrfScore,
    this.documentId,
    this.fileName,
    this.versionNo,
    this.pageNo,
    this.sectionPath = const [],
    this.vectorRank,
    this.vectorScore,
    this.keywordRank,
    this.keywordScore,
    this.rerankRank,
    this.rerankScore,
  });

  final String chunkId;
  final String documentVersionId;

  /// 单路字段为空表示该候选未在该路命中；rerank 在降级时为空
  final String? documentId;
  final String? fileName;
  final int? versionNo;
  final int? pageNo;
  final List<String> sectionPath;
  final int? vectorRank;
  final num? vectorScore;
  final int? keywordRank;
  final num? keywordScore;
  final int rrfRank;
  final num rrfScore;
  final int? rerankRank;
  final num? rerankScore;

  factory SearchDebugCandidate.fromJson(Map<String, dynamic> json) =>
      SearchDebugCandidate(
        chunkId: json['chunk_id'] is String ? json['chunk_id'] as String : '',
        documentVersionId: json['document_version_id'] is String
            ? json['document_version_id'] as String
            : '',
        rrfRank: json['rrf_rank'] is int ? json['rrf_rank'] as int : 0,
        rrfScore: json['rrf_score'] is num ? json['rrf_score'] as num : 0,
        documentId: json['document_id'] as String?,
        fileName: json['file_name'] as String?,
        versionNo: json['version_no'] as int?,
        pageNo: json['page_no'] as int?,
        sectionPath: [
          if (json['section_path'] is List)
            for (final item in json['section_path'] as List)
              if (item is String) item,
        ],
        vectorRank: json['vector_rank'] as int?,
        vectorScore: json['vector_score'] as num?,
        keywordRank: json['keyword_rank'] as int?,
        keywordScore: json['keyword_score'] as num?,
        rerankRank: json['rerank_rank'] as int?,
        rerankScore: json['rerank_score'] as num?,
      );
}

/// 调试检索各阶段规模（含丢弃计数供索引漂移观察）
class SearchDebugStages {
  const SearchDebugStages({
    required this.vectorHits,
    required this.keywordHits,
    required this.fused,
    required this.reranked,
    required this.rerankDegraded,
    required this.droppedHitCount,
  });

  final int vectorHits;
  final int keywordHits;
  final int fused;
  final int reranked;
  final bool rerankDegraded;
  final int droppedHitCount;

  factory SearchDebugStages.fromJson(Map<String, dynamic> json) =>
      SearchDebugStages(
        vectorHits: json['vector_hits'] is int ? json['vector_hits'] as int : 0,
        keywordHits:
            json['keyword_hits'] is int ? json['keyword_hits'] as int : 0,
        fused: json['fused'] is int ? json['fused'] as int : 0,
        reranked: json['reranked'] is int ? json['reranked'] as int : 0,
        rerankDegraded: json['rerank_degraded'] == true,
        droppedHitCount:
            json['dropped_hit_count'] is int ? json['dropped_hit_count'] as int : 0,
      );
}

/// 调试检索结果：候选明细、阶段规模与实际生效的覆盖参数
class SearchDebugResult {
  const SearchDebugResult({
    required this.candidates,
    required this.stages,
    required this.overridden,
  });

  final List<SearchDebugCandidate> candidates;
  final SearchDebugStages stages;

  /// 非空键值 = 本次请求实际覆盖的参数（覆盖名 → 覆盖值）
  final Map<String, Object> overridden;

  factory SearchDebugResult.fromJson(Map<String, dynamic> json) =>
      SearchDebugResult(
        candidates: [
          if (json['candidates'] is List)
            for (final item in json['candidates'] as List)
              if (item is Map<String, dynamic>)
                SearchDebugCandidate.fromJson(item),
        ],
        stages: SearchDebugStages.fromJson(
          json['stages'] is Map<String, dynamic>
              ? json['stages'] as Map<String, dynamic>
              : const {},
        ),
        overridden: {
          if (json['config'] is Map<String, dynamic> &&
              json['config']['overridden'] is Map<String, dynamic>)
            for (final entry
                in (json['config']['overridden'] as Map<String, dynamic>)
                    .entries)
              if (entry.value is num || entry.value is String)
                entry.key: entry.value as Object,
        },
      );
}
