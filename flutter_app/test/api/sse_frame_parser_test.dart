// SSE 帧解析器测试：多帧同块、帧跨字节块边界（含多字节字符截断）、
// 心跳注释过滤与事件序号跟踪。
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:desktop_document_ai/api/sse/sse_frame_parser.dart';

void main() {
  test('一次推送解析多条事件帧', () {
    final parser = SseFrameParser();
    final body = 'id: 1\nevent: meta\ndata: {"query_id":"q-1"}\n\n'
        'id: 2\nevent: stage\ndata: {"name":"retrieval_started"}\n\n';
    final frames = parser.push(utf8.encode(body));

    expect(frames, hasLength(2));
    expect(frames[0].id, 1);
    expect(frames[0].event, 'meta');
    expect(frames[0].data, '{"query_id":"q-1"}');
    expect(frames[1].event, 'stage');
    expect(parser.lastEventId, 2);
  });

  test('帧跨字节块边界拆分且不截断多字节字符', () {
    final parser = SseFrameParser();
    final frame = 'id: 7\nevent: tokens\ndata: {"text":"员工累计工作满一年"}\n\n';
    final bytes = utf8.encode(frame);
    // 在多字节中文字符的编码序列中间切开
    final cut = bytes.length - 10;
    final first = parser.push(bytes.sublist(0, cut));
    final second = parser.push(bytes.sublist(cut));

    expect(first, isEmpty);
    expect(second, hasLength(1));
    expect(second.single.data, '{"text":"员工累计工作满一年"}');
    expect(parser.lastEventId, 7);
  });

  test('心跳注释行不产出事件帧但推进分帧', () {
    final parser = SseFrameParser();
    final body = ': heartbeat\n\n'
        'id: 3\nevent: tokens\ndata: {"from":1,"to":2,"text":"年假"}\n\n';
    final frames = parser.push(utf8.encode(body));

    expect(frames, hasLength(1));
    expect(frames.single.event, 'tokens');
  });

  test('终态空载荷帧正常解析', () {
    final parser = SseFrameParser();
    final frames = parser.push(utf8.encode('id: 9\nevent: done\ndata: {}\n\n'));

    expect(frames.single.event, 'done');
    expect(frames.single.data, '{}');
  });
}
