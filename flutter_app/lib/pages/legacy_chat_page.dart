import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../models/drop_file_model.dart';
import '../models/chat_message.dart';
import '../widgets/sidebar.dart';
import '../widgets/drop_zone.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/loading_dots.dart';

/// 过渡期问答页：承载既有聊天链路（Hive 会话 + 旧版端点）
///
/// 行为与拆分前的单页应用保持一致；服务端化问答在后续任务
/// 一次性替换本页与 Hive 路径。
class LegacyChatPage extends StatefulWidget {
  const LegacyChatPage({super.key});

  @override
  State<LegacyChatPage> createState() => _LegacyChatPageState();
}

class _LegacyChatPageState extends State<LegacyChatPage> {
  final TextEditingController _queryController = TextEditingController();
  bool _isConnecting = false;
  final TextEditingController _urlController = TextEditingController(
    text: 'http://127.0.0.1:8000',
  );
  final ScrollController _chatScrollController = ScrollController();

  /// 页面生命周期内使用的状态模型引用
  /// 在 initState 中获取，避免 dispose 等非活跃阶段访问 InheritedWidget
  late final DropFileModel _model;

  @override
  void initState() {
    super.initState();
    _model = context.read<DropFileModel>();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _connectRagService();
    });
  }

  @override
  void dispose() {
    _queryController.dispose();
    _urlController.dispose();
    _chatScrollController.dispose();
    _model.stopRagService();
    super.dispose();
  }

  // ===================== 统一错误提示 =====================

  void _showErrorSnackBar(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Row(
          children: [
            const Icon(Icons.error_outline, color: Colors.white),
            const SizedBox(width: 10),
            Expanded(child: Text(message, maxLines: 3, overflow: TextOverflow.ellipsis)),
          ],
        ),
        backgroundColor: const Color(0xFFDC2626),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 4),
        action: SnackBarAction(
          label: '关闭',
          textColor: Colors.white,
          onPressed: () {
            ScaffoldMessenger.of(context).hideCurrentSnackBar();
          },
        ),
      ),
    );
  }

  void _showSuccessSnackBar(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Row(
          children: [
            const Icon(Icons.check_circle_outline, color: Colors.white),
            const SizedBox(width: 10),
            Expanded(child: Text(message)),
          ],
        ),
        backgroundColor: const Color(0xFF16A34A),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _connectRagService() async {
    setState(() => _isConnecting = true);
    bool success = false;
    String? failureMessage;
    try {
      success = await _model.initRagService(baseUrl: _urlController.text.trim());
    } catch (e) {
      failureMessage = '初始化失败: $e';
    }

    if (!mounted) return;
    if (!success) {
      _showErrorSnackBar(
          failureMessage ?? _model.ragService.errorMessage ?? 'RAG 服务连接失败');
    }

    setState(() => _isConnecting = false);
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
      _showErrorSnackBar('RAG 服务未连接，请先启动后端服务');
      return;
    }

    _queryController.clear();
    model.setQueryText('');
    final error = await model.askQuestion(query);
    if (error != null && mounted) {
      _showErrorSnackBar(error);
    }
    _scrollToBottom();
  }

  Future<void> _startProcessing() async {
    final model = context.read<DropFileModel>();
    if (model.fileCount == 0) return;

    if (!model.ragService.isReady) {
      _showErrorSnackBar('RAG 服务未连接，请先启动后端服务');
      return;
    }

    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text('正在添加文件到知识库...'),
        duration: Duration(seconds: 1),
      ),
    );

    final (successCount, errors) = await model.addAllFilesToKnowledgeBase();

    if (!mounted) return;

    if (errors.isEmpty) {
      _showSuccessSnackBar('成功添加 $successCount 个文件到知识库');
    } else if (successCount == 0) {
      _showErrorSnackBar('所有文件添加失败:\n${errors.take(3).join('\n')}');
    } else {
      // 部分成功
      _showSuccessSnackBar('成功添加 $successCount / ${model.fileCount} 个文件');
      if (errors.isNotEmpty) {
        // 延迟显示失败详情，避免覆盖成功提示
        Future.delayed(const Duration(milliseconds: 2200), () {
          if (mounted) {
            _showErrorSnackBar('部分文件添加失败:\n${errors.take(3).join('\n')}');
          }
        });
      }
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
    if (_isConnecting) {
      return const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            LoadingDots(radius: 8),
            SizedBox(height: 16),
            Text('正在连接 RAG 服务...'),
          ],
        ),
      );
    }
    return Row(
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
    );
  }

  Widget _buildModelSelector() {
    return Consumer<DropFileModel>(
      builder: (context, model, _) {
        final ragService = model.ragService;
        return ListenableBuilder(
          listenable: ragService,
          builder: (context, _) {
            final models = ragService.models;
            final currentId = ragService.currentModelId;

            if (models.isEmpty || !ragService.isReady) {
              return const SizedBox.shrink();
            }

            return Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.surface,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(
                  color: Theme.of(context).dividerColor,
                  width: 1,
                ),
              ),
              child: DropdownButtonHideUnderline(
                child: DropdownButton<String>(
                  value: currentId ?? models.first.id,
                  isDense: true,
                  icon: const Icon(Icons.keyboard_arrow_down, size: 18),
                  items: models.map((m) {
                    return DropdownMenuItem<String>(
                      value: m.id,
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(
                            m.isLocal ? Icons.computer : Icons.cloud,
                            size: 14,
                            color: m.isLocal
                                ? const Color(0xFF16A34A)
                                : const Color(0xFF3B82F6),
                          ),
                          const SizedBox(width: 6),
                          Text(
                            m.name,
                            style: const TextStyle(fontSize: 13),
                          ),
                        ],
                      ),
                    );
                  }).toList(),
                  onChanged: model.isLoading
                      ? null
                      : (value) {
                          if (value != null) {
                            ragService.setCurrentModel(value);
                          }
                        },
                ),
              ),
            );
          },
        );
      },
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
                Row(
                  children: [
                    const Icon(Icons.smart_toy, size: 20),
                    const SizedBox(width: 8),
                    const Text('智能问答', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
                    const SizedBox(width: 12),
                    _buildModelSelector(),
                  ],
                ),
                Row(
                  children: [
                    Consumer<DropFileModel>(
                      builder: (context, model, _) {
                        if (model.chatHistory.isEmpty || model.currentConversation == null) {
                          return const SizedBox.shrink();
                        }
                        return TextButton.icon(
                          onPressed: () {
                            model.deleteConversation(model.currentConversation!.id);
                          },
                          icon: const Icon(Icons.delete_outline, size: 16),
                          label: const Text('删除对话'),
                        );
                      },
                    ),
                    IconButton(
                      tooltip: '服务设置',
                      icon: const Icon(Icons.settings_outlined, size: 20),
                      onPressed: _showSettingsDialog,
                    ),
                  ],
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
