import 'package:flutter/material.dart';
import 'package:desktop_drop/desktop_drop.dart';
import 'package:cross_file/cross_file.dart';
import 'package:provider/provider.dart';
import '../theme/colors.dart';
import '../models/drop_file_model.dart';

/// 拖拽区域组件
/// 默认状态：虚线边框 + 上传图标
/// 拖入状态：实线蓝色边框 + 释放图标 + 高亮背景
class DropZone extends StatefulWidget {
  /// 拖拽区域高度
  final double height;

  /// 拖入文件回调（若为 null 则使用 Provider 自动添加）
  final void Function(List<XFile>)? onFilesDropped;

  const DropZone({
    super.key,
    this.height = 150,
    this.onFilesDropped,
  });

  @override
  State<DropZone> createState() => _DropZoneState();
}

class _DropZoneState extends State<DropZone> {
  bool _isDragging = false;

  @override
  Widget build(BuildContext context) {
    return DropTarget(
      onDragEntered: (_) => setState(() => _isDragging = true),
      onDragExited: (_) => setState(() => _isDragging = false),
      onDragDone: (details) {
        setState(() => _isDragging = false);
        if (widget.onFilesDropped != null) {
          widget.onFilesDropped!(details.files);
        } else {
          context.read<DropFileModel>().addFiles(details.files);
        }
      },
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        height: widget.height,
        decoration: BoxDecoration(
          color: _isDragging
              ? AppColors.accentWithOpacity
              : AppColors.secondary,
          borderRadius: BorderRadius.circular(12),
          border: _isDragging
              ? Border.all(color: AppColors.accent, width: 2)
              : null,
        ),
        child: CustomPaint(
          painter: _isDragging ? null : _DashedBorderPainter(),
          child: Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                AnimatedSwitcher(
                  duration: const Duration(milliseconds: 200),
                  child: Icon(
                    _isDragging
                        ? Icons.download_rounded
                        : Icons.cloud_upload_outlined,
                    key: ValueKey(_isDragging),
                    size: 40,
                    color: _isDragging
                        ? AppColors.accent
                        : AppColors.textHint,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  _isDragging ? '释放以添加文件' : '拖拽文件到此处',
                  style: TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w500,
                    color: _isDragging
                        ? AppColors.accent
                        : AppColors.textSecondary,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  '支持多文件同时拖拽',
                  style: TextStyle(
                    fontSize: 11,
                    color: _isDragging
                        ? AppColors.accent.withValues(alpha: 0.7)
                        : AppColors.textHint,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// 虚线边框绘制器
class _DashedBorderPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = AppColors.textHint
      ..strokeWidth = 1.5
      ..style = PaintingStyle.stroke;

    const dashWidth = 6.0;
    const dashSpace = 4.0;
    const radius = 12.0;

    final rrect = RRect.fromRectAndRadius(
      Rect.fromLTWH(0.75, 0.75, size.width - 1.5, size.height - 1.5),
      const Radius.circular(radius),
    );

    // 使用 Path 绘制虚线圆角矩形
    final path = Path()..addRRect(rrect);
    final dashedPath = _createDashedPath(path, dashWidth, dashSpace);
    canvas.drawPath(dashedPath, paint);
  }

  /// 沿路径生成虚线
  Path _createDashedPath(Path source, double dashWidth, double dashSpace) {
    final result = Path();
    for (final metric in source.computeMetrics()) {
      double distance = 0;
      while (distance < metric.length) {
        final len = (distance + dashWidth).clamp(0.0, metric.length);
        result.addPath(metric.extractPath(distance, len), Offset.zero);
        distance += dashWidth + dashSpace;
      }
    }
    return result;
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
