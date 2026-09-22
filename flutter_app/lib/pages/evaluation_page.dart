import 'dart:async';

import 'package:flutter/material.dart';

import '../api/dto/metrics_dto.dart';
import '../api/dto/ops_dto.dart';
import '../api/knowledge_api_client.dart';
import '../api/ops_api_client.dart';
import '../api/query_api_client.dart';
import '../controllers/evaluation_controller.dart';

/// 评测页（只读）：查询聚合指标、TTFT 分位、token 用量与本地调试检索
///
/// 指标数据只来自 GET /metrics/queries（唯一既有聚合端点）；分段 P95、
/// 失败案例与 Query Trace 浏览缺承载端点，待合同补齐后扩展。
/// 指标为复盘数据：进入加载，更新由手动刷新驱动（无自动轮询）。
/// 调试检索区域由服务端运行配置开关驱动显隐（本地调试关闭时不渲染），
/// 候选仅含定位事实与各阶段分数，不含正文。
class EvaluationPage extends StatefulWidget {
  const EvaluationPage({
    super.key,
    required this.queryClient,
    required this.knowledgeClient,
    required this.opsClient,
    this.initialKbId,
  });

  final QueryApiClient queryClient;
  final KnowledgeApiClient knowledgeClient;
  final OpsApiClient opsClient;

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
      opsClient: widget.opsClient,
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
    // 调试检索区域独立于指标状态：开关开启即渲染（空库也可先行调参）
    final debugCard = controller.debugAvailable == true
        ? _DebugSearchCard(controller: controller)
        : null;
    // 切片参数与评测运行为常驻区域（调参是评测行为的一部分）
    final chunkingCard = _ChunkingCard(controller: controller);
    final evaluationCard = _EvaluationRunCard(controller: controller);
    if (metrics == null) return const SizedBox.shrink();
    if (metrics.total == 0) {
      return ListView(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
        children: [
          chunkingCard,
          const SizedBox(height: 16),
          evaluationCard,
          const SizedBox(height: 16),
          if (debugCard != null) ...[debugCard, const SizedBox(height: 16)],
          SizedBox(height: 420, child: _EmptyView()),
        ],
      );
    }
    return _MetricsView(
      metrics: metrics,
      debugCard: debugCard,
      chunkingCard: chunkingCard,
      evaluationCard: evaluationCard,
    );
  }
}

/// 概览/分位/用量三组指标卡（Wrap 布局适配窄窗口换行）
class _MetricsView extends StatelessWidget {
  const _MetricsView({
    required this.metrics,
    this.debugCard,
    this.chunkingCard,
    this.evaluationCard,
  });

  final QueryMetricsSummary metrics;

  /// 调试检索卡片（开关开启时由页面传入，置于指标卡之前）
  final Widget? debugCard;

  /// 切片参数与评测运行卡片（常驻区域，置于页面顶部）
  final Widget? chunkingCard;
  final Widget? evaluationCard;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
      children: [
        if (chunkingCard != null) ...[chunkingCard!, const SizedBox(height: 16)],
        if (evaluationCard != null) ...[evaluationCard!, const SizedBox(height: 16)],
        if (debugCard != null) ...[debugCard!, const SizedBox(height: 16)],
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

/// 本地调试检索卡片：问题输入、Top-K 高级覆盖折叠项与候选明细表
///
/// 交互语义：需在工具栏选定具体知识库（"全部知识库"时检索禁用）；
/// 覆盖参数留空即沿用服务端配置值；结果表仅含定位事实与各阶段分数，
/// 不含切片正文。
class _DebugSearchCard extends StatefulWidget {
  const _DebugSearchCard({required this.controller});

  final EvaluationController controller;

  @override
  State<_DebugSearchCard> createState() => _DebugSearchCardState();
}

class _DebugSearchCardState extends State<_DebugSearchCard> {
  final TextEditingController _questionField = TextEditingController();
  final TextEditingController _vectorTopK = TextEditingController();
  final TextEditingController _keywordTopK = TextEditingController();
  final TextEditingController _fusedTopK = TextEditingController();
  final TextEditingController _rerankTopN = TextEditingController();

  @override
  void dispose() {
    _questionField.dispose();
    _vectorTopK.dispose();
    _keywordTopK.dispose();
    _fusedTopK.dispose();
    _rerankTopN.dispose();
    super.dispose();
  }

  /// 覆盖输入解析：空白视为沿用默认；非法数字保持 null 由服务端校验
  int? _parseOverride(TextEditingController controller) =>
      int.tryParse(controller.text.trim());

  void _run() {
    unawaited(
      widget.controller.runDebugSearch(
        question: _questionField.text,
        vectorTopK: _parseOverride(_vectorTopK),
        keywordTopK: _parseOverride(_keywordTopK),
        fusedTopK: _parseOverride(_fusedTopK),
        rerankTopN: _parseOverride(_rerankTopN),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    final colors = Theme.of(context).colorScheme;
    final kbSelected = controller.kbFilter != null;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.science_outlined, size: 18),
                const SizedBox(width: 8),
                const Expanded(
                  child: Text(
                    '调试检索',
                    style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
                  ),
                ),
                if (!kbSelected)
                  Text(
                    '请先在上方选择具体知识库',
                    style: TextStyle(fontSize: 12, color: colors.tertiary),
                  ),
              ],
            ),
            const SizedBox(height: 12),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: TextField(
                    controller: _questionField,
                    enabled: kbSelected && !controller.debugLoading,
                    minLines: 1,
                    maxLines: 3,
                    textInputAction: TextInputAction.search,
                    onSubmitted: (_) => kbSelected ? _run() : null,
                    decoration: const InputDecoration(
                      hintText: '输入调试问题（不产生查询记录与指标）',
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: ElevatedButton.icon(
                    onPressed: kbSelected && !controller.debugLoading
                        ? _run
                        : null,
                    icon: controller.debugLoading
                        ? const SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.search, size: 18),
                    label: const Text('检索'),
                  ),
                ),
              ],
            ),
            Theme(
              data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
              child: ExpansionTile(
                tilePadding: EdgeInsets.zero,
                childrenPadding: EdgeInsets.zero,
                title: const Text(
                  'Top-K 覆盖（留空沿用服务端配置）',
                  style: TextStyle(fontSize: 13),
                ),
                children: [
                  Row(
                    children: [
                      _expanded(
                        _vectorTopK,
                        '向量召回 vector_top_k',
                      ),
                      const SizedBox(width: 8),
                      _expanded(
                        _keywordTopK,
                        '关键词召回 keyword_top_k',
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      _expanded(_fusedTopK, '融合候选 fused_top_k'),
                      const SizedBox(width: 8),
                      _expanded(_rerankTopN, '重排输出 rerank_top_n'),
                    ],
                  ),
                ],
              ),
            ),
            if (controller.debugError != null) ...[
              const SizedBox(height: 8),
              Text(
                '检索失败：${controller.debugError!.message}（${controller.debugError!.code}）',
                style: TextStyle(fontSize: 13, color: colors.error),
              ),
            ],
            if (controller.debugResult != null)
              _DebugResultView(result: controller.debugResult!),
          ],
        ),
      ),
    );
  }

  Widget _expanded(TextEditingController controller, String label) => Expanded(
        child: TextField(
          controller: controller,
          keyboardType: TextInputType.number,
          decoration: InputDecoration(
            labelText: label,
            isDense: true,
            border: const OutlineInputBorder(),
          ),
        ),
      );
}

/// 调试检索结果：阶段规模摘要 + 候选明细表（横向滚动，零正文）
class _DebugResultView extends StatelessWidget {
  const _DebugResultView({required this.result});

  final SearchDebugResult result;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final stages = result.stages;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const SizedBox(height: 12),
        Wrap(
          spacing: 16,
          runSpacing: 4,
          children: [
            _stageChip(theme, '向量 ${stages.vectorHits}'),
            _stageChip(theme, '关键词 ${stages.keywordHits}'),
            _stageChip(theme, '融合 ${stages.fused}'),
            _stageChip(
              theme,
              stages.rerankDegraded ? '重排降级' : '重排 ${stages.reranked}',
              highlight: stages.rerankDegraded,
            ),
            if (stages.droppedHitCount > 0)
              _stageChip(theme, '丢弃 ${stages.droppedHitCount}'),
            for (final entry in result.overridden.entries)
              _stageChip(theme, '${entry.key}=${entry.value}'),
          ],
        ),
        const SizedBox(height: 8),
        if (result.candidates.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 16),
            child: Text('无候选命中', style: TextStyle(fontSize: 13)),
          )
        else
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: DataTable(
              headingTextStyle: const TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w600,
              ),
              dataTextStyle: const TextStyle(fontSize: 12),
              columnSpacing: 18,
              columns: const [
                DataColumn(label: Text('#')),
                DataColumn(label: Text('文件')),
                DataColumn(label: Text('页')),
                DataColumn(label: Text('章节')),
                DataColumn(label: Text('向量')),
                DataColumn(label: Text('关键词')),
                DataColumn(label: Text('RRF')),
                DataColumn(label: Text('重排')),
              ],
              rows: [
                for (final candidate in result.candidates)
                  DataRow(
                    cells: [
                      DataCell(Text('${candidate.rrfRank}')),
                      DataCell(
                        SizedBox(
                          width: 160,
                          child: Text(
                            candidate.fileName ?? '—',
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ),
                      DataCell(Text(candidate.pageNo?.toString() ?? '—')),
                      DataCell(
                        SizedBox(
                          width: 140,
                          child: Text(
                            candidate.sectionPath.isEmpty
                                ? '—'
                                : candidate.sectionPath.join(' / '),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ),
                      DataCell(Text(_route(candidate.vectorRank, candidate.vectorScore))),
                      DataCell(Text(_route(candidate.keywordRank, candidate.keywordScore))),
                      DataCell(Text(_score(candidate.rrfScore))),
                      DataCell(
                        Text(_route(candidate.rerankRank, candidate.rerankScore)),
                      ),
                    ],
                  ),
              ],
            ),
          ),
      ],
    );
  }

  Widget _stageChip(ThemeData theme, String text, {bool highlight = false}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: highlight
            ? theme.colorScheme.errorContainer.withValues(alpha: 0.4)
            : theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(text, style: const TextStyle(fontSize: 12)),
    );
  }

  /// 单路展示：未命中显示占位，命中显示 rank@score
  String _route(int? rank, num? score) {
    if (rank == null) return '—';
    return '$rank @ ${_score(score)}';
  }

  String _score(num? score) =>
      score == null ? '—' : score.toStringAsFixed(4);
}

/// 在役切片参数卡片：读取当前生效值并支持覆盖保存（写入即对后续导入/重建生效）
class _ChunkingCard extends StatefulWidget {
  const _ChunkingCard({required this.controller});

  final EvaluationController controller;

  @override
  State<_ChunkingCard> createState() => _ChunkingCardState();
}

class _ChunkingCardState extends State<_ChunkingCard> {
  final _parentField = TextEditingController();
  final _childField = TextEditingController();
  bool _initialized = false;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_syncFromServer);
  }

  void _syncFromServer() {
    final config = widget.controller.chunkingConfig;
    if (config == null || _initialized) return;
    _initialized = true;
    _parentField.text = '${config.parentChunkChars}';
    _childField.text = '${config.childChunkChars}';
  }

  @override
  void dispose() {
    widget.controller.removeListener(_syncFromServer);
    _parentField.dispose();
    _childField.dispose();
    super.dispose();
  }

  void _save() {
    final parent = int.tryParse(_parentField.text.trim());
    final child = int.tryParse(_childField.text.trim());
    if (parent == null || child == null) return;
    unawaited(
      widget.controller.saveChunkingParams(
        parentChunkChars: parent,
        childChunkChars: child,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    final config = controller.chunkingConfig;
    final colors = Theme.of(context).colorScheme;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.content_cut, size: 18),
                const SizedBox(width: 8),
                const Text(
                  '切片参数',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
                ),
                const SizedBox(width: 8),
                if (config != null && config.isDefault)
                  Text(
                    '当前为默认值',
                    style: TextStyle(fontSize: 12, color: colors.onSurfaceVariant),
                  ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              '写入即对后续导入与索引重建生效；参数变化将产生新配置版本与索引版本',
              style: TextStyle(fontSize: 12, color: colors.onSurfaceVariant),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                SizedBox(
                  width: 140,
                  child: TextField(
                    controller: _parentField,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: '父切片预算',
                      hintText: '1200',
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                SizedBox(
                  width: 140,
                  child: TextField(
                    controller: _childField,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: '子切片预算',
                      hintText: '400',
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const Spacer(),
                FilledButton.tonal(
                  onPressed: controller.chunkingSaving ? null : _save,
                  child: Text(controller.chunkingSaving ? '保存中' : '保存'),
                ),
              ],
            ),
            if (controller.chunkingError != null) ...[
              const SizedBox(height: 8),
              Text(
                '${controller.chunkingError!.message}（${controller.chunkingError!.code}）',
                style: TextStyle(fontSize: 12, color: colors.error),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// 切片参数对比评测卡片：参数组编辑、问题集输入、进度与对比结果
class _EvaluationRunCard extends StatefulWidget {
  const _EvaluationRunCard({required this.controller});

  final EvaluationController controller;

  @override
  State<_EvaluationRunCard> createState() => _EvaluationRunCardState();
}

class _EvaluationRunCardState extends State<_EvaluationRunCard> {
  /// 参数组编辑行：parent/child 文本控制器对
  final List<(TextEditingController, TextEditingController)> _groupFields = [
    (TextEditingController(text: '800'), TextEditingController(text: '300')),
    (TextEditingController(text: '1200'), TextEditingController(text: '400')),
    (TextEditingController(text: '1600'), TextEditingController(text: '400')),
  ];
  final _questionsField = TextEditingController();

  @override
  void dispose() {
    for (final (parent, child) in _groupFields) {
      parent.dispose();
      child.dispose();
    }
    _questionsField.dispose();
    super.dispose();
  }

  void _addGroup() {
    if (_groupFields.length >= 5) return;
    setState(() {
      _groupFields.add((
        TextEditingController(text: '1200'),
        TextEditingController(text: '400'),
      ));
    });
  }

  void _removeGroup(int index) {
    setState(() {
      final removed = _groupFields.removeAt(index);
      removed.$1.dispose();
      removed.$2.dispose();
    });
  }

  void _start() {
    final questions = [
      for (final line in _questionsField.value.text.split('\n'))
        if (line.trim().isNotEmpty) line.trim(),
    ];
    final groups = <ChunkingParams>[];
    for (final (parent, child) in _groupFields) {
      final parentValue = int.tryParse(parent.text.trim());
      final childValue = int.tryParse(child.text.trim());
      if (parentValue != null && childValue != null) {
        groups.add(
          ChunkingParams(
            parentChunkChars: parentValue,
            childChunkChars: childValue,
          ),
        );
      }
    }
    if (questions.isEmpty || groups.isEmpty) return;
    unawaited(
      widget.controller.startEvaluationRun(
        questions: questions,
        paramGroups: groups,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    final colors = Theme.of(context).colorScheme;
    final run = controller.evaluationRun;
    final running = run?.isRunning == true;
    final kbSelected = controller.kbFilter != null;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.science, size: 18),
                const SizedBox(width: 8),
                const Text(
                  '切片参数评测',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
                ),
                const SizedBox(width: 8),
                if (!kbSelected)
                  Text(
                    '请先在上方选择具体知识库',
                    style: TextStyle(fontSize: 12, color: colors.tertiary),
                  ),
                const Spacer(),
                if (running)
                  FilledButton.tonalIcon(
                    onPressed: () =>
                        unawaited(controller.cancelEvaluationRun()),
                    icon: const Icon(Icons.cancel_outlined, size: 16),
                    label: const Text('取消'),
                  )
                else
                  FilledButton.icon(
                    onPressed:
                        (kbSelected && !controller.evaluationSubmitting)
                            ? _start
                            : null,
                    icon: controller.evaluationSubmitting
                        ? const SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.play_arrow, size: 16),
                    label: const Text('开始评测'),
                  ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              '每组参数将重建所选知识库索引后跑一遍问题集（每组产生一次全量嵌入调用）；'
              '评测结束后自动恢复原参数并重建',
              style: TextStyle(fontSize: 12, color: colors.onSurfaceVariant),
            ),
            const SizedBox(height: 12),
            ..._buildGroupEditors(colors, running),
            const SizedBox(height: 8),
            TextField(
              controller: _questionsField,
              enabled: !running,
              minLines: 3,
              maxLines: 6,
              decoration: const InputDecoration(
                labelText: '问题集（每行一个问题，最多 200 行）',
                border: OutlineInputBorder(),
                isDense: true,
              ),
            ),
            if (controller.evaluationError != null) ...[
              const SizedBox(height: 8),
              Text(
                '${controller.evaluationError!.message}（${controller.evaluationError!.code}）',
                style: TextStyle(fontSize: 12, color: colors.error),
              ),
            ],
            if (run != null) ...[
              const SizedBox(height: 12),
              _RunProgress(run: run),
            ],
            if (run != null && run.results != null && run.results!.isNotEmpty)
              _ComparisonTable(results: run.results!),
          ],
        ),
      ),
    );
  }

  List<Widget> _buildGroupEditors(ColorScheme colors, bool running) {
    return [
      for (var index = 0; index < _groupFields.length; index++) ...[
        Row(
          children: [
            Text('组 ${index + 1}', style: const TextStyle(fontSize: 12)),
            const SizedBox(width: 8),
            Expanded(
              child: TextField(
                controller: _groupFields[index].$1,
                enabled: !running,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(
                  labelText: '父预算',
                  isDense: true,
                  border: OutlineInputBorder(),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: TextField(
                controller: _groupFields[index].$2,
                enabled: !running,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(
                  labelText: '子预算',
                  isDense: true,
                  border: OutlineInputBorder(),
                ),
              ),
            ),
            IconButton(
              tooltip: '删除该组',
              onPressed:
                  running || _groupFields.length <= 1
                      ? null
                      : () => _removeGroup(index),
              icon: const Icon(Icons.remove_circle_outline, size: 18),
            ),
          ],
        ),
        if (index == _groupFields.length - 1)
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed:
                  running || _groupFields.length >= 5 ? null : _addGroup,
              icon: const Icon(Icons.add, size: 16),
              label: const Text('添加参数组（最多 5 组）'),
            ),
          ),
      ],
    ];
  }
}

/// 评测运行进度行（组/问题指针与终态徽标）
class _RunProgress extends StatelessWidget {
  const _RunProgress({required this.run});

  final EvaluationRun run;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final (label, color) = switch (run.state) {
      'running' => (
        '进行中：第 ${((run.currentGroupIndex ?? 0) + 1)} 组 /'
            ' 第 ${((run.currentQuestionIndex ?? 0) + 1)} 问',
        colors.primary,
      ),
      'completed' => ('已完成', colors.primary),
      'cancelled' => ('已取消', colors.onSurfaceVariant),
      'failed' => ('失败（${run.errorCode ?? "INTERNAL_ERROR"}）', colors.error),
      _ => (run.state, colors.onSurfaceVariant),
    };
    return Row(
      children: [
        SizedBox(
          width: 14,
          height: 14,
          child:
              run.isRunning
                  ? const CircularProgressIndicator(strokeWidth: 2)
                  : null,
        ),
        const SizedBox(width: 8),
        Text(label, style: TextStyle(fontSize: 12, color: color)),
        const SizedBox(width: 8),
        Text(
          '共 ${run.paramGroups.length} 组 × ${run.questions.length} 问',
          style: TextStyle(fontSize: 12, color: colors.onSurfaceVariant),
        ),
      ],
    );
  }
}

/// 参数组对比表：一行一组，列为关键指标（横向滚动适配窄窗口）
class _ComparisonTable extends StatelessWidget {
  const _ComparisonTable({required this.results});

  final List<EvaluationGroupMetrics> results;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: DataTable(
        headingTextStyle: TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.w600,
          color: colors.onSurface,
        ),
        dataTextStyle: TextStyle(fontSize: 12, color: colors.onSurface),
        columnSpacing: 24,
        columns: const [
          DataColumn(label: Text('参数组 (父/子)')),
          DataColumn(label: Text('完成')),
          DataColumn(label: Text('拒答')),
          DataColumn(label: Text('TTFT P50')),
          DataColumn(label: Text('TTFT P95')),
          DataColumn(label: Text('输入 tokens')),
          DataColumn(label: Text('输出 tokens')),
          DataColumn(label: Text('重排降级')),
        ],
        rows: [
          for (final result in results)
            DataRow(
              cells: [
                DataCell(
                  Text('${result.group.parentChunkChars}/${result.group.childChunkChars}'),
                ),
                DataCell(Text('${result.completed ?? "—"} / ${result.total ?? "—"}')),
                DataCell(Text('${result.refused ?? "—"}')),
                DataCell(Text(_ms(result.ttftP50Ms))),
                DataCell(Text(_ms(result.ttftP95Ms))),
                DataCell(Text('${result.inputTokens ?? "—"}')),
                DataCell(Text('${result.outputTokens ?? "—"}')),
                DataCell(Text('${result.rerankDegraded ?? "—"}')),
              ],
            ),
        ],
      ),
    );
  }

  String _ms(int? value) => value == null ? '—' : '$value ms';
}
