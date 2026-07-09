import 'package:flutter/material.dart';

/// 三点跳动加载动画
/// 三个圆点依次上下跳动，形成等待效果
class LoadingDots extends StatefulWidget {
  /// 圆点颜色
  final Color color;

  /// 圆点半径
  final double radius;

  /// 圆点间距
  final double spacing;

  /// 跳动幅度
  final double bounceHeight;

  const LoadingDots({
    super.key,
    this.color = const Color(0xFF3B82F6),
    this.radius = 6,
    this.spacing = 8,
    this.bounceHeight = 12,
  });

  @override
  State<LoadingDots> createState() => _LoadingDotsState();
}

class _LoadingDotsState extends State<LoadingDots>
    with TickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: List.generate(3, (index) {
        return AnimatedBuilder(
          animation: _controller,
          builder: (context, child) {
            // 每个点延迟启动：0, 0.2, 0.4
            final delay = index * 0.2;
            final t = (_controller.value - delay) % 1.0;

            // 使用正弦曲线模拟弹跳：0→上→0
            final bounce = (t < 0.5)
                ? (1 - (2 * t).abs()) * widget.bounceHeight
                : 0.0;

            // 透明度随跳动变化
            final opacity = 0.4 + 0.6 * (1 - (2 * t - 1).abs().clamp(0.0, 1.0));

            return Container(
              margin: EdgeInsets.symmetric(horizontal: widget.spacing / 2),
              transform: Matrix4.translationValues(0, -bounce, 0),
              child: Opacity(
                opacity: opacity.clamp(0.3, 1.0),
                child: Container(
                  width: widget.radius * 2,
                  height: widget.radius * 2,
                  decoration: BoxDecoration(
                    color: widget.color,
                    shape: BoxShape.circle,
                  ),
                ),
              ),
            );
          },
        );
      }),
    );
  }
}
