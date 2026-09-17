// 应用主界面 smoke 测试（替代失效的 Counter 模板测试）。
// 验证应用在无真实后端依赖的情况下完成初始化并渲染主界面骨架，
// 同时覆盖连接失败路径：初始化异常不再阻断 UI（BUG-01 补丁行为回归）。
//
// 说明：widget test 的 FakeAsync 环境会拦截全部 HTTP 请求（固定返回 400），
// 因此连接必然走失败路径；Hive boxes 在 setUp 的真实异步环境中预打开，
// 避免真实文件 IO 在 FakeAsync 中无法完成。
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:hive/hive.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:provider/provider.dart';

import 'package:desktop_document_ai/main.dart';
import 'package:desktop_document_ai/models/drop_file_model.dart';

/// path_provider 平台接口 mock
/// 将应用文档目录指向测试专用临时目录，避免测试触碰用户真实 Hive 数据
class MockPathProviderPlatform extends PathProviderPlatform {
  MockPathProviderPlatform(this.tempPath);

  /// mock 返回的文档目录路径
  final String tempPath;

  @override
  Future<String?> getApplicationDocumentsPath() async => tempPath;
}

void main() {
  late Directory tempDir;

  setUp(() async {
    tempDir = await Directory.systemTemp.createTemp('rag_chat_db_test');
    PathProviderPlatform.instance = MockPathProviderPlatform(tempDir.path);

    // 在真实异步环境中预打开 Hive boxes（Hive 为全局单例，openBox 幂等），
    // 使被测代码在 FakeAsync 环境中 openBox 时直接命中已打开的实例
    Hive.init(tempDir.path);
    await Hive.openBox<Map>('conversations');
    await Hive.openBox<Map>('messages');
    await Hive.openBox<Map>('files');
  });

  tearDown(() async {
    try {
      await tempDir.delete(recursive: true);
    } on FileSystemException {
      // Hive 关闭是异步的，文件可能被短暂占用；测试临时目录允许残留
    }
  });

  testWidgets('应用启动后渲染主界面骨架', (WidgetTester tester) async {
    // 与 main() 相同的 Provider 装配方式构建应用
    await tester.pumpWidget(
      ChangeNotifierProvider(
        create: (context) => DropFileModel(),
        child: const MyApp(),
      ),
    );

    // 触发 postFrameCallback 中的连接初始化
    await tester.pump();

    // 连接与初始化的完成回调需要真实事件循环轮转，分批等待最多约 2 秒，
    // 直到主界面状态稳定
    for (var i = 0; i < 20; i++) {
      await tester.runAsync(() async {
        await Future<void>.delayed(const Duration(milliseconds: 100));
      });
      await tester.pump();
      if (find.text('智能问答').evaluate().isNotEmpty) break;
    }

    // 推进虚拟时间：清除连接失败路径下 SnackBar 的 4 秒展示 timer 及退场动画
    await tester.pump(const Duration(seconds: 6));
    await tester.pump();

    // 主界面骨架正常渲染（连接失败时 UI 同样恢复，不卡加载页）
    expect(find.text('RAG 文档智能助手'), findsOneWidget);
    expect(find.text('智能问答'), findsOneWidget);
  });
}
