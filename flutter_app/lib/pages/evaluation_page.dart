import 'dart:async';

import 'package:flutter/material.dart';

import '../api/dto/metrics_dto.dart';
import '../api/knowledge_api_client.dart';
import '../api/query_api_client.dart';
import '../controllers/evaluation_controller.dart';

/// 评测页（只读）：查询聚合指标、TTFT 分位与 token 用量
///
/// 数据只来自 GET /metrics/queries（唯一既有聚合端点）；分段 P95、
/// 失败案例与 Query Trace 浏览缺承载端点，待合同补齐后扩展。
/// 指标为复盘数据：进入加载，更新由手动刷新驱动（无自动轮询）。
class EvaluationPage extends StatefulWidget {
  const EvaluationPage({
    super.key,
    required this.queryClient,
    required this.knowledgeClient,
    this.initialKbId,
  });

  final QueryApiClient queryClient;
  final KnowledgeApiClient knowledgeClient;

  /// 路由 query 恢复的知识库筛选（/evaluation?kb=）
  final String? initialKbId;

  @override
  State<EvaluationPage> createState() => _EvaluationPageState();
}

class _EvaluationPageState extends State<EvaluationPage> {
  late final EvaluationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = EvaluationController(
      queryClient: widget.queryClient,
      knowledgeClient: widget.knowledgeClient,
      kbFilter: widget.initialKbId,
    );
    _controller.loadInitial();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _controller,
      builder: (context, _) {
        final controller = _controller;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _buildToolbar(controller),
            if (controller.loading && controller.metrics != null)
              const LinearProgressIndicator(minHeight: 2),
            Expanded(child: _buildBody(controller)),
          ],
        );
      },
    );
  }

  /// 筛选下拉的"全部知识库"哨兵值（DropdownButton 空值显示 hint）
  static const _allKbValue = '';

  Widget _buildToolbar(EvaluationController controller) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 8),
      child: Row(
        children: [
          const Text(
            '评测',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
          ),
          const SizedBox(width: 16),
          SizedBox(
            width: 240,
            child: InputDecorator(
              decoration: const InputDecoration(labelText: '知识库'),
              child: DropdownButtonHideUnderline(
                child: DropdownButton<String>(
                  value: controller.kbFilter ?? _allKbValue,
                  isExpanded: true,
                  isDense: true,
                  items: [
                    const DropdownMenuItem<String>(
                      value: _allKbValue,
                      child: Text('全部知识库', overflow: TextOverflow.ellipsis),
                    ),
                    for (final kb in controller.knowledgeBases)
                      DropdownMenuItem<String>(
                        value: kb.id,
                        child: Text(kb.name, overflow: TextOverflow.ellipsis),
                      ),
                  ],
                  onChanged: (value) => unawaited(
                    _controller.setKbFilter(value == _allKbValue ? null : value),
                  ),
                ),
              ),
            ),
          ),
          const Spacer(),
          IconButton(
            tooltip: '刷新',
            onPressed: controller.loading ? null : () => _controller.refresh(),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
    );
  }

  Widget _buildBody(EvaluationController controller) {
    if (controller.loading && controller.metrics == null) {
      return const Center(child: CircularProgressIndicator());
    }
    final error = controller.error;
    final metrics = controller.metrics;
    if (error != null && metrics == null) {
      return _ErrorRetryView(
        message: '${error.message}（${error.code}）',
        onRetry: () => _controller.refresh(),
      );
    }
    if (metrics == null) return const SizedBox.shrink();
    if (metrics.total == 0) return const _EmptyView();
    return _MetricsView(metrics: metrics);
  }
}

/// 概览/分位/用量三组指标卡（Wrap 布局适配窄窗口换行）
class _MetricsView extends StatelessWidget {
  const _MetricsView({required this.metrics});

  final QueryMetricsSummary metrics;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  '运行概览',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 12,
                  runSpacing: 12,
                  children: [
                    _StatTile(value: '${metrics.total}', label: '总查询'),
                    _StatTile(value: '${metrics.completed}', label: '成功'),
                    _StatTile(value: '${metrics.failed}', label: '失败'),
                    _StatTile(value: '${metrics.cancelled}', label: '已取消'),
                    _StatTile(value: '${metrics.refused}', label: '拒答'),
                    _StatTile(value: '${metrics.degraded}', label: '重排降级'),
                    _StatTile(value: _failureRate, label: '失败率'),
                  ],
                ),
              ],
            ),
          ),
        ),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'TTFT 首字延迟（服务端）',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 12,
                  runSpacing: 12,
                  children: [
                    _StatTile(value: _ms(metrics.p50TtftMs), label: 'P50'),
                    _StatTile(value: _ms(metrics.p95TtftMs), label: 'P95'),
                    _StatTile(value: _ms(metrics.p99TtftMs), label: 'P99'),
                    _StatTile(value: '${metrics.ttftSampleSize}', label: '成功样本数'),
                  ],
                ),
              ],
            ),
          ),
        ),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Token 用量',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 12,
                  runSpacing: 12,
                  children: [
                    _StatTile(value: '${metrics.inputTokens}', label: '输入合计'),
                    _StatTile(value: '${metrics.outputTokens}', label: '输出合计'),
                  ],
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  String get _failureRate {
    final rate = metrics.failureRate;
    if (rate == null) return '—';
    return '${(rate * 100).toStringAsFixed(1)}%';
  }

  static String _ms(int? value) => value == null ? '—' : '$value ms';
}

/// 单个指标值块：稳定尺寸，数值缺失以占位符表达
class _StatTile extends StatelessWidget {
  const _StatTile({required this.value, required this.label});

  final String value;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 128,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        border: Border.all(color: Theme.of(context).dividerColor),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            value,
            style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 4),
          Text(
            label,
            style: TextStyle(
              fontSize: 12,
              color: Theme.of(context).hintColor,
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyView extends StatelessWidget {
  const _EmptyView();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            Icons.insights_outlined,
            size: 56,
            color: theme.colorScheme.onSurface.withValues(alpha: 0.25),
          ),
          const SizedBox(height: 16),
          Text('暂无查询运行记录', style: theme.textTheme.titleLarge),
          const SizedBox(height: 8),
          Text(
            '在问答页提问后，此处展示查询指标与延迟分位',
            style: theme.textTheme.bodyMedium?.copyWith(
              color: theme.colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

class _ErrorRetryView extends StatelessWidget {
  const _ErrorRetryView({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.error_outline, size: 48),
          const SizedBox(height: 12),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 13),
            ),
          ),
          const SizedBox(height: 16),
          OutlinedButton.icon(
            onPressed: onRetry,
            icon: const Icon(Icons.refresh),
            label: const Text('重试'),
          ),
        ],
      ),
    );
  }
}
