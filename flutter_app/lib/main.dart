import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'models/drop_file_model.dart';
import 'models/chat_message.dart';
import 'theme/theme.dart';
import 'widgets/sidebar.dart';
import 'widgets/drop_zone.dart';
import 'widgets/chat_bubble.dart';
import 'widgets/loading_dots.dart';

void main() {
  runApp(
    ChangeNotifierProvider(
      create: (context) => DropFileModel(),
      child: const MyApp(),
    ),
  );
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Document AI - RAG 文档助手',
      theme: AppTheme.light,
      home: const MyHomePage(),
    );
  }
}

class MyHomePage extends StatefulWidget {
  const MyHomePage({super.key});

  @override
  State<MyHomePage> createState() => _MyHomePageState();
}

class _MyHomePageState extends State<MyHomePage> {
  final TextEditingController _queryController = TextEditingController();
  bool _isConnecting = false;
  final TextEditingController _urlController = TextEditingController(
    text: 'http://127.0.0.1:8000',
  );
  final ScrollController _chatScrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _connectRagService();
    });
  }

  @override
  void dispose() {
    _queryController.dispose();
    _urlController.dispose();
    _chatScrollController.dispose();
    context.read<DropFileModel>().stopRagService();
    super.dispose();
  }

  Future<void> _connectRagService() async {
    setState(() => _isConnecting = true);
    final model = context.read<DropFileModel>();
    final success = await model.initRagService(baseUrl: _urlController.text.trim());

    if (!success && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('RAG 服务连接失败: ${model.ragService.errorMessage}')),
      );
    }

    if (mounted) setState(() => _isConnecting = false);
  }

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
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('取消')),
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

  Future<void> _submitQuery() async {
    final query = _queryController.text.trim();
    if (query.isEmpty) return;

    final model = context.read<DropFileModel>();
    if (!model.ragService.isReady) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('RAG 服务未连接，请先启动后端服务')),
      );
      return;
    }

    _queryController.clear();
    model.setQueryText('');
    await model.askQuestion(query);
    _scrollToBottom();
  }

  Future<void> _startProcessing() async {
    final model = context.read<DropFileModel>();
    if (model.fileCount == 0) return;

    if (!model.ragService.isReady) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('RAG 服务未连接，请先启动后端服务')),
      );
      return;
    }

    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('正在添加文件到知识库...')),
    );

    final successCount = await model.addAllFilesToKnowledgeBase();

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('成功添加 $successCount / ${model.fileCount} 个文件到知识库'),
        ),
      );
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_chatScrollController.hasClients) {
        _chatScrollController.animateTo(
          _chatScrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('RAG 文档智能助手'),
        actions: [
          IconButton(
            tooltip: '服务设置',
            icon: const Icon(Icons.settings),
            onPressed: _showSettingsDialog,
          ),
          Consumer<DropFileModel>(
            builder: (context, model, _) {
              return Tooltip(
                message: model.ragService.isReady ? 'RAG 服务已连接' : 'RAG 服务未连接',
                child: Container(
                  margin: const EdgeInsets.only(right: 16),
                  child: Icon(
                    Icons.circle,
                    color: model.ragService.isReady ? const Color(0xFF22C55E) : Colors.grey,
                    size: 12,
                  ),
                ),
              );
            },
          ),
        ],
      ),
      body: _isConnecting
          ? const Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  LoadingDots(radius: 8),
                  SizedBox(height: 16),
                  Text('正在连接 RAG 服务...'),
                ],
              ),
            )
          : Row(
              children: [
                Sidebar(onAddToKnowledgeBase: _startProcessing),
                Expanded(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        const DropZone(height: 120),
                        const SizedBox(height: 16),
                        Expanded(child: _buildChatPanel()),
                      ],
                    ),
                  ),
                ),
              ],
            ),
    );
  }

  Widget _buildChatPanel() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Row(
                  children: [
                    Icon(Icons.smart_toy, size: 20),
                    SizedBox(width: 8),
                    Text('智能问答', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
                  ],
                ),
                Consumer<DropFileModel>(
                  builder: (context, model, _) {
                    if (model.chatHistory.isEmpty) return const SizedBox.shrink();
                    return TextButton.icon(
                      onPressed: () => model.clearChatHistory(),
                      icon: const Icon(Icons.delete_outline, size: 16),
                      label: const Text('清空对话'),
                    );
                  },
                ),
              ],
            ),
            const Divider(),
            Expanded(child: _buildMessageList()),
            const SizedBox(height: 12),
            _buildQueryInput(),
          ],
        ),
      ),
    );
  }

  Widget _buildMessageList() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        if (model.chatHistory.isEmpty && !model.isLoading) {
          return Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(
                  Icons.psychology_alt_outlined,
                  size: 56,
                  color: Theme.of(context).colorScheme.primary.withValues(alpha: 0.3),
                ),
                const SizedBox(height: 12),
                const Text('在下方输入问题，开始智能问答'),
              ],
            ),
          );
        }

        return ListView.builder(
          controller: _chatScrollController,
          padding: const EdgeInsets.symmetric(vertical: 8),
          itemCount: model.chatHistory.length + (model.isLoading ? 1 : 0),
          itemBuilder: (context, index) {
            if (index == model.chatHistory.length && model.isLoading) {
              return ChatBubble(
                message: model.chatHistory.isNotEmpty && model.chatHistory.last.isUser
                    ? model.chatHistory.last
                    : ChatMessage(id: 'loading', type: MessageType.assistant, content: ''),
                isLoading: true,
              );
            }
            return ChatBubble(message: model.chatHistory[index]);
          },
        );
      },
    );
  }

  Widget _buildQueryInput() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        return Row(
          children: [
            Expanded(
              child: TextField(
                controller: _queryController,
                decoration: InputDecoration(
                  hintText: '请输入您的问题...',
                  suffixIcon: model.isLoading
                      ? const SizedBox(
                          width: 20,
                          height: 20,
                          child: LoadingDots(
                            radius: 4,
                            spacing: 4,
                            bounceHeight: 6,
                          ),
                        )
                      : null,
                ),
                maxLines: 2,
                minLines: 1,
                onChanged: (value) => model.setQueryText(value),
                onSubmitted: (_) => _submitQuery(),
                enabled: !model.isLoading && model.ragService.isReady,
              ),
            ),
            const SizedBox(width: 12),
            ElevatedButton.icon(
              onPressed: model.isLoading || !model.ragService.isReady ? null : _submitQuery,
              icon: const Icon(Icons.send, size: 18),
              label: const Text('提问'),
            ),
          ],
        );
      },
    );
  }
}
