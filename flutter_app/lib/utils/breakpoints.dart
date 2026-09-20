/// 窗口宽度三档断点：AppShell 与页面响应式形态的唯一判定来源
enum WindowBreakpoint { compact, medium, expanded }

class Breakpoints {
  Breakpoints._();

  /// 中档最小宽度（含）；低于此值进入紧凑档
  static const double mediumMin = 800;

  /// 宽档最小宽度（含）；导航此时展示文字标签
  static const double expandedMin = 1100;

  static WindowBreakpoint of(double width) {
    if (width >= expandedMin) return WindowBreakpoint.expanded;
    if (width >= mediumMin) return WindowBreakpoint.medium;
    return WindowBreakpoint.compact;
  }
}
