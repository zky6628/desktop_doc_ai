/// 查询聚合指标（GET /api/v1/metrics/queries 响应 data）
///
/// 分位与失败率在无成功样本/无运行时为 null，展示层以占位符表达；
/// 分位仅统计 completed 且非拒答的成功样本（07 契约口径）。
class QueryMetricsSummary {
  const QueryMetricsSummary({
    required this.p50TtftMs,
    required this.p95TtftMs,
    required this.p99TtftMs,
    required this.ttftSampleSize,
    required this.total,
    required this.completed,
    required this.failed,
    required this.cancelled,
    required this.refused,
    required this.degraded,
    required this.failureRate,
    required this.inputTokens,
    required this.outputTokens,
  });

  final int? p50TtftMs;
  final int? p95TtftMs;
  final int? p99TtftMs;

  /// 成功延迟分位的样本数
  final int ttftSampleSize;

  /// 全部运行计数（含排队/生成中等非终态）
  final int total;
  final int completed;
  final int failed;
  final int cancelled;

  /// 证据不足拒答的运行数
  final int refused;

  /// 重排降级（回退 RRF 前 5）的运行数
  final int degraded;

  /// 失败率（无运行时为 null）
  final double? failureRate;
  final int inputTokens;
  final int outputTokens;

  factory QueryMetricsSummary.fromJson(Map<String, dynamic> json) =>
      QueryMetricsSummary(
        p50TtftMs: _nullableInt(json['p50_ttft_ms']),
        p95TtftMs: _nullableInt(json['p95_ttft_ms']),
        p99TtftMs: _nullableInt(json['p99_ttft_ms']),
        ttftSampleSize: _intOrZero(json['ttft_sample_size']),
        total: _intOrZero(json['total']),
        completed: _intOrZero(json['completed']),
        failed: _intOrZero(json['failed']),
        cancelled: _intOrZero(json['cancelled']),
        refused: _intOrZero(json['refused']),
        degraded: _intOrZero(json['degraded']),
        failureRate: (json['failure_rate'] as num?)?.toDouble(),
        inputTokens: _intOrZero(json['input_tokens']),
        outputTokens: _intOrZero(json['output_tokens']),
      );

  static int? _nullableInt(dynamic value) =>
      value == null ? null : (value as num).toInt();

  static int _intOrZero(dynamic value) =>
      value == null ? 0 : (value as num).toInt();
}
