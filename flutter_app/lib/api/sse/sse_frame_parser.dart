import 'dart:convert';

/// 解析出的一条 SSE 事件帧
class SseFrame {
  const SseFrame({required this.id, required this.event, required this.data});

  /// 事件序号（合同为查询内单调递增整数；缺失时为 null）
  final int? id;
  final String event;
  final String data;
}

/// SSE 帧增量解析：字节流切片为完整事件帧
///
/// 本合同帧形固定：`id:`/`event:`/`data:` 单行字段，`:` 开头注释行
/// 为心跳，空行结束一帧。多行 data 按 SSE 规范以换行合并（当前服务端
/// 不产生，解析保持规范兼容）。
class SseFrameParser {
  final List<int> _pending = [];

  /// 记录最近一次收到的事件序号（重连时作为 Last-Event-ID）
  int lastEventId = 0;

  /// 送入一个字节块，返回其中完整解析出的事件帧（心跳注释不产出）
  List<SseFrame> push(List<int> chunk) {
    _pending.addAll(chunk);
    final frames = <SseFrame>[];
    while (true) {
      final boundary = _findFrameBoundary(_pending);
      if (boundary < 0) break;
      final block = utf8.decode(_pending.sublist(0, boundary));
      _pending.removeRange(0, boundary + 2);
      final frame = _parseBlock(block);
      if (frame != null) frames.add(frame);
    }
    return frames;
  }

  /// 帧边界为连续两个换行（\n\n）；返回第一字节下标，未找到返回 -1
  static int _findFrameBoundary(List<int> bytes) {
    for (var i = 0; i + 1 < bytes.length; i++) {
      if (bytes[i] == 0x0A && bytes[i + 1] == 0x0A) return i;
    }
    return -1;
  }

  SseFrame? _parseBlock(String block) {
    int? id;
    var event = '';
    final dataLines = <String>[];
    for (var line in block.split('\n')) {
      if (line.endsWith('\r')) line = line.substring(0, line.length - 1);
      if (line.isEmpty || line.startsWith(':')) continue;
      final colon = line.indexOf(':');
      final field = colon < 0 ? line : line.substring(0, colon);
      var value = colon < 0 ? '' : line.substring(colon + 1);
      if (value.startsWith(' ')) value = value.substring(1);
      switch (field) {
        case 'id':
          final parsed = int.tryParse(value);
          if (parsed != null) {
            id = parsed;
            lastEventId = parsed;
          }
        case 'event':
          event = value;
        case 'data':
          dataLines.add(value);
      }
    }
    if (event.isEmpty && dataLines.isEmpty) return null;
    return SseFrame(id: id, event: event, data: dataLines.join('\n'));
  }
}
