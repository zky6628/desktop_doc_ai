import 'package:flutter/material.dart';
import 'package:cross_file/cross_file.dart';

/// 文件处理器工具类
/// 管理支持的文件格式、文件类型标签和图标
class FileHandler {
  FileHandler._();

  /// 支持的文件扩展名列表
  static const List<String> supportedExtensions = [
    '.pdf', '.txt', '.docx',
    '.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff',
  ];

  /// 获取文件扩展名（小写，含点号）
  static String getFileExtension(String fileName) {
    final dotIndex = fileName.lastIndexOf('.');
    if (dotIndex == -1 || dotIndex == fileName.length - 1) return '';
    return fileName.substring(dotIndex).toLowerCase();
  }

  /// 检查文件是否为支持的格式
  static bool isSupported(String fileName) {
    return supportedExtensions.contains(getFileExtension(fileName));
  }

  /// 获取文件类型标签（如 PDF / TXT / DOCX / IMG）
  static String getFileTypeLabel(String fileName) {
    final ext = getFileExtension(fileName);
    switch (ext) {
      case '.pdf':
        return 'PDF';
      case '.txt':
        return 'TXT';
      case '.docx':
        return 'DOCX';
      case '.jpg':
      case '.jpeg':
      case '.png':
      case '.bmp':
      case '.webp':
      case '.tiff':
        return 'IMG';
      default:
        return '未知';
    }
  }

  /// 获取文件类型对应的图标
  static IconData getFileTypeIcon(String fileName) {
    final ext = getFileExtension(fileName);
    switch (ext) {
      case '.pdf':
        return Icons.picture_as_pdf;
      case '.txt':
        return Icons.description;
      case '.docx':
        return Icons.article;
      case '.jpg':
      case '.jpeg':
      case '.png':
      case '.bmp':
      case '.webp':
      case '.tiff':
        return Icons.image;
      default:
        return Icons.insert_drive_file;
    }
  }

  /// 获取文件类型对应的颜色
  static Color getFileTypeColor(String fileName) {
    final ext = getFileExtension(fileName);
    switch (ext) {
      case '.pdf':
        return const Color(0xFFEF4444); // 红色
      case '.txt':
        return const Color(0xFF3B82F6); // 蓝色
      case '.docx':
        return const Color(0xFF2563EB); // 深蓝色
      case '.jpg':
      case '.jpeg':
      case '.png':
      case '.bmp':
      case '.webp':
      case '.tiff':
        return const Color(0xFF9333EA); // 紫色
      default:
        return const Color(0xFF6B7280); // 灰色
    }
  }

  /// 过滤文件列表，只保留支持的格式
  /// 返回 (支持的文件列表, 不支持的文件名列表)
  static (List<XFile>, List<String>) filterSupported(List<XFile> files) {
    final supported = <XFile>[];
    final unsupported = <String>[];
    for (final file in files) {
      if (isSupported(file.name)) {
        supported.add(file);
      } else {
        unsupported.add(file.name);
      }
    }
    return (supported, unsupported);
  }
}
