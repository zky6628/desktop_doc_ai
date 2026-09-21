/// 请求序号防过期响应守卫
///
/// 每类请求持有独立守卫：发起前 [begin] 取序号，响应返回后
/// [isLatest] 判定是否仍为最新一次请求，过期响应直接丢弃。
class LatestRequestGuard {
  int _seq = 0;

  int begin() => ++_seq;

  bool isLatest(int seq) => seq == _seq;
}
