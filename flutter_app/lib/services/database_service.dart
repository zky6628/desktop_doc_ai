import 'package:hive/hive.dart';
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';
import 'dart:io';
import '../models/conversation.dart';
import '../models/chat_message.dart';
import '../models/knowledge_file.dart';

/// Hive 本地数据库服务（单例）
///
/// 负责本地数据库的初始化以及对话、消息、文件的 CRUD 操作。
/// 使用纯 Dart 实现的 Hive 数据库，桌面端无原生构建依赖。
class DatabaseService {
  static final DatabaseService _instance = DatabaseService._internal();

  /// 获取单例实例
  factory DatabaseService() => _instance;
  DatabaseService._internal();

  /// 对话 Box
  Box<Map>? _conversationsBox;

  /// 消息 Box
  Box<Map>? _messagesBox;

  /// 文件 Box
  Box<Map>? _filesBox;

  /// UUID 生成器，用于生成主键 ID
  final _uuid = const Uuid();

  /// 是否已初始化
  bool _initialized = false;

  /// 数据库目录路径（Hive.init 使用的目录）
  String? _dbDir;

  /// 获取对话 Box
  Future<Box<Map>> get _conversations async {
    await _ensureInitialized();
    return _conversationsBox!;
  }

  /// 获取消息 Box
  Future<Box<Map>> get _messages async {
    await _ensureInitialized();
    return _messagesBox!;
  }

  /// 获取文件 Box
  Future<Box<Map>> get _files async {
    await _ensureInitialized();
    return _filesBox!;
  }

  /// 打开指定名称的 Box
  ///
  /// Windows 下 Hive 的 .lock 标记文件偶发无法打开（PathAccessException，
  /// errno = 5），而该文件是 0 字节的锁标记、不承载数据。
  /// 此时删除该标记文件后重试一次，即可恢复正常打开。
  /// 若删除本身失败（说明标记文件仍被其他进程持有），按原语义抛出异常。
  ///
  /// [name] Box 名称
  Future<Box<Map>> _openBoxWithLockRecovery(String name) async {
    try {
      return await Hive.openBox<Map>(name);
    } on PathAccessException {
      final lockFile =
          File('$_dbDir${Platform.pathSeparator}$name.lock');
      try {
        await lockFile.delete();
      } on FileSystemException {
        rethrow;
      }
      return await Hive.openBox<Map>(name);
    }
  }

  /// 确保数据库已初始化
  Future<void> _ensureInitialized() async {
    if (_initialized) return;

    final appDir = await getApplicationDocumentsDirectory();
    final dbDir = '${appDir.path}${Platform.pathSeparator}rag_chat_db';
    Directory(dbDir).createSync(recursive: true);
    _dbDir = dbDir;

    Hive.init(dbDir);

    _conversationsBox = await _openBoxWithLockRecovery('conversations');
    _messagesBox = await _openBoxWithLockRecovery('messages');
    _filesBox = await _openBoxWithLockRecovery('files');

    _initialized = true;
  }

  // ===================== 对话相关 =====================

  /// 获取所有对话列表，按更新时间倒序排列
  Future<List<Conversation>> getConversations() async {
    final box = await _conversations;
    final list = box.values
        .map((m) => Conversation.fromMap(Map<String, dynamic>.from(m)))
        .toList();
    list.sort((a, b) => b.updatedAt.compareTo(a.updatedAt));
    return list;
  }

  /// 根据 ID 获取单个对话
  ///
  /// [id] 对话 ID，不存在时返回 null
  Future<Conversation?> getConversation(String id) async {
    final box = await _conversations;
    final map = box.get(id);
    if (map == null) return null;
    return Conversation.fromMap(Map<String, dynamic>.from(map));
  }

  /// 创建新对话
  ///
  /// [title] 对话标题，默认为"新对话"
  /// 返回创建后的对话对象
  Future<Conversation> createConversation({String? title}) async {
    final box = await _conversations;
    final now = DateTime.now();
    final convo = Conversation(
      id: _uuid.v4(),
      title: title ?? '新对话',
      createdAt: now,
      updatedAt: now,
    );
    await box.put(convo.id, convo.toMap());
    return convo;
  }

  /// 更新对话信息（标题和更新时间）
  ///
  /// [conversation] 要更新的对话对象
  Future<void> updateConversation(Conversation conversation) async {
    final box = await _conversations;
    final updated = conversation.copyWith(updatedAt: DateTime.now());
    await box.put(conversation.id, updated.toMap());
  }

  /// 更新对话标题
  ///
  /// [id] 对话 ID
  /// [title] 新标题
  Future<void> updateConversationTitle(String id, String title) async {
    final box = await _conversations;
    final map = box.get(id);
    if (map == null) return;
    final convo = Conversation.fromMap(Map<String, dynamic>.from(map));
    final updated = convo.copyWith(title: title, updatedAt: DateTime.now());
    await box.put(id, updated.toMap());
  }

  /// 删除对话及其关联的所有消息
  ///
  /// [id] 对话 ID
  Future<void> deleteConversation(String id) async {
    final convBox = await _conversations;
    final msgBox = await _messages;

    // 删除关联的消息
    final msgKeysToDelete = msgBox.keys.where((key) {
      final map = msgBox.get(key);
      return map != null && map['conversation_id'] == id;
    }).toList();
    await msgBox.deleteAll(msgKeysToDelete);

    // 删除对话
    await convBox.delete(id);
  }

  // ===================== 消息相关 =====================

  /// 获取指定对话的所有消息，按创建时间升序排列
  ///
  /// [conversationId] 对话 ID
  Future<List<ChatMessage>> getMessages(String conversationId) async {
    final box = await _messages;
    final list = box.values
        .where((m) => m['conversation_id'] == conversationId)
        .map((m) => ChatMessage.fromMap(Map<String, dynamic>.from(m)))
        .toList();
    list.sort((a, b) => a.timestamp.compareTo(b.timestamp));
    return list;
  }

  /// 添加一条消息，同时更新对应对话的更新时间
  ///
  /// [message] 要添加的消息对象
  Future<void> addMessage(ChatMessage message) async {
    final msgBox = await _messages;
    final convBox = await _conversations;

    final map = message.toMap();
    map['conversation_id'] = message.conversationId;
    await msgBox.put(message.id, map);

    // 更新对话的更新时间
    if (message.conversationId != null) {
      final convMap = convBox.get(message.conversationId);
      if (convMap != null) {
        final convo = Conversation.fromMap(Map<String, dynamic>.from(convMap));
        final updated = convo.copyWith(updatedAt: DateTime.now());
        await convBox.put(convo.id, updated.toMap());
      }
    }
  }

  /// 删除一条消息
  ///
  /// [id] 消息 ID
  Future<void> deleteMessage(String id) async {
    final box = await _messages;
    await box.delete(id);
  }

  // ===================== 文件相关 =====================

  /// 获取所有文件列表，按创建时间倒序排列
  Future<List<KnowledgeFile>> getFiles() async {
    final box = await _files;
    final list = box.values
        .map((m) => KnowledgeFile.fromMap(Map<String, dynamic>.from(m)))
        .toList();
    list.sort((a, b) => b.createdAt.compareTo(a.createdAt));
    return list;
  }

  /// 根据文件路径查找文件
  ///
  /// [filepath] 文件完整路径，不存在时返回 null
  Future<KnowledgeFile?> getFileByPath(String filepath) async {
    final box = await _files;
    for (final key in box.keys) {
      final map = box.get(key);
      if (map != null && map['filepath'] == filepath) {
        return KnowledgeFile.fromMap(Map<String, dynamic>.from(map));
      }
    }
    return null;
  }

  /// 添加文件记录（若已存在则直接返回现有记录）
  ///
  /// [filename] 文件名
  /// [filepath] 文件完整路径
  /// 返回文件对象
  Future<KnowledgeFile> addFile(String filename, String filepath) async {
    final existing = await getFileByPath(filepath);
    if (existing != null) return existing;

    final box = await _files;
    final file = KnowledgeFile(
      id: _uuid.v4(),
      filename: filename,
      filepath: filepath,
      createdAt: DateTime.now(),
    );
    await box.put(file.id, file.toMap());
    return file;
  }

  /// 更新文件是否已加入知识库的状态
  ///
  /// [id] 文件 ID
  /// [inKb] 是否已在知识库中
  Future<void> updateFileInKB(String id, bool inKb) async {
    final box = await _files;
    final map = box.get(id);
    if (map == null) return;
    final file = KnowledgeFile.fromMap(Map<String, dynamic>.from(map));
    final updated = file.copyWith(inKb: inKb);
    await box.put(id, updated.toMap());
  }

  /// 删除文件记录
  ///
  /// [id] 文件 ID
  Future<void> deleteFile(String id) async {
    final box = await _files;
    await box.delete(id);
  }

  /// 根据文件路径删除文件记录
  ///
  /// [filepath] 文件完整路径
  Future<void> deleteFileByPath(String filepath) async {
    final box = await _files;
    final keysToDelete = box.keys.where((key) {
      final map = box.get(key);
      return map != null && map['filepath'] == filepath;
    }).toList();
    await box.deleteAll(keysToDelete);
  }

  /// 清空所有文件记录
  Future<void> clearFiles() async {
    final box = await _files;
    await box.clear();
  }

  // ===================== 关闭 =====================

  /// 关闭数据库连接
  Future<void> close() async {
    await _conversationsBox?.close();
    await _messagesBox?.close();
    await _filesBox?.close();
    _conversationsBox = null;
    _messagesBox = null;
    _filesBox = null;
    _initialized = false;
  }
}
