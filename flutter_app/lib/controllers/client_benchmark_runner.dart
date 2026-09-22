import 'dart:async';

import 'package:flutter/foundation.dart';

/// 生成链路抽象：评测跑题驱动问答所需的最小接口
///
/// [ChatController] 经 [ChatGenerationHarness] 适配；测试以替身实现。
/// 时间戳快照三元组为 (queryId, 发送时刻, SSE 首 token 时刻, 首帧
/// 渲染时刻)，三时间戳齐全才有值。
abstract interface class GenerationHarness {
  /// 是否有生成任务进行中（评测循环以它的下沿判定单题结束）
  bool get generating;

  /// 当前知识库 ID（未解析时为 null）
  String? get knowledgeBaseId;

  /// 清空当前会话进入草稿态（保证逐条独立问答）
  void newConversation();

  /// 经真实链路发送问题（创建查询 + SSE 流式 + 遥测上报）
  Future<void> send(String question);

  /// 切换评测目标知识库
  Future<void> loadKnowledgeBase(String kbId);

  /// 最近一次生成的遥测快照（单题结束后读取）
  (String, DateTime, DateTime, DateTime)? get lastTelemetry;

  /// 监听生成状态变化（单题结束检测依赖 generating 下沿）
  void addListener(void Function() listener);

  /// 移除生成状态监听
  void removeListener(void Function() listener);
}

/// [ChatController] 的评测适配器：不改动控制器既有签名
class ChatGenerationHarness implements GenerationHarness {
  ChatGenerationHarness(this._controller);

  final dynamic _controller;

  @override
  bool get generating => _controller.generating as bool;

  @override
  String? get knowledgeBaseId =>
      _controller.knowledgeBase?.id as String?;

  @override
  void newConversation() => _controller.newConversation();

  @override
  Future<void> send(String question) => _controller.send(question);

  @override
  Future<void> loadKnowledgeBase(String kbId) =>
      _controller.loadInitial(kbId: kbId);

  @override
  (String, DateTime, DateTime, DateTime)? get lastTelemetry =>
      _controller.lastTelemetry as (String, DateTime, DateTime, DateTime)?;

  @override
  void addListener(void Function() listener) =>
      _controller.addListener(listener);

  @override
  void removeListener(void Function() listener) =>
      _controller.removeListener(listener);
}

/// 单题评测记录
class BenchmarkRecord {
  const BenchmarkRecord({
    required this.question,
    required this.state,
    this.queryId,
    this.clientTtftMs,
    this.detail,
  });

  final String question;

  /// completed / no_telemetry / error / timeout
  final String state;
  final String? queryId;

  /// 客户端 TTFT：首帧渲染时刻 − 发送时刻（毫秒）
  final int? clientTtftMs;
  final String? detail;
}

/// 客户端 TTFT 评测跑题器：问题集逐条经真实问答链路执行
///
/// 单题流程：新建会话（独立问答）→ 发送 → 等待 generating 下沿 →
/// 读取遥测快照记录客户端 TTFT。发送为触发式（内部流式消费），
/// 单题结束以 generating 翻转为 false 为准。串行执行保证延迟数据
/// 互不干扰；单题超时即终止整批（此时生成可能仍在途，继续跑会
/// 被禁发逻辑拦截）。
class ClientBenchmarkRunner extends ChangeNotifier {
  ClientBenchmarkRunner({required this.harness});

  final GenerationHarness harness;

  /// 单题超时（秒）：生成卡死兜底
  static const _questionTimeout = Duration(seconds: 300);

  bool running = false;
  bool stopRequested = false;
  int currentIndex = 0;
  int total = 0;
  String? error;
  final List<BenchmarkRecord> records = [];

  bool _awaitingCompletion = false;

  /// 客户端 TTFT 分位（毫秒；仅含有效遥测记录）
  Map<String, int?> summaryPercentiles() {
    final ttfts =
        records.map((r) => r.clientTtftMs).whereType<int>().toList()..sort();
    int? pick(double ratio) => ttfts.isEmpty
        ? null
        : ttfts[(ratio * (ttfts.length - 1)).round()];
    return {
      'samples': ttfts.length,
      'p50': pick(0.50),
      'p95': pick(0.95),
      'p99': pick(0.99),
    };
  }

  /// 启动跑批：切换知识库后逐题执行（重复调用在运行中被忽略）
  Future<void> start(List<String> questions, {required String kbId}) async {
    if (running || questions.isEmpty) return;
    running = true;
    stopRequested = false;
    currentIndex = 0;
    total = questions.length;
    records.clear();
    error = null;
    notifyListeners();
    try {
      await harness.loadKnowledgeBase(kbId);
      if (harness.knowledgeBaseId != kbId) {
        error = '知识库切换失败，无法开始评测';
        return;
      }
      for (var index = 0; index < questions.length; index++) {
        if (stopRequested) break;
        currentIndex = index;
        notifyListeners();
        final question = questions[index];
        await _runSingle(question);
      }
    } finally {
      running = false;
      notifyListeners();
    }
  }

  /// 请求在当前题结束后停止
  void requestStop() {
    if (running) stopRequested = true;
    notifyListeners();
  }

  Future<void> _runSingle(String question) async {
    harness.newConversation();
    final completion = Completer<void>();
    void onChanged() {
      if (_awaitingCompletion && !harness.generating && !completion.isCompleted) {
        completion.complete();
      }
    }

    harness.addListener(onChanged);
    _awaitingCompletion = true;
    try {
      await harness.send(question);
      if (!harness.generating) {
        // 发送即失败（创建查询被拒等）：结束回调已触发，直接落记录
        _record(question);
        return;
      }
      await completion.future.timeout(
        _questionTimeout,
        onTimeout: () {
          error = '单题超时（${_questionTimeout.inSeconds}s），已终止整批';
          throw TimeoutException('评测单题超时');
        },
      );
      _record(question);
    } on TimeoutException {
      _recordTimeout(question);
    } finally {
      _awaitingCompletion = false;
      harness.removeListener(onChanged);
      notifyListeners();
    }
  }

  void _record(String question) {
    final telemetry = harness.lastTelemetry;
    if (telemetry == null) {
      records.add(BenchmarkRecord(
        question: question,
        state: 'no_telemetry',
        detail: '时间戳不齐（失败/取消/拒答无渲染）',
      ));
      return;
    }
    records.add(BenchmarkRecord(
      question: question,
      state: 'completed',
      queryId: telemetry.$1,
      clientTtftMs: telemetry.$4.difference(telemetry.$2).inMilliseconds,
    ));
  }

  void _recordTimeout(String question) {
    records.add(BenchmarkRecord(
      question: question,
      state: 'timeout',
      detail: error,
    ));
  }
}
