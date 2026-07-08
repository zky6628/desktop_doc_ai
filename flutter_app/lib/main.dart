// 导入 Flutter 材料设计包
import 'package:flutter/material.dart';
// 导入 Provider 状态管理包
import 'package:provider/provider.dart';
// 导入桌面拖拽包
import 'package:desktop_drop/desktop_drop.dart';
// 导入状态模型
import 'models/drop_file_model.dart';

/// 应用入口函数
void main() {
  runApp(
    // 使用 ChangeNotifierProvider 提供全局状态管理
    ChangeNotifierProvider(
      create: (context) => DropFileModel(),
      child: const MyApp(),
    ),
  );
}

/// 应用根组件
class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Document AI - RAG 文档助手',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.deepPurple),
        useMaterial3: true,
      ),
      home: const MyHomePage(),
    );
  }
}

/// 首页组件
class MyHomePage extends StatefulWidget {
  const MyHomePage({super.key});

  @override
  State<MyHomePage> createState() => _MyHomePageState();
}

/// 首页状态类
class _MyHomePageState extends State<MyHomePage> {
  // 是否正在拖拽
  bool _isDragging = false;
  // 查询输入控制器
  final TextEditingController _queryController = TextEditingController();
  // RAG 服务是否连接中
  bool _isConnecting = false;
  // 后端服务地址控制器
  final TextEditingController _urlController = TextEditingController(
    text: 'http://127.0.0.1:8000',
  );

  @override
  void initState() {
    super.initState();
    // 界面初始化后，尝试连接 RAG 服务
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _connectRagService();
    });
  }

  @override
  void dispose() {
    // 释放输入控制器
    _queryController.dispose();
    _urlController.dispose();
    // 断开服务连接
    final model = context.read<DropFileModel>();
    model.stopRagService();
    super.dispose();
  }

  /// 连接 RAG 服务
  Future<void> _connectRagService() async {
    setState(() {
      _isConnecting = true;
    });

    final model = context.read<DropFileModel>();

    // 连接服务
    final success = await model.initRagService(
      baseUrl: _urlController.text.trim(),
    );

    if (!success && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('RAG 服务连接失败: ${model.ragService.errorMessage}'),
          backgroundColor: Colors.red,
        ),
      );
    }

    if (mounted) {
      setState(() {
        _isConnecting = false;
      });
    }
  }

  /// 显示服务地址设置对话框
  Future<void> _showSettingsDialog() async {
    return showDialog(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('RAG 服务设置'),
        content: TextField(
          controller: _urlController,
          decoration: const InputDecoration(
            labelText: '服务地址',
            hintText: 'http://127.0.0.1:8000',
            border: OutlineInputBorder(),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('取消'),
          ),
          ElevatedButton(
            onPressed: () {
              Navigator.pop(context);
              _connectRagService();
            },
            child: const Text('连接'),
          ),
        ],
      ),
    );
  }

  /// 提交查询
  Future<void> _submitQuery() async {
    final query = _queryController.text.trim();
    if (query.isEmpty) return;

    final model = context.read<DropFileModel>();
    if (!model.ragService.isReady) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('RAG 服务未连接，请先启动后端服务'),
          backgroundColor: Colors.orange,
        ),
      );
      return;
    }

    // 执行查询
    await model.askQuestion(query);
  }

  /// 开始处理所有文件
  Future<void> _startProcessing() async {
    final model = context.read<DropFileModel>();
    if (model.fileCount == 0) return;

    if (!model.ragService.isReady) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('RAG 服务未连接，请先启动后端服务'),
          backgroundColor: Colors.orange,
        ),
      );
      return;
    }

    // 显示处理中提示
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('正在添加文件到知识库...')),
    );

    // 添加所有文件到知识库
    final successCount = await model.addAllFilesToKnowledgeBase();

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('成功添加 $successCount / ${model.fileCount} 个文件到知识库'),
          backgroundColor: successCount > 0 ? Colors.green : Colors.red,
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: Theme.of(context).colorScheme.inversePrimary,
        title: const Text('RAG 文档智能助手'),
        actions: [
          // 设置按钮
          IconButton(
            tooltip: '服务设置',
            icon: const Icon(Icons.settings),
            onPressed: _showSettingsDialog,
          ),
          // 服务状态指示灯
          Consumer<DropFileModel>(
            builder: (context, model, child) {
              return Tooltip(
                message: model.ragService.isReady
                    ? 'RAG 服务已连接'
                    : 'RAG 服务未连接',
                child: Container(
                  margin: const EdgeInsets.only(right: 16),
                  child: Icon(
                    Icons.circle,
                    color: model.ragService.isReady
                        ? Colors.green
                        : Colors.grey,
                    size: 12,
                  ),
                ),
              );
            },
          ),
        ],
      ),
      body: _isConnecting
          ? const Center(child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                CircularProgressIndicator(),
                SizedBox(height: 16),
                Text('正在连接 RAG 服务...'),
              ],
            ))
          : Padding(
              padding: const EdgeInsets.all(16.0),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  // 左侧：文件管理区域
                  Expanded(
                    flex: 1,
                    child: _buildFilePanel(),
                  ),
                  const SizedBox(width: 16),
                  // 右侧：问答区域
                  Expanded(
                    flex: 2,
                    child: _buildQAPanel(),
                  ),
                ],
              ),
            ),
    );
  }

  /// 构建左侧文件面板
  Widget _buildFilePanel() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildDropZone(),
        const SizedBox(height: 16),
        _buildFileListHeader(),
        const SizedBox(height: 8),
        Expanded(child: _buildFileList()),
      ],
    );
  }

  /// 构建拖拽区域
  Widget _buildDropZone() {
    return Consumer<DropFileModel>(
      builder: (context, model, child) {
        return DropTarget(
          onDragEntered: (details) {
            setState(() {
              _isDragging = true;
            });
          },
          onDragExited: (details) {
            setState(() {
              _isDragging = false;
            });
          },
          onDragDone: (details) {
            setState(() {
              _isDragging = false;
            });
            // 添加文件到列表
            model.addFiles(details.files);
          },
          child: Container(
            height: 150,
            decoration: BoxDecoration(
              color: _isDragging
                  ? Theme.of(context).colorScheme.primary.withValues(alpha: 0.1)
                  : Colors.grey.withValues(alpha: 0.05),
              border: Border.all(
                color: _isDragging
                    ? Theme.of(context).colorScheme.primary
                    : Colors.grey.withValues(alpha: 0.5),
                width: 2,
                style: BorderStyle.solid,
              ),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(
                    Icons.cloud_upload_outlined,
                    size: 40,
                    color: _isDragging
                        ? Theme.of(context).colorScheme.primary
                        : Colors.grey,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    _isDragging ? '释放以添加文件' : '拖拽文件到此处',
                    style: TextStyle(
                      fontSize: 14,
                      color: _isDragging
                          ? Theme.of(context).colorScheme.primary
                          : Colors.grey[700],
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '支持多文件同时拖拽',
                    style: TextStyle(
                      fontSize: 11,
                      color: Colors.grey[500],
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }

  /// 构建文件列表头部
  Widget _buildFileListHeader() {
    return Consumer<DropFileModel>(
      builder: (context, model, child) {
        return Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(
              '已添加文件 (${model.fileCount})',
              style: const TextStyle(
                fontSize: 14,
                fontWeight: FontWeight.bold,
              ),
            ),
            if (model.fileCount > 0)
              Row(
                children: [
                  TextButton.icon(
                    onPressed: model.ragService.isReady ? _startProcessing : null,
                    icon: const Icon(Icons.play_arrow, size: 16),
                    label: const Text('添加到知识库'),
                  ),
                  const SizedBox(width: 4),
                  TextButton.icon(
                    onPressed: () {
                      model.clearFiles();
                    },
                    icon: const Icon(Icons.clear_all, size: 16),
                    label: const Text('清空'),
                  ),
                ],
              ),
          ],
        );
      },
    );
  }

  /// 构建文件列表
  Widget _buildFileList() {
    return Consumer<DropFileModel>(
      builder: (context, model, child) {
        if (model.files.isEmpty) {
          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(
                  Icons.folder_open,
                  size: 48,
                  color: Colors.grey[300],
                ),
                const SizedBox(height: 12),
                Text(
                  '暂无文件',
                  style: TextStyle(
                    fontSize: 13,
                    color: Colors.grey[400],
                  ),
                ),
              ],
            ),
          );
        }

        return ListView.separated(
          itemCount: model.files.length,
          separatorBuilder: (context, index) => const Divider(height: 1),
          itemBuilder: (context, index) {
            final file = model.files[index];
            final status = model.getFileStatus(file.path);
            return ListTile(
              leading: const Icon(Icons.insert_drive_file, size: 20),
              title: Text(
                file.name,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 13),
              ),
              subtitle: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    file.path,
                    style: TextStyle(fontSize: 11, color: Colors.grey[600]),
                    overflow: TextOverflow.ellipsis,
                  ),
                  if (status != null)
                    Text(
                      status,
                      style: TextStyle(
                        fontSize: 11,
                        color: status == '已添加'
                            ? Colors.green
                            : status == '处理中...'
                                ? Colors.orange
                                : Colors.red,
                      ),
                    ),
                ],
              ),
              trailing: IconButton(
                icon: const Icon(Icons.close, size: 16),
                onPressed: () {
                  model.removeFile(file);
                },
              ),
              contentPadding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            );
          },
        );
      },
    );
  }

  /// 构建右侧问答面板
  Widget _buildQAPanel() {
    return Card(
      elevation: 2,
      child: Padding(
        padding: const EdgeInsets.all(16.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // 标题
            const Text(
              '智能问答',
              style: TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.bold,
              ),
            ),
            const SizedBox(height: 16),
            // 输入框
            _buildQueryInput(),
            const SizedBox(height: 16),
            // 结果展示区
            Expanded(child: _buildResultArea()),
          ],
        ),
      ),
    );
  }

  /// 构建查询输入框
  Widget _buildQueryInput() {
    return Consumer<DropFileModel>(
      builder: (context, model, child) {
        return Row(
          children: [
            Expanded(
              child: TextField(
                controller: _queryController,
                decoration: InputDecoration(
                  hintText: '请输入您的问题...',
                  border: const OutlineInputBorder(),
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 12,
                  ),
                  suffixIcon: model.isLoading
                      ? const Padding(
                          padding: EdgeInsets.all(8.0),
                          child: SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          ),
                        )
                      : null,
                ),
                maxLines: 2,
                minLines: 1,
                onChanged: (value) {
                  model.setQueryText(value);
                },
                onSubmitted: (_) => _submitQuery(),
                enabled: !model.isLoading && model.ragService.isReady,
              ),
            ),
            const SizedBox(width: 12),
            ElevatedButton.icon(
              onPressed: model.isLoading || !model.ragService.isReady
                  ? null
                  : _submitQuery,
              icon: const Icon(Icons.send),
              label: const Text('提问'),
              style: ElevatedButton.styleFrom(
                padding: const EdgeInsets.symmetric(
                  horizontal: 20,
                  vertical: 16,
                ),
              ),
            ),
          ],
        );
      },
    );
  }

  /// 构建结果展示区域
  Widget _buildResultArea() {
    return Consumer<DropFileModel>(
      builder: (context, model, child) {
        // 加载中
        if (model.isLoading) {
          return const Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                CircularProgressIndicator(),
                SizedBox(height: 16),
                Text('正在思考中...'),
              ],
            ),
          );
        }

        // 有错误
        if (model.errorMessage != null) {
          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const Icon(Icons.error_outline, size: 48, color: Colors.red),
                const SizedBox(height: 16),
                Text(
                  model.errorMessage!,
                  style: const TextStyle(color: Colors.red),
                  textAlign: TextAlign.center,
                ),
              ],
            ),
          );
        }

        // 有回答
        if (model.answer != null) {
          return SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // 回答区域
                const Row(
                  children: [
                    Icon(Icons.auto_awesome, color: Colors.deepPurple),
                    SizedBox(width: 8),
                    Text(
                      '回答',
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.deepPurple.withValues(alpha: 0.05),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(
                      color: Colors.deepPurple.withValues(alpha: 0.2),
                    ),
                  ),
                  child: Text(
                    model.answer!,
                    style: const TextStyle(fontSize: 14, height: 1.6),
                  ),
                ),
                const SizedBox(height: 20),
                // 参考来源区域
                if (model.sources.isNotEmpty) ...[
                  const Row(
                    children: [
                      Icon(Icons.menu_book, color: Colors.blueGrey),
                      SizedBox(width: 8),
                      Text(
                        '参考来源',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  ...model.sources.asMap().entries.map((entry) {
                    final index = entry.key;
                    final source = entry.value;
                    return Container(
                      width: double.infinity,
                      margin: const EdgeInsets.only(bottom: 8),
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: Colors.grey.withValues(alpha: 0.05),
                        borderRadius: BorderRadius.circular(6),
                        border: Border.all(
                          color: Colors.grey.withValues(alpha: 0.2),
                        ),
                      ),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Container(
                            width: 20,
                            height: 20,
                            decoration: BoxDecoration(
                              color: Colors.blueGrey.withValues(alpha: 0.2),
                              borderRadius: BorderRadius.circular(4),
                            ),
                            child: Center(
                              child: Text(
                                '${index + 1}',
                                style: const TextStyle(
                                  fontSize: 11,
                                  fontWeight: FontWeight.bold,
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(
                              source,
                              style: TextStyle(
                                fontSize: 12,
                                color: Colors.grey[700],
                                height: 1.5,
                              ),
                            ),
                          ),
                        ],
                      ),
                    );
                  }),
                ],
              ],
            ),
          );
        }

        // 默认状态
        return Center(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(
                Icons.psychology_alt_outlined,
                size: 64,
                color: Colors.grey[300],
              ),
              const SizedBox(height: 16),
              Text(
                '在上方输入问题，开始智能问答',
                style: TextStyle(
                  fontSize: 14,
                  color: Colors.grey[400],
                ),
              ),
              const SizedBox(height: 8),
              Text(
                '请先将文档添加到知识库',
                style: TextStyle(
                  fontSize: 12,
                  color: Colors.grey[400],
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}
