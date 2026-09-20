import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../api/dto/knowledge_dto.dart';
import '../api/knowledge_api_client.dart';
import '../controllers/task_center_controller.dart';
import '../widgets/knowledge/confirm_dialogs.dart';

/// 任务中心：全局队列、筛选（写入 URL query）、事件时间线与取消/重试
///
/// 操作规则（UI 规范 §8）：非终态可取消（执行中显示"正在请求取消"，
/// 不提前标为已取消）；仅失败任务可重试（展示父子关系）；终态只读。
class TaskCenterPage extends StatefulWidget {
  const TaskCenterPage({
    super.key,
    required this.knowledgeClient,
    this.initialState,
    this.initialTaskType,
  });

  final KnowledgeApiClient knowledgeClient;
  final String? initialState;
  final String? initialTaskType;

  @override
  State<TaskCenterPage> createState() => _TaskCenterPageState();
}

class _TaskCenterPageState extends State<TaskCenterPage> {
  late final TaskCenterController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TaskCenterController(knowledgeClient: widget.knowledgeClient);
    _controller.loadInitial(
      state: widget.initialState,
      taskType: widget.initialTaskType,
    );
  }

  @override
  void didUpdateWidget(TaskCenterPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    // 路由 query 变化（浏览器前进/后退或地址跳转）时同步筛选
    if (widget.initialState != oldWidget.initialState ||
        widget.initialTaskType != oldWidget.initialTaskType) {
      _controller.applyFilters(
        state: widget.initialState,
        taskType: widget.initialTaskType,
      );
    }
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
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 12, 20, 8),
              child: Row(
                children: [
                  const Text(
                    '任务中心',
                    style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(width: 16),
                  _stateFilterDropdown(context),
                  const SizedBox(width: 12),
                  _typeFilterDropdown(context),
                  const Spacer(),
                  IconButton(
                    tooltip: '刷新',
                    icon: const Icon(Icons.refresh, size: 20),
                    onPressed: controller.loading ? null : controller.reload,
                  ),
                ],
              ),
            ),
            if (controller.error != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                child: Text(
                  '加载失败：${controller.error!.message}',
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            const Divider(height: 1),
            Expanded(child: _buildBody(context)),
          ],
        );
      },
    );
  }

  Widget _buildBody(BuildContext context) {
    final controller = _controller;
    if (controller.loading && controller.tasks.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (controller.tasks.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(
              Icons.task_alt,
              size: 56,
              color: Theme.of(
                context,
              ).colorScheme.onSurface.withValues(alpha: 0.25),
            ),
            const SizedBox(height: 12),
            const Text('当前筛选下没有任务', style: TextStyle(fontSize: 14)),
          ],
        ),
      );
    }
    return SizedBox(
      // 任务表格列宽固定：窄窗口下横向滚动，不挤压重排（UI 规范 §3）
      height: double.infinity,
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: SizedBox(
          width: 1080,
          child: ListView.builder(
            itemCount: controller.tasks.length + (controller.hasMore ? 1 : 0),
            itemBuilder: (context, index) {
              if (index == controller.tasks.length) {
                return TextButton(
                  onPressed: controller.loading ? null : controller.loadMore,
                  child: const Text('加载更多'),
                );
              }
              final task = controller.tasks[index];
              return Column(
                children: [
                  _buildTaskRow(context, task),
                  if (controller.expandedTaskId == task.id)
                    _buildDetailPanel(context, task.id),
                  const Divider(height: 1),
                ],
              );
            },
          ),
        ),
      ),
    );
  }

  Widget _buildTaskRow(BuildContext context, TaskDto task) {
    final controller = _controller;
    final expanded = controller.expandedTaskId == task.id;
    return InkWell(
      onTap: () => controller.toggleDetail(task.id),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
        child: Row(
          children: [
            _stateDot(task.state),
            const SizedBox(width: 10),
            SizedBox(
              width: 110,
              child: Text(
                task.taskType,
                style: const TextStyle(fontSize: 13),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Expanded(
              flex: 4,
              child: Text(
                task.documentDisplayName ?? task.knowledgeBaseId ?? task.id,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 14),
              ),
            ),
            SizedBox(
              width: 130,
              child: Text(
                task.state.zhLabel +
                    (task.state == TaskWireStatus.cancelRequested
                        ? '…'
                        : ''),
                style: const TextStyle(fontSize: 13),
              ),
            ),
            SizedBox(
              width: 90,
              child: Text(
                task.stage ?? '-',
                style: const TextStyle(fontSize: 12),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            SizedBox(
              width: 110,
              child: Tooltip(
                message: '进度 ${(task.progress * 100).toStringAsFixed(0)}%',
                child: LinearProgressIndicator(
                  value: task.state.isTerminal
                      ? (task.state == TaskWireStatus.failed ? 1 : task.progress)
                      : (task.progress <= 0 ? null : task.progress),
                  minHeight: 6,
                ),
              ),
            ),
            SizedBox(
              width: 70,
              child: Text(
                task.queuePosition?.toString() ?? '-',
                style: const TextStyle(fontSize: 13),
              ),
            ),
            SizedBox(
              width: 90,
              child: Text(
                _shortTime(task.createdAt),
                style: const TextStyle(fontSize: 12),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              flex: 2,
              child: Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  if (task.retryable)
                    TextButton(
                      onPressed: controller.loading
                          ? null
                          : () => unawaited(controller.retryTask(task.id)),
                      child: const Text('重试'),
                    ),
                  if (task.cancellable)
                    TextButton(
                      onPressed: controller.loading
                          ? null
                          : () => unawaited(_cancelFlow(task)),
                      child: const Text('取消'),
                    ),
                  Icon(
                    expanded ? Icons.expand_less : Icons.expand_more,
                    size: 18,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildDetailPanel(BuildContext context, String taskId) {
    final controller = _controller;
    final detail = controller.detail;
    if (controller.detailLoading || detail == null) {
      return const Padding(
        padding: EdgeInsets.all(16),
        child: Center(child: CircularProgressIndicator()),
      );
    }
    final task = detail.task;
    return Container(
      color: Theme.of(context).colorScheme.surface,
      padding: const EdgeInsets.fromLTRB(24, 12, 24, 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 16,
            runSpacing: 4,
            children: [
              Text('任务 ID：${task.id}', style: const TextStyle(fontSize: 12)),
              if (task.parentTaskId != null)
                Text(
                  '重试自：${task.parentTaskId}',
                  style: const TextStyle(fontSize: 12),
                ),
              if (task.documentVersionId != null)
                Text(
                  '文档版本：${task.documentVersionId}',
                  style: const TextStyle(fontSize: 12),
                ),
              Text(
                '尝试：${task.retryCount}/${task.maxRetries}'
                '（阶段第 ${task.stageAttempt} 次）',
                style: const TextStyle(fontSize: 12),
              ),
              if (task.error != null)
                Text(
                  '${task.error!.code}: ${task.error!.message}',
                  style: TextStyle(
                    fontSize: 12,
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
            ],
          ),
          const SizedBox(height: 10),
          const Text('事件时间线', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
          for (final event in detail.recentEvents)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(
                    width: 150,
                    child: Text(
                      _shortTime(event.createdAt),
                      style: const TextStyle(fontSize: 12),
                    ),
                  ),
                  SizedBox(
                    width: 120,
                    child: Text(
                      event.eventType,
                      style: const TextStyle(fontSize: 12),
                    ),
                  ),
                  Expanded(
                    child: Text(
                      [
                        event.state,
                        if (event.stage != null) event.stage!,
                        if (event.errorCode != null) event.errorCode!,
                        if (event.detailJson != null &&
                            event.detailJson!.length <= 120)
                          event.detailJson!,
                      ].join(' · '),
                      style: const TextStyle(fontSize: 12),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _stateDot(TaskWireStatus state) {
    final color = switch (state) {
      TaskWireStatus.succeeded => const Color(0xFF22C55E),
      TaskWireStatus.failed => const Color(0xFFEF4444),
      TaskWireStatus.cancelled => Colors.grey,
      TaskWireStatus.cancelRequested || TaskWireStatus.retryWaiting ||
      TaskWireStatus.waitingExternal => const Color(0xFFF59E0B),
      TaskWireStatus.waitingUser => const Color(0xFF3B82F6),
      _ => const Color(0xFFF59E0B),
    };
    return Icon(Icons.circle, size: 10, color: color);
  }

  Widget _stateFilterDropdown(BuildContext context) {
    final controller = _controller;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Text('状态', style: _filterLabelStyle),
        const SizedBox(width: 6),
        DropdownButton<String?>(
          value: controller.stateFilter,
          isDense: true,
          hint: const Text('全部'),
          items: [
            const DropdownMenuItem<String?>(value: null, child: Text('全部')),
            for (final state in TaskWireStatus.values)
              DropdownMenuItem<String?>(
                value: state.wireName,
                child: Text(state.zhLabel),
              ),
          ],
          onChanged: (value) => _applyFiltersAndSyncUrl(
            state: value,
            taskType: controller.taskTypeFilter,
          ),
        ),
      ],
    );
  }

  Widget _typeFilterDropdown(BuildContext context) {
    const types = {
      'import': '导入',
      'rebuild_index': '索引重建',
      'delete_kb': '知识库删除',
      'delete_document': '文档删除',
      'health_check': '健康检查',
      'cleanup': '补偿清理',
    };
    final controller = _controller;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Text('类型', style: _filterLabelStyle),
        const SizedBox(width: 6),
        DropdownButton<String?>(
          value: controller.taskTypeFilter,
          isDense: true,
          hint: const Text('全部'),
          items: [
            const DropdownMenuItem<String?>(value: null, child: Text('全部')),
            for (final entry in types.entries)
              DropdownMenuItem<String?>(
                value: entry.key,
                child: Text(entry.value),
              ),
          ],
          onChanged: (value) => _applyFiltersAndSyncUrl(
            state: controller.stateFilter,
            taskType: value,
          ),
        ),
      ],
    );
  }

  /// 筛选写入 URL query（UI 规范 §8），路由重建驱动控制器应用
  void _applyFiltersAndSyncUrl({String? state, String? taskType}) {
    final params = <String, String>{'state': ?state, 'task_type': ?taskType};
    context.go(
      Uri(path: '/tasks', queryParameters: params).toString(),
    );
  }

  Future<void> _cancelFlow(TaskDto task) async {
    final confirmed = await showDestructiveConfirmDialog(
      context,
      title: '取消任务',
      confirmVerb: '取消任务',
      infoLines: [
        '目标：${task.documentDisplayName ?? task.taskType}（${task.id}）',
        '排队/等待中的任务将立即取消；执行中的任务在安全检查点停止',
        '已生成的中间产物按恢复语义保留',
      ],
    );
    if (confirmed) await _controller.cancelTask(task.id);
  }
}

const _filterLabelStyle = TextStyle(fontSize: 13);

String _shortTime(String isoTime) {
  final time = DateTime.tryParse(isoTime);
  if (time == null) return isoTime;
  return '${time.month.toString().padLeft(2, '0')}-'
      '${time.day.toString().padLeft(2, '0')} '
      '${time.hour.toString().padLeft(2, '0')}:'
      '${time.minute.toString().padLeft(2, '0')}';
}
