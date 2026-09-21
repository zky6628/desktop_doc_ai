import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../app/app_preferences.dart';
import '../app/server_address.dart';
import '../controllers/app_shell_controller.dart';
import '../controllers/settings_controller.dart';
import '../controllers/theme_controller.dart';

/// 设置页：服务地址（含连接测试）、主题模式与客户端信息
///
/// 服务端侧的组件健康、模型 Profile、密钥状态与数据目录维护依赖
/// 后端 /health 与 /config/public 端点（合同已定义、尚未实现），
/// 对应分组待端点落地后补充。
class SettingsPage extends StatefulWidget {
  const SettingsPage({
    super.key,
    required this.preferences,
    required this.addressStore,
    required this.themeController,
  });

  final AppPreferences preferences;
  final ServerAddressStore addressStore;
  final ThemeController themeController;

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
    );
    _addressField = TextEditingController(
      text: widget.addressStore.value.toString(),
    );
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
