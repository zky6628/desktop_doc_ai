import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../controllers/app_shell_controller.dart';
import '../controllers/chat_controller.dart';
import '../api/dto/knowledge_dto.dart';
import '../theme/colors.dart';
import '../widgets/chat/chat_message_item.dart';

/// 问答页：消息流式渲染、引用抽屉与遥测采样
///
/// 会话列表与新建会话入口由应用外壳侧边栏承担（全局 [ChatController]
/// 提供），本页只负责消息区与引用抽屉。行为对齐 UI 规范 §4/§12：发送
/// 即记录 client_send_at 并显示用户消息，SSE token 增量渲染（首 token
/// 首帧后采样 rendered 时间戳），终态上传客户端遥测；[S编号] 可点击
/// 定位引用抽屉；知识库已删除进入只读。
class ChatPage extends StatefulWidget {
  const ChatPage({
    super.key,
    this.initialKbId,
    this.initialConversationId,
  });

  final String? initialKbId;
  final String? initialConversationId;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  late final ChatController _controller;
  final TextEditingController _inputController = TextEditingController();
  final ScrollController _messagesScroll = ScrollController();
  AppShellController? _shell;

  // 引用抽屉状态：当前展示的消息与高亮引用编号
  bool _drawerOpen = false;
  String? _drawerMessageId;
  int? _highlightOrder;
  final Map<String, GlobalKey> _citationKeys = {};

  // 首 token 渲染采样：按流式占位消息记录，避免重复回填
  String? _renderMarkedForId;
  String? _lastErrorText;

  @override
  void initState() {
    super.initState();
    _controller = context.read<ChatController>();
    _controller.addListener(_onControllerChanged);
    // 首载延后到首帧之后：控制器为全局单例（外壳侧边栏同帧监听），
    // 构建阶段同步通知会把兄弟组件标记为需要重建
    WidgetsBinding.instance.addPostFrameCallback((_) {
      unawaited(
        _controller.loadInitial(
          kbId: widget.initialKbId,
          conversationId: widget.initialConversationId,
        ),
      );
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final shell = context.read<AppShellController>();
    if (!identical(shell, _shell)) {
      _shell?.removeListener(_onShellChanged);
      _shell = shell;
      shell.addListener(_onShellChanged);
    }
  }

  /// 知识库页切库广播：问答页随最新选择自动重载
  ///
  /// IndexedStack 分支保活，页面不会重建，启动时的 loadInitial
  /// 覆盖不到此后发生的跨页切换，需监听壳层广播补载
  void _onShellChanged() {
    final id = _shell?.currentKnowledgeBaseId;
    if (id == null || id == _controller.knowledgeBase?.id) return;
    unawaited(_controller.loadInitial(kbId: id));
  }

  @override
  void dispose() {
    _shell?.removeListener(_onShellChanged);
    // 控制器为全局单例（生命周期与应用一致），页面只解除监听不释放
    _controller.removeListener(_onControllerChanged);
    _inputController.dispose();
    _messagesScroll.dispose();
    super.dispose();
  }

  void _onControllerChanged() {
    // 错误出现时弹一次提示，不阻塞消息展示
    final errorText =
        _controller.actionError?.message ??
        _controller.listError?.message ??
        _controller.messagesError?.message;
    if (errorText != null && errorText != _lastErrorText && mounted) {
      _lastErrorText = errorText;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(errorText, maxLines: 3, overflow: TextOverflow.ellipsis),
          behavior: SnackBarBehavior.floating,
          duration: const Duration(seconds: 4),
        ),
      );
    } else if (errorText == null) {
      _lastErrorText = null;
    }

    // 生成中贴底滚动；首 token 渲染完成后回填遥测时间戳
    if (_controller.generating) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!_messagesScroll.hasClients) return;
        _messagesScroll.jumpTo(_messagesScroll.position.maxScrollExtent);
      });
    }
    for (final message in _controller.messages) {
      if (message.role == 'assistant' &&
          message.streaming &&
          message.content.isNotEmpty &&
          _renderMarkedForId != message.id) {
        _renderMarkedForId = message.id;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _controller.markFirstTokenRendered();
        });
      }
    }
    setState(() {});
  }

  void _submit() {
    final text = _inputController.text.trim();
    if (text.isEmpty) return;
    _inputController.clear();
    unawaited(_controller.send(text));
  }

  void _openCitationDrawer(ChatMessageView message, int order) {
    setState(() {
      _drawerOpen = true;
      _drawerMessageId = message.id;
      _highlightOrder = order;
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final key = _citationKeys['${message.id}:$order'];
      final contextKey = key?.currentContext;
      if (contextKey != null) {
        Scrollable.ensureVisible(
          contextKey,
          duration: const Duration(milliseconds: 250),
          alignment: 0.15,
        );
      }
    });
  }

  void _closeCitationDrawer() {
    setState(() {
      _drawerOpen = false;
      _highlightOrder = null;
    });
  }

  ChatMessageView? get _drawerMessage {
    if (_drawerMessageId == null) return null;
    for (final message in _controller.messages) {
      if (message.id == _drawerMessageId) return message;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final kb = _controller.knowledgeBase;
    final readOnly = _controller.kbDeleted;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Expanded(child: _buildChatArea(context, theme, kb, readOnly)),
        _buildCitationDrawer(theme),
      ],
    );
  }

  Widget _buildChatArea(
    BuildContext context,
    ThemeData theme,
    KbDto? kb,
    bool readOnly,
  ) {
    if (kb == null && !_controller.kbLoading) {
      return _buildNoKnowledgeBase(context, theme);
    }
    if (_controller.kbLoading) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2.4));
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (readOnly) _buildReadOnlyBanner(theme),
        Expanded(child: _buildMessageList(context)),
        _buildInputArea(theme, readOnly),
      ],
    );
  }

  Widget _buildNoKnowledgeBase(BuildContext context, ThemeData theme) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            Icons.library_books_outlined,
            size: 52,
            color: theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.4),
          ),
          const SizedBox(height: 12),
          const Text('选择一个知识库开始问答'),
          const SizedBox(height: 12),
          ElevatedButton(
            onPressed: () => context.go('/knowledge-bases'),
            child: const Text('前往知识库'),
          ),
        ],
      ),
    );
  }

  Widget _buildReadOnlyBanner(ThemeData theme) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      color: AppColors.warning.withValues(alpha: 0.15),
      child: Row(
        children: [
          const Icon(Icons.lock_outline, size: 16, color: AppColors.warning),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '知识库已删除：会话与引用只读，不能继续提问',
              style: TextStyle(fontSize: 13, color: AppColors.warning),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildMessageList(BuildContext context) {
    if (_controller.messagesLoading) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2.4));
    }
    if (_controller.messages.isEmpty) {
      return Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(
              Icons.forum_outlined,
              size: 52,
              color: Theme.of(context)
                  .colorScheme
                  .onSurfaceVariant
                  .withValues(alpha: 0.4),
            ),
            const SizedBox(height: 12),
            const Text('输入问题，开始与知识库对话'),
          ],
        ),
      );
    }
    return ListView.builder(
      controller: _messagesScroll,
      padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 16),
      itemCount: _controller.messages.length,
      itemBuilder: (context, index) {
        final message = _controller.messages[index];
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            ChatMessageItem(
              message: message,
              onCitationTap: (order) => _openCitationDrawer(message, order),
            ),
            if (message.role == 'assistant' &&
                message.errorMessage != null &&
                !_controller.generating)
              Align(
                alignment: Alignment.centerLeft,
                child: Padding(
                  padding: const EdgeInsets.only(left: 4, bottom: 4),
                  child: TextButton.icon(
                    onPressed: () => unawaited(_controller.retryLast()),
                    icon: const Icon(Icons.refresh, size: 16),
                    label: const Text('重发'),
                  ),
                ),
              ),
          ],
        );
      },
    );
  }

  Widget _buildInputArea(ThemeData theme, bool readOnly) {
    final generating = _controller.generating;
    final statusText = _controller.statusText;
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 14),
      decoration: BoxDecoration(
        color: theme.colorScheme.surface,
        border: Border(top: BorderSide(color: theme.dividerColor, width: 1)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (statusText != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Row(
                children: [
                  const SizedBox(
                    width: 12,
                    height: 12,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    statusText,
                    style: TextStyle(
                      fontSize: 12,
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                ],
              ),
            ),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: TextField(
                  controller: _inputController,
                  enabled: !readOnly && !generating,
                  maxLines: 3,
                  minLines: 1,
                  textInputAction: TextInputAction.send,
                  onSubmitted: (_) => _submit(),
                  decoration: const InputDecoration(
                    hintText: '输入问题，Enter 发送',
                    isDense: true,
                    border: OutlineInputBorder(),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              if (generating)
                Tooltip(
                  message: '停止生成',
                  child: IconButton.filled(
                    style: IconButton.styleFrom(
                      backgroundColor: AppColors.error,
                      foregroundColor: Colors.white,
                    ),
                    onPressed: () => unawaited(_controller.cancel()),
                    icon: const Icon(Icons.stop),
                  ),
                )
              else
                IconButton.filled(
                  tooltip: '发送',
                  onPressed: readOnly ? null : _submit,
                  icon: const Icon(Icons.send),
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildCitationDrawer(ThemeData theme) {
    final message = _drawerMessage;
    final open = _drawerOpen && message != null;
    _citationKeys.removeWhere(
      (key, _) => !key.startsWith('${_drawerMessageId ?? ''}:'),
    );
    return AnimatedContainer(
      duration: const Duration(milliseconds: 200),
      curve: Curves.easeOut,
      width: open ? 360 : 0,
      decoration: BoxDecoration(
        color: theme.colorScheme.surface,
        border: Border(
          left: BorderSide(color: theme.dividerColor, width: open ? 1 : 0),
        ),
      ),
      clipBehavior: open ? Clip.none : Clip.hardEdge,
      child: open
          ? Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
                  child: Row(
                    children: [
                      const Icon(Icons.format_quote_outlined, size: 18),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          '引用来源（${message.citations.length}）',
                          style: const TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      IconButton(
                        tooltip: '关闭引用抽屉',
                        icon: const Icon(Icons.close, size: 18),
                        onPressed: _closeCitationDrawer,
                      ),
                    ],
                  ),
                ),
                const Divider(height: 1),
                Expanded(
                  child: message.citations.isEmpty
                      ? const Center(
                          child: Text('本条回答没有引用', style: TextStyle(fontSize: 13)),
                        )
                      : ListView.separated(
                          padding: const EdgeInsets.all(12),
                          itemCount: message.citations.length,
                          separatorBuilder: (_, _) => const SizedBox(height: 10),
                          itemBuilder: (context, index) {
                            final citation = message.citations[index];
                            final key = '${message.id}:${citation.citationOrder}';
                            _citationKeys[key] ??= GlobalKey();
                            return CitationCard(
                              key: _citationKeys[key],
                              citation: citation,
                              highlighted:
                                  citation.citationOrder != null &&
                                  citation.citationOrder == _highlightOrder,
                            );
                          },
                        ),
                ),
              ],
            )
          : const SizedBox.shrink(),
    );
  }
}
