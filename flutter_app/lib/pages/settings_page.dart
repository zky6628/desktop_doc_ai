import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api/ops_api_client.dart';
import '../app/app_preferences.dart';
import '../app/server_address.dart';
import '../controllers/app_shell_controller.dart';
import '../controllers/settings_controller.dart';
import '../controllers/theme_controller.dart';

/// 设置页：服务地址（含连接测试）、服务健康、运行配置、主题模式与客户端信息
///
/// 服务健康与运行配置消费后端聚合探针与无密钥配置概览（进入加载 +
/// 手动刷新），凭据只展示 configured/unconfigured，不出现密钥材料。
class SettingsPage extends StatefulWidget {
  const SettingsPage({
    super.key,
    required this.preferences,
    required this.addressStore,
    required this.themeController,
    required this.opsClient,
  });

  final AppPreferences preferences;
  final ServerAddressStore addressStore;
  final ThemeController themeController;
  final OpsApiClient opsClient;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final SettingsController _controller;
  late final TextEditingController _addressField;
  String? _addressError;

  @override
  void initState() {
    super.initState();
    _controller = SettingsController(
      preferences: widget.preferences,
      addressStore: widget.addressStore,
      opsClient: widget.opsClient,
    );
    _addressField = TextEditingController(
      text: widget.addressStore.value.toString(),
    );
    _controller.loadInitial();
  }

  @override
  void dispose() {
    _addressField.dispose();
    _controller.dispose();
    super.dispose();
  }

  void _saveAddress() {
    final error = _controller.saveAddress(_addressField.text);
    setState(() => _addressError = error);
    if (error != null) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        const SnackBar(content: Text('服务地址已保存，后续请求立即生效')),
      );
  }

  void _restoreDefault() {
    _controller.restoreDefaultAddress();
    _addressField.text = AppPreferences.defaultServerBaseUrl;
    setState(() => _addressError = null);
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(const SnackBar(content: Text('已恢复默认服务地址')));
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([_controller, widget.themeController]),
      builder: (context, _) {
        return ListView(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
          children: [
            const _SectionTitle('服务连接'),
            _buildConnectionCard(context),
            const SizedBox(height: 16),
            const _SectionTitle('服务健康'),
            _buildHealthCard(context),
            const SizedBox(height: 16),
            const _SectionTitle('运行配置'),
            _buildRuntimeConfigCard(context),
            const SizedBox(height: 16),
            const _SectionTitle('外观'),
            _buildAppearanceCard(context),
            const SizedBox(height: 16),
            const _SectionTitle('客户端信息'),
            _buildClientInfoCard(context),
          ],
        );
      },
    );
  }

  Widget _buildConnectionCard(BuildContext context) {
    final controller = _controller;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _addressField,
              decoration: InputDecoration(
                labelText: '服务地址',
                hintText: AppPreferences.defaultServerBaseUrl,
                errorText: _addressError,
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                ElevatedButton(onPressed: _saveAddress, child: const Text('保存')),
                const SizedBox(width: 12),
                OutlinedButton(
                  onPressed: _restoreDefault,
                  child: const Text('恢复默认'),
                ),
                const SizedBox(width: 12),
                OutlinedButton(
                  onPressed: controller.probeState == ConnectionProbeState.testing
                      ? null
                      : controller.testConnection,
                  child: const Text('测试连接'),
                ),
              ],
            ),
            if (controller.probeState != ConnectionProbeState.idle) ...[
              const SizedBox(height: 12),
              _buildProbeStatus(controller),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildProbeStatus(SettingsController controller) {
    final colors = Theme.of(context).colorScheme;
    switch (controller.probeState) {
      case ConnectionProbeState.idle:
        return const SizedBox.shrink();
      case ConnectionProbeState.testing:
        return const Row(
          children: [
            SizedBox(
              width: 14,
              height: 14,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
            SizedBox(width: 8),
            Text('正在测试连接…', style: TextStyle(fontSize: 13)),
          ],
        );
      case ConnectionProbeState.success:
        return Row(
          children: [
            Icon(Icons.check_circle, size: 16, color: colors.primary),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                controller.probeMessage ?? '',
                style: const TextStyle(fontSize: 13),
              ),
            ),
          ],
        );
      case ConnectionProbeState.failure:
        return Row(
          children: [
            Icon(Icons.error_outline, size: 16, color: colors.error),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                controller.probeMessage ?? '',
                style: const TextStyle(fontSize: 13),
              ),
            ),
          ],
        );
    }
  }

  // 组件状态的展示文案（state：ok/error/stopped）
  static const _componentLabels = {
    'sqlite': '数据库',
    'chroma': '向量库',
    'fts': '全文索引',
    'worker': '解析任务',
  };

  // 配置摘要键的展示文案（服务端新增键未映射时原样展示）
  static const _summaryKeyLabels = {
    'vector_top_k': '向量召回数',
    'keyword_top_k': '关键词召回数',
    'fused_top_k': '融合候选数',
    'rrf_k': 'RRF 常数',
    'parent_chunk_chars': '父切片字符预算',
    'child_chunk_chars': '子切片字符预算',
    'model': '模型',
    'dimensions': '向量维度',
    'top_n': '重排输出数',
    'max_output_tokens': '输出上限',
  };

  Widget _buildHealthCard(BuildContext context) {
    final controller = _controller;
    final colors = Theme.of(context).colorScheme;
    if (controller.healthLoading && controller.health == null) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Center(
            child: SizedBox(
              width: 22,
              height: 22,
              child: CircularProgressIndicator(strokeWidth: 2.4),
            ),
          ),
        ),
      );
    }
    if (controller.healthError != null && controller.health == null) {
      return Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '无法获取服务健康状态：${controller.healthError!.message}',
                style: TextStyle(fontSize: 13, color: colors.error),
              ),
              const SizedBox(height: 8),
              TextButton.icon(
                onPressed: () => unawaited(controller.refreshHealth()),
                icon: const Icon(Icons.refresh, size: 16),
                label: const Text('重试'),
              ),
            ],
          ),
        ),
      );
    }
    final health = controller.health;
    if (health == null) {
      return const SizedBox.shrink();
    }
    final healthy = health.status == 'healthy';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(
                  healthy ? Icons.check_circle : Icons.warning_amber_outlined,
                  size: 18,
                  color: healthy ? colors.primary : colors.tertiary,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    healthy ? '运行正常' : '组件降级：${health.degraded.join('、')}',
                    style: const TextStyle(fontSize: 14),
                  ),
                ),
                IconButton(
                  tooltip: '刷新健康状态',
                  icon: const Icon(Icons.refresh, size: 18),
                  onPressed: controller.healthLoading
                      ? null
                      : () => unawaited(controller.refreshHealth()),
                ),
              ],
            ),
            const SizedBox(height: 8),
            for (final component in health.components) ...[
              _HealthRow(
                label: _componentLabels[component.name] ?? component.name,
                value: switch (component.state) {
                  'ok' => '正常',
                  'stopped' => '未启用',
                  _ => '异常',
                },
                valueColor: switch (component.state) {
                  'ok' => colors.primary,
                  'stopped' => colors.onSurfaceVariant,
                  _ => colors.error,
                },
              ),
            ],
            _HealthRow(
              label: '解析凭据',
              value: health.providers
                  .map(
                    (provider) =>
                        '${provider.name}${provider.configured ? ' 已配置' : ' 未配置'}',
                  )
                  .join(' · '),
            ),
            _HealthRow(
              label: '任务队列',
              value:
                  '运行 ${health.queue.running} / 排队 ${health.queue.pending}'
                  '（上限 ${health.queue.capacityRunning} / ${health.queue.capacityPending}）',
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildRuntimeConfigCard(BuildContext context) {
    final controller = _controller;
    final colors = Theme.of(context).colorScheme;
    if (controller.configLoading && controller.publicConfig == null) {
      return const Card(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: Center(
            child: SizedBox(
              width: 22,
              height: 22,
              child: CircularProgressIndicator(strokeWidth: 2.4),
            ),
          ),
        ),
      );
    }
    if (controller.configError != null && controller.publicConfig == null) {
      return Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '无法获取运行配置：${controller.configError!.message}',
                style: TextStyle(fontSize: 13, color: colors.error),
              ),
              const SizedBox(height: 8),
              TextButton.icon(
                onPressed: () => unawaited(controller.refreshConfig()),
                icon: const Icon(Icons.refresh, size: 16),
                label: const Text('重试'),
              ),
            ],
          ),
        ),
      );
    }
    final config = controller.publicConfig;
    if (config == null) {
      return const SizedBox.shrink();
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Expanded(
                  child: Text('模型', style: TextStyle(fontSize: 14)),
                ),
                IconButton(
                  tooltip: '刷新运行配置',
                  icon: const Icon(Icons.refresh, size: 18),
                  onPressed: controller.configLoading
                      ? null
                      : () => unawaited(controller.refreshConfig()),
                ),
              ],
            ),
            for (final profile in config.modelProfiles)
              _HealthRow(
                label: switch (profile.role) {
                  'embedding' => '向量化',
                  'rerank' => '重排',
                  'generation' => '生成',
                  _ => profile.role,
                },
                value: '${profile.provider} / ${profile.modelName}',
              ),
            const Divider(),
            for (final pipeline in config.pipelineConfigs) ...[
              Padding(
                padding: const EdgeInsets.only(top: 8, bottom: 2),
                child: Text(
                  '配置 · ${pipeline.configType} v${pipeline.version}',
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              for (final entry in pipeline.summary.entries)
                _HealthRow(
                  label: _summaryKeyLabels[entry.key] ?? entry.key,
                  value: '${entry.value}',
                ),
            ],
            const Divider(),
            _HealthRow(
              label: '队列容量',
              value:
                  '执行 ${config.limits.maxRunning} / 排队 ${config.limits.maxPending}'
                  ' / 非终态 ${config.limits.maxNonTerminal}',
            ),
            _HealthRow(
              label: '上传限制',
              value:
                  '单文件 ${config.limits.maxFileMb} MB / 单批 ${config.limits.maxBatchFiles} 个',
            ),
            // 调试能力关闭时不展示开关行（未开启的能力不占配置版面）
            if (config.features.localDebugEnabled)
              _HealthRow(label: '本地调试检索', value: '已开启'),
          ],
        ),
      ),
    );
  }

  Widget _buildAppearanceCard(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('主题模式', style: TextStyle(fontSize: 14)),
            const SizedBox(height: 12),
            SegmentedButton<ThemeMode>(
              segments: const [
                ButtonSegment(value: ThemeMode.system, label: Text('跟随系统')),
                ButtonSegment(value: ThemeMode.light, label: Text('亮色')),
                ButtonSegment(value: ThemeMode.dark, label: Text('暗色')),
              ],
              selected: {widget.themeController.mode},
              onSelectionChanged: (selection) =>
                  unawaited(widget.themeController.setMode(selection.first)),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildClientInfoCard(BuildContext context) {
    final shell = context.watch<AppShellController>();
    final size = MediaQuery.sizeOf(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        child: Column(
          children: [
            _InfoRow(
              label: '实例标识',
              child: SelectableText(
                widget.preferences.clientInstanceId,
                style: const TextStyle(fontSize: 13),
              ),
            ),
            const Divider(),
            _InfoRow(
              label: '最近知识库',
              child: Text(
                shell.currentKnowledgeBaseName ?? '未记录',
                style: const TextStyle(fontSize: 13),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            const Divider(),
            _InfoRow(
              label: '窗口尺寸',
              child: Text(
                '${size.width.round()} x ${size.height.round()}',
                style: const TextStyle(fontSize: 13),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle(this.title);

  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Text(
        title,
        style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 96,
            child: Text(
              label,
              style: TextStyle(
                fontSize: 13,
                color: Theme.of(context).hintColor,
              ),
            ),
          ),
          Expanded(child: child),
        ],
      ),
    );
  }
}

/// 健康与配置卡片的紧凑信息行（标签定宽 + 值文本，值可着色）
class _HealthRow extends StatelessWidget {
  const _HealthRow({
    required this.label,
    required this.value,
    this.valueColor,
  });

  final String label;
  final String value;
  final Color? valueColor;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 96,
            child: Text(
              label,
              style: TextStyle(
                fontSize: 13,
                color: Theme.of(context).hintColor,
              ),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: TextStyle(fontSize: 13, color: valueColor),
            ),
          ),
        ],
      ),
    );
  }
}
