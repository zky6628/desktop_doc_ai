import 'dart:async';
import 'dart:convert';

import '../api_error.dart';
import '../dto/query_dto.dart';
import '../query_api_client.dart';
import 'sse_frame_parser.dart';

/// 会话阶段（对齐 UI 规范的 SSE 客户端状态；creating_query 由上层编排）
enum QueryStreamPhase { connectingStream, receiving, reconnecting, completed, failed, cancelled }

/// 流式更新：一次事件或阶段变化产生的增量
class QueryStreamUpdate {
  const QueryStreamUpdate({
    required this.phase,
    this.appendedText = '',
    this.stage,
    this.citation,
    this.terminal,
  });

  final QueryStreamPhase phase;

  /// 本次 tokens 事件新增的文本（调用方负责累积）
  final String appendedText;
  final QueryStageEvent? stage;
  final CitationSnapshot? citation;
  final QueryTerminal? terminal;
}

/// 终态：SSE 终态事件或重连耗尽后的聚合恢复产出
class QueryTerminal {
  const QueryTerminal({
    required this.phase,
    this.refused = false,
    this.error,
    this.recoveredAggregate,
  });

  final QueryStreamPhase phase;
  final bool refused;
  final ApiError? error;

  /// 重连耗尽走 GET 聚合恢复时携带（含最终正文与引用）
  final QueryAggregate? recoveredAggregate;
}

/// 查询 SSE 会话：连接、断线重连、事件去重与终态恢复
///
/// 行为（对齐 UI 规范）：首次断开立即重连，后续按 1/2/4/8 秒退避，
/// 最多 5 次重连；每次携带 Last-Event-ID 并按事件序号去重；已接收
/// token 文本始终保留；重连耗尽改用 GET 聚合恢复终态（以聚合为准）；
/// error/cancelled/done 到达即终态关流。断开不触发取消。
class QueryStreamSession {
  QueryStreamSession({
    required this.queryId,
    required this.connect,
    required this.recoverAggregate,
    List<Duration>? retryBackoff,
    this.maxReconnects = 5,
  }) : retryBackoff = retryBackoff ??
            const [
              Duration.zero,
              Duration(seconds: 1),
              Duration(seconds: 2),
              Duration(seconds: 4),
              Duration(seconds: 8),
            ];

  final String queryId;

  /// SSE 连接工厂（由 [QueryApiClient.openEventStream] 提供，测试可注入）
  final SseConnect connect;

  /// 重连耗尽后的聚合恢复入口（GET /queries/{id}）
  final Future<QueryAggregate> Function() recoverAggregate;

  /// 第 n 次重连前的等待（下标 n-1）；首项为零即首次断开立即重连
  final List<Duration> retryBackoff;
  final int maxReconnects;

  final SseFrameParser _parser = SseFrameParser();

  /// 已接收 token 全文（跨重连保留）
  String get accumulatedText => _text.toString();
  final StringBuffer _text = StringBuffer();

  /// 启动会话；返回单订阅更新流，终态产出后流关闭
  Stream<QueryStreamUpdate> start() async* {
    var reconnectAttempt = 0;
    while (true) {
      yield QueryStreamUpdate(
        phase: reconnectAttempt == 0
            ? QueryStreamPhase.connectingStream
            : QueryStreamPhase.reconnecting,
      );
      try {
        final opened = await connect(_parser.lastEventId);
        final response = opened.$1;
        final closeConnection = opened.$2;
        try {
          if (response.statusCode != 200) {
            // 连接被拒绝（如查询已不存在）：读取信封错误后按可重试性分流
            final body = await response.stream.bytesToString();
            throw _connectErrorOf(response.statusCode, body);
          }
          yield QueryStreamUpdate(phase: QueryStreamPhase.receiving);
          await for (final chunk in response.stream) {
            for (final update in _consumeChunk(chunk)) {
              yield update;
              final terminal = update.terminal;
              if (terminal != null) return;
            }
          }
          // 服务端未发终态即关流：视为断线进入重连
        } finally {
          closeConnection();
        }
      } on ApiException catch (error) {
        if (!error.isRetryable) {
          // 不可重试的业务错误（如查询不存在）：直接进入失败终态
          yield QueryStreamUpdate(
            phase: QueryStreamPhase.failed,
            terminal: QueryTerminal(
              phase: QueryStreamPhase.failed,
              error: ApiError(
                code: error.code,
                message: error.message,
                retryable: error.retryable,
              ),
            ),
          );
          return;
        }
      }
      reconnectAttempt += 1;
      if (reconnectAttempt > maxReconnects) {
        yield await _recoverByAggregate();
        return;
      }
      final delay = retryBackoff[reconnectAttempt - 1];
      if (delay > Duration.zero) {
        await Future<void>.delayed(delay);
      }
    }
  }

  /// 把一个字节块解析为事件并映射为更新
  List<QueryStreamUpdate> _consumeChunk(List<int> chunk) {
    final updates = <QueryStreamUpdate>[];
    for (final frame in _parser.push(chunk)) {
      // 重放去重：序号不大于已见最大值的事件丢弃
      if (frame.id != null && frame.id! <= _seenEventId) continue;
      if (frame.id != null) _seenEventId = frame.id!;
      final data = _decodeData(frame.data);
      switch (frame.event) {
        case 'tokens':
          final batch = QueryTokenBatch.fromJson(data);
          _text.write(batch.text);
          updates.add(
            QueryStreamUpdate(
              phase: QueryStreamPhase.receiving,
              appendedText: batch.text,
            ),
          );
        case 'stage':
          updates.add(
            QueryStreamUpdate(
              phase: QueryStreamPhase.receiving,
              stage: QueryStageEvent.fromJson(data),
            ),
          );
        case 'citation':
          updates.add(
            QueryStreamUpdate(
              phase: QueryStreamPhase.receiving,
              citation: CitationSnapshot.fromJson(data),
            ),
          );
        case 'done':
          updates.add(
            QueryStreamUpdate(
              phase: QueryStreamPhase.completed,
              terminal: QueryTerminal(
                phase: QueryStreamPhase.completed,
                refused: data['refused'] == true,
              ),
            ),
          );
        case 'error':
          updates.add(
            QueryStreamUpdate(
              phase: QueryStreamPhase.failed,
              terminal: QueryTerminal(
                phase: QueryStreamPhase.failed,
                error: ApiError(
                  code: data['code'] as String? ?? 'UNKNOWN',
                  message: '生成失败（错误码 ${data['code']}）',
                ),
              ),
            ),
          );
        case 'cancelled':
          updates.add(
            QueryStreamUpdate(
              phase: QueryStreamPhase.cancelled,
              terminal: const QueryTerminal(
                phase: QueryStreamPhase.cancelled,
              ),
            ),
          );
        default:
          // meta 与未知事件类型不产生渲染更新，仅推进去重游标
          break;
      }
    }
    return updates;
  }

  int _seenEventId = 0;

  Map<String, dynamic> _decodeData(String raw) {
    if (raw.isEmpty) return const {};
    try {
      final decoded = jsonDecode(raw);
      return decoded is Map<String, dynamic> ? decoded : const {};
    } on FormatException {
      throw ApiException(
        code: 'UNKNOWN',
        message: 'SSE 事件载荷不是合法 JSON',
      );
    }
  }

  /// 连接被拒绝时的错误归类：可重试性决定后续走重连还是终态失败
  ApiException _connectErrorOf(int statusCode, String body) {
    final envelope = _tryDecodeEnvelope(body);
    if (envelope != null && envelope['error'] is Map<String, dynamic>) {
      return ApiException(
        code: (envelope['error'] as Map<String, dynamic>)['code'] as String? ??
            'UNKNOWN',
        message:
            (envelope['error'] as Map<String, dynamic>)['message'] as String? ??
                '连接事件流失败',
        retryable: statusCode >= 500 || statusCode == 429,
        statusCode: statusCode,
        requestId: envelope['request_id'] as String?,
      );
    }
    return ApiException(
      code: 'NETWORK_ERROR',
      message: '连接事件流返回 $statusCode',
      retryable: true,
      statusCode: statusCode,
    );
  }

  Map<String, dynamic>? _tryDecodeEnvelope(String body) {
    try {
      final decoded = jsonDecode(body);
      return decoded is Map<String, dynamic> ? decoded : null;
    } on FormatException {
      return null;
    }
  }

  /// 重连耗尽：改用 GET 聚合轮询恢复，以聚合终态为准
  ///
  /// 聚合尚未到终态（服务端仍在执行）时按固定间隔继续轮询；网络
  /// 故障持续重试，仅不可重试的业务错误（如查询不存在）转为失败终态
  Future<QueryStreamUpdate> _recoverByAggregate() async {
    const pollInterval = Duration(seconds: 2);
    while (true) {
      final aggregate = await _fetchAggregateGuarded();
      switch (aggregate.state) {
        case QueryRunStatus.completed:
          return QueryStreamUpdate(
            phase: QueryStreamPhase.completed,
            terminal: QueryTerminal(
              phase: QueryStreamPhase.completed,
              refused: aggregate.refused,
              recoveredAggregate: aggregate,
            ),
          );
        case QueryRunStatus.failed:
          return QueryStreamUpdate(
            phase: QueryStreamPhase.failed,
            terminal: QueryTerminal(
              phase: QueryStreamPhase.failed,
              error: aggregate.error,
              recoveredAggregate: aggregate,
            ),
          );
        case QueryRunStatus.cancelled:
          return const QueryStreamUpdate(
            phase: QueryStreamPhase.cancelled,
            terminal: QueryTerminal(phase: QueryStreamPhase.cancelled),
          );
        default:
          await Future<void>.delayed(pollInterval);
      }
    }
  }

  Future<QueryAggregate> _fetchAggregateGuarded() async {
    while (true) {
      try {
        return await recoverAggregate();
      } on ApiException catch (error) {
        if (!error.isRetryable) rethrow;
        await Future<void>.delayed(const Duration(seconds: 2));
      }
    }
  }
}
