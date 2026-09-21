import 'package:flutter/foundation.dart';

/// 全局服务地址单一事实源：全部 API 客户端与连接探测由此取址
///
/// 设置页保存新地址后调用 [update]：后续请求立即走新地址；进行中的
/// 请求与 SSE 连接不迁移，按各自结果自然收尾。监听方（如顶栏探测）
/// 在地址变更后立即探测新地址。
class ServerAddressStore extends ChangeNotifier {
  ServerAddressStore(Uri initial) : _value = initial;

  Uri _value;

  /// 当前服务地址
  Uri get value => _value;

  /// 解析并校验用户输入的服务地址：要求 http/https 且主机非空，
  /// 非法输入返回 null 由调用方表达错误
  static Uri? tryParse(String raw) {
    final uri = Uri.tryParse(raw.trim());
    if (uri == null || !uri.hasScheme) return null;
    if (uri.scheme != 'http' && uri.scheme != 'https') return null;
    if (uri.host.isEmpty) return null;
    return uri;
  }

  /// 切换服务地址（同值不通知）
  void update(Uri uri) {
    if (uri == _value) return;
    _value = uri;
    notifyListeners();
  }
}
