/// 知识库文件模型
///
/// 表示一个已导入的文件，记录其基本信息和是否已加入向量知识库。
class KnowledgeFile {
  /// 文件唯一标识（UUID）
  final String id;

  /// 文件名
  final String filename;

  /// 文件完整路径
  final String filepath;

  /// 导入时间
  final DateTime createdAt;

  /// 是否已加入向量知识库
  final bool inKnowledgeBase;

  KnowledgeFile({
    required this.id,
    required this.filename,
    required this.filepath,
    required this.createdAt,
    this.inKnowledgeBase = false,
  });

  /// 从数据库 Map 构造文件对象
  factory KnowledgeFile.fromMap(Map<String, dynamic> map) {
    return KnowledgeFile(
      id: map['id'] as String,
      filename: map['filename'] as String,
      filepath: map['filepath'] as String,
      createdAt: DateTime.fromMillisecondsSinceEpoch(map['created_at'] as int),
      inKnowledgeBase: (map['in_kb'] as int? ?? 0) == 1,
    );
  }

  /// 转换为数据库存储用的 Map
  Map<String, dynamic> toMap() {
    return {
      'id': id,
      'filename': filename,
      'filepath': filepath,
      'created_at': createdAt.millisecondsSinceEpoch,
      'in_kb': inKnowledgeBase ? 1 : 0,
    };
  }

  /// 创建副本并修改指定字段
  ///
  /// 用于不可变对象的部分更新。
  KnowledgeFile copyWith({
    String? id,
    String? filename,
    String? filepath,
    DateTime? createdAt,
    bool? inKb,
  }) {
    return KnowledgeFile(
      id: id ?? this.id,
      filename: filename ?? this.filename,
      filepath: filepath ?? this.filepath,
      createdAt: createdAt ?? this.createdAt,
      inKnowledgeBase: inKb ?? inKnowledgeBase,
    );
  }
}
