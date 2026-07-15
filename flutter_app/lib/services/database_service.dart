import 'dart:io' show Platform;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';
import 'package:path/path.dart' as path;
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';
import '../models/conversation.dart';
import '../models/chat_message.dart';
import '../models/knowledge_file.dart';

/// SQLite 数据库服务（单例）
///
/// 负责本地数据库的初始化、表创建以及对话、消息、文件的 CRUD 操作。
/// 桌面端（Windows/Linux/macOS）使用 sqflite_common_ffi 作为数据库实现。
class DatabaseService {
  static final DatabaseService _instance = DatabaseService._internal();

  /// 获取单例实例
  factory DatabaseService() => _instance;
  DatabaseService._internal();

  /// 数据库实例（懒加载）
  Database? _db;

  /// UUID 生成器，用于生成主键 ID
  final _uuid = const Uuid();

  /// 是否已初始化 FFI（桌面端用）
  static bool _ffiInitialized = false;

  /// 获取数据库实例，若未初始化则先初始化
  Future<Database> get database async {
    if (_db != null) return _db!;
    _db = await _initDatabase();
    return _db!;
  }

  /// 初始化数据库
  ///
  /// 桌面端会先初始化 FFI 工厂，然后在应用文档目录下创建数据库文件。
  Future<Database> _initDatabase() async {
    if (Platform.isWindows || Platform.isLinux || Platform.isMacOS) {
      if (!_ffiInitialized) {
        sqfliteFfiInit();
        databaseFactory = databaseFactoryFfi;
        _ffiInitialized = true;
      }
    }
    final appDir = await getApplicationDocumentsDirectory();
    final dbPath = path.join(appDir.path, 'rag_chat.db');
    return await openDatabase(
      dbPath,
      version: 1,
      onCreate: _onCreate,
    );
  }

  /// 数据库创建回调，初始化表结构和索引
  Future<void> _onCreate(Database db, int version) async {
    await db.execute('''
      CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      )
    ''');

    await db.execute('''
      CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        sources TEXT,
        meta TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
      )
    ''');

    await db.execute('''
      CREATE TABLE files (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        in_kb INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL
      )
    ''');

    await db.execute(
        'CREATE INDEX idx_messages_conversation_id ON messages (conversation_id)');
    await db.execute('CREATE INDEX idx_files_filepath ON files (filepath)');
  }

  // ===================== 对话相关 =====================

  /// 获取所有对话列表，按更新时间倒序排列
  Future<List<Conversation>> getConversations() async {
    final db = await database;
    final maps = await db.query(
      'conversations',
      orderBy: 'updated_at DESC',
    );
    return maps.map((m) => Conversation.fromMap(m)).toList();
  }

  /// 根据 ID 获取单个对话
  ///
  /// [id] 对话 ID，不存在时返回 null
  Future<Conversation?> getConversation(String id) async {
    final db = await database;
    final maps = await db.query(
      'conversations',
      where: 'id = ?',
      whereArgs: [id],
    );
    if (maps.isEmpty) return null;
    return Conversation.fromMap(maps.first);
  }

  /// 创建新对话
  ///
  /// [title] 对话标题，默认为"新对话"
  /// 返回创建后的对话对象
  Future<Conversation> createConversation({String? title}) async {
    final db = await database;
    final now = DateTime.now();
    final convo = Conversation(
      id: _uuid.v4(),
      title: title ?? '新对话',
      createdAt: now,
      updatedAt: now,
    );
    await db.insert('conversations', convo.toMap());
    return convo;
  }

  /// 更新对话信息（标题和更新时间）
  ///
  /// [conversation] 要更新的对话对象
  Future<void> updateConversation(Conversation conversation) async {
    final db = await database;
    await db.update(
      'conversations',
      {
        'title': conversation.title,
        'updated_at': DateTime.now().millisecondsSinceEpoch,
      },
      where: 'id = ?',
      whereArgs: [conversation.id],
    );
  }

  /// 更新对话标题
  ///
  /// [id] 对话 ID
  /// [title] 新标题
  Future<void> updateConversationTitle(String id, String title) async {
    final db = await database;
    await db.update(
      'conversations',
      {
        'title': title,
        'updated_at': DateTime.now().millisecondsSinceEpoch,
      },
      where: 'id = ?',
      whereArgs: [id],
    );
  }

  /// 删除对话及其关联的所有消息
  ///
  /// [id] 对话 ID
  Future<void> deleteConversation(String id) async {
    final db = await database;
    await db.delete('messages',
        where: 'conversation_id = ?', whereArgs: [id]);
    await db.delete('conversations', where: 'id = ?', whereArgs: [id]);
  }

  // ===================== 消息相关 =====================

  /// 获取指定对话的所有消息，按创建时间升序排列
  ///
  /// [conversationId] 对话 ID
  Future<List<ChatMessage>> getMessages(String conversationId) async {
    final db = await database;
    final maps = await db.query(
      'messages',
      where: 'conversation_id = ?',
      whereArgs: [conversationId],
      orderBy: 'created_at ASC',
    );
    return maps.map((m) => ChatMessage.fromMap(m)).toList();
  }

  /// 添加一条消息，同时更新对应对话的更新时间
  ///
  /// [message] 要添加的消息对象
  Future<void> addMessage(ChatMessage message) async {
    final db = await database;
    final map = message.toMap();
    map['conversation_id'] = message.conversationId;
    await db.insert('messages', map);
    if (message.conversationId != null) {
      await db.update(
        'conversations',
        {'updated_at': DateTime.now().millisecondsSinceEpoch},
        where: 'id = ?',
        whereArgs: [message.conversationId],
      );
    }
  }

  /// 删除一条消息
  ///
  /// [id] 消息 ID
  Future<void> deleteMessage(String id) async {
    final db = await database;
    await db.delete('messages', where: 'id = ?', whereArgs: [id]);
  }

  // ===================== 文件相关 =====================

  /// 获取所有文件列表，按创建时间倒序排列
  Future<List<KnowledgeFile>> getFiles() async {
    final db = await database;
    final maps = await db.query(
      'files',
      orderBy: 'created_at DESC',
    );
    return maps.map((m) => KnowledgeFile.fromMap(m)).toList();
  }

  /// 根据文件路径查找文件
  ///
  /// [filepath] 文件完整路径，不存在时返回 null
  Future<KnowledgeFile?> getFileByPath(String filepath) async {
    final db = await database;
    final maps = await db.query(
      'files',
      where: 'filepath = ?',
      whereArgs: [filepath],
    );
    if (maps.isEmpty) return null;
    return KnowledgeFile.fromMap(maps.first);
  }

  /// 添加文件记录（若已存在则直接返回现有记录）
  ///
  /// [filename] 文件名
  /// [filepath] 文件完整路径
  /// 返回文件对象
  Future<KnowledgeFile> addFile(String filename, String filepath) async {
    final db = await database;
    final existing = await getFileByPath(filepath);
    if (existing != null) return existing;

    final file = KnowledgeFile(
      id: _uuid.v4(),
      filename: filename,
      filepath: filepath,
      createdAt: DateTime.now(),
    );
    await db.insert('files', file.toMap());
    return file;
  }

  /// 更新文件是否已加入知识库的状态
  ///
  /// [id] 文件 ID
  /// [inKb] 是否已在知识库中
  Future<void> updateFileInKB(String id, bool inKb) async {
    final db = await database;
    await db.update(
      'files',
      {'in_kb': inKb ? 1 : 0},
      where: 'id = ?',
      whereArgs: [id],
    );
  }

  /// 删除文件记录
  ///
  /// [id] 文件 ID
  Future<void> deleteFile(String id) async {
    final db = await database;
    await db.delete('files', where: 'id = ?', whereArgs: [id]);
  }

  /// 根据文件路径删除文件记录
  ///
  /// [filepath] 文件完整路径
  Future<void> deleteFileByPath(String filepath) async {
    final db = await database;
    await db.delete('files', where: 'filepath = ?', whereArgs: [filepath]);
  }

  /// 清空所有文件记录
  Future<void> clearFiles() async {
    final db = await database;
    await db.delete('files');
  }

  // ===================== 关闭 =====================

  /// 关闭数据库连接
  Future<void> close() async {
    _db?.close();
    _db = null;
  }
}
