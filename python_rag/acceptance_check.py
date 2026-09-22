# -*- coding: utf-8 -*-
"""真机验收脚本：切片参数可调与评测链路的端到端检查

对运行中的后端服务（默认 127.0.0.1:8000）逐项执行验收并输出
PASS/FAIL/SKIP 汇总，退出码非零表示存在 FAIL。零费用项默认执行
（健康/参数往返/非法值拒绝/独占语义）；产生嵌入与生成调用的评测
全流程仅在 --with-evaluation 时执行（建议先在小知识库上验收）。

用法：
  python acceptance_check.py [--base http://127.0.0.1:8000]
                             [--with-evaluation]
"""
import argparse
import sys
import time

import httpx

# 评测问题集：问题内容不影响结构验收（回答失败也是有效数据点），
# 两组参数覆盖"参数变化产生新索引版本"的核心链路
_EVAL_QUESTIONS = ["知识库里讲了什么内容", "请概括主要主题"]
_EVAL_GROUPS = [
    {"parent_chunk_chars": 800, "child_chunk_chars": 300},
    {"parent_chunk_chars": 1200, "child_chunk_chars": 400},
]

# 评测运行终态轮询上限（秒）：小知识库几分钟内应完成
_EVAL_TIMEOUT_SECONDS = 900
_POLL_INTERVAL_SECONDS = 3


class Report:
    """验收结果收集器：逐项打印并保留失败清单"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.skips = 0

    def pass_(self, name: str, detail: str = "") -> None:
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        self.failures.append(name)

    def skip(self, name: str, reason: str) -> None:
        print(f"[SKIP] {name} — {reason}")
        self.skips += 1

    def summary(self) -> int:
        total = self.skips + len(self.failures)
        print()
        if self.failures:
            print(f"验收未通过：{len(self.failures)} 项 FAIL，{self.skips} 项 SKIP")
            for name in self.failures:
                print(f"  - {name}")
            return 1
        print(f"验收全部通过（{self.skips} 项跳过）")
        return 0


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=30)


def _data(response: httpx.Response) -> dict:
    """统一信封的 data 提取；非成功信封抛 RuntimeError"""
    body = response.json()
    if not body.get("success"):
        raise RuntimeError(f"响应信封失败: {body.get('error')}")
    return body["data"]


def _expect_status(report: Report, name: str, response: httpx.Response, expected: int) -> dict | None:
    if response.status_code != expected:
        report.fail(name, f"HTTP {response.status_code} != {expected}: {response.text[:200]}")
        return None
    return _data(response)


def check_health(client: httpx.Client, report: Report) -> None:
    """健康探针：整体状态与核心组件"""
    name = "健康探针 /health"
    response = client.get("/api/v1/health")
    data = _expect_status(report, name, response, 200)
    if data is None:
        return
    degraded = data.get("degraded", [])
    if data.get("status") == "healthy" and not degraded:
        report.pass_(name)
    else:
        report.fail(name, f"status={data.get('status')} degraded={degraded}")


def check_chunking_roundtrip(client: httpx.Client, report: Report) -> None:
    """在役切片参数：读取 → 写入覆盖 → 回读 → 恢复原值"""
    name = "切片参数读取"
    original = _expect_status(report, name, client.get("/api/v1/config/chunking"), 200)
    if original is None:
        return
    for key in ("parent_chunk_chars", "child_chunk_chars", "is_default"):
        if key not in original:
            report.fail(name, f"缺少字段 {key}")
            return
    report.pass_(name, f"当前值 {original['parent_chunk_chars']}/{original['child_chunk_chars']}")

    name = "切片参数写入与回读"
    written = _expect_status(
        report, name,
        client.put(
            "/api/v1/config/chunking",
            json={"parent_chunk_chars": 800, "child_chunk_chars": 300},
        ),
        200,
    )
    if written is None:
        return
    reread = _expect_status(report, name, client.get("/api/v1/config/chunking"), 200)
    if reread is None:
        return
    if (reread["parent_chunk_chars"], reread["child_chunk_chars"]) != (800, 300):
        report.fail(name, f"回读不一致: {reread}")
        return
    report.pass_(name, "覆盖值写入并回读一致")

    name = "切片参数恢复原值"
    restore = client.put(
        "/api/v1/config/chunking",
        json={
            "parent_chunk_chars": original["parent_chunk_chars"],
            "child_chunk_chars": original["child_chunk_chars"],
        },
    )
    if restore.status_code != 200:
        report.fail(name, f"恢复写入失败: {restore.status_code}")
        return
    report.pass_(name)

    name = "非法切片参数拒绝（子超父）"
    response = client.put(
        "/api/v1/config/chunking",
        json={"parent_chunk_chars": 300, "child_chunk_chars": 800},
    )
    error = response.json().get("error") or {}
    if response.status_code == 422 and error.get("code") == "INVALID_PARAM":
        report.pass_(name)
    else:
        report.fail(name, f"HTTP {response.status_code} code={error.get('code')}")

    name = "非法切片参数拒绝（非正数）"
    response = client.put(
        "/api/v1/config/chunking",
        json={"parent_chunk_chars": 0, "child_chunk_chars": 0},
    )
    if response.status_code == 422:
        report.pass_(name)
    else:
        report.fail(name, f"HTTP {response.status_code}")


def _pick_kb_with_documents(client: httpx.Client, report: Report) -> str | None:
    """选择第一个活动知识库（评测对象；无可用库时由调用方跳过）"""
    response = client.get("/api/v1/knowledge-bases")
    if response.status_code != 200:
        return None
    items = _data(response).get("items", [])
    for kb in items:
        if kb.get("status") == "active" and kb.get("deleted_at") is None:
            return kb["id"]
    return None


def check_debug_search(client: httpx.Client, report: Report) -> None:
    """调试检索两级匹配：本地调试开启时验证关键词路可召回"""
    name = "调试检索两级匹配"
    config = client.get("/api/v1/config/public")
    if config.status_code != 200:
        report.skip(name, "运行配置不可读")
        return
    features = _data(config).get("features", {})
    if not features.get("local_debug_enabled"):
        report.skip(name, "本地调试检索未开启（WORKBENCH_LOCAL_DEBUG=0）")
        return
    kb_id = _pick_kb_with_documents(client, report)
    if kb_id is None:
        report.skip(name, "无活动知识库")
        return
    response = client.post(
        "/api/v1/search",
        json={"knowledge_base_id": kb_id, "question": "这个知识库主要介绍什么内容"},
    )
    if response.status_code != 200:
        report.fail(name, f"HTTP {response.status_code}: {response.text[:200]}")
        return
    data = _data(response)
    stages = data.get("stages", {})
    # 自然语言提问（词元不相邻）命中即证明 AND 降级生效；
    # 知识库内容与问题无关时向量路仍应有命中
    if stages.get("vector_hits", 0) > 0:
        report.pass_(
            name,
            f"vector={stages.get('vector_hits')} keyword={stages.get('keyword_hits')}"
            f"（自然语言提问下关键词命中即 AND 降级生效，0 亦可能是内容无交集）",
        )
    else:
        report.fail(name, f"向量路零命中，检索链路异常: {stages}")


def check_evaluation_flow(client: httpx.Client, report: Report) -> None:
    """评测全流程：创建 → 独占 409 → 轮询终态 → 结果结构 → 参数恢复"""
    name = "评测创建前置（选定知识库）"
    kb_id = _pick_kb_with_documents(client, report)
    if kb_id is None:
        report.skip("评测全流程", "无活动知识库")
        return
    report.pass_(name, f"kb={kb_id}")

    original = _data(client.get("/api/v1/config/chunking"))
    body = {
        "knowledge_base_id": kb_id,
        "questions": _EVAL_QUESTIONS,
        "param_groups": _EVAL_GROUPS,
    }
    name = "评测运行创建（202）"
    created = client.post("/api/v1/evaluation-runs", json=body)
    data = _expect_status(report, name, created, 202)
    if data is None:
        return
    run_id = data["id"]
    task_id = data["task_id"]
    if data["state"] != "running":
        report.fail(name, f"初始状态非 running: {data['state']}")
        return
    report.pass_(name, f"run={run_id[:8]}")

    name = "评测独占语义（运行中创建被拒 409）"
    second = client.post("/api/v1/evaluation-runs", json=body)
    error = second.json().get("error") or {}
    if second.status_code == 409 and error.get("code") == "EVALUATION_RUNNING":
        report.pass_(name)
    else:
        report.fail(name, f"HTTP {second.status_code} code={error.get('code')}")

    name = "评测运行至终态"
    deadline = time.monotonic() + _EVAL_TIMEOUT_SECONDS
    run = None
    while time.monotonic() < deadline:
        run = _data(client.get(f"/api/v1/evaluation-runs/{run_id}"))
        if run["state"] != "running":
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    if run is None or run["state"] == "running":
        report.fail(name, f"超时未终态（可查任务 {task_id[:8]} 排查）")
        return
    if run["state"] != "completed":
        report.fail(name, f"终态非 completed: {run['state']} error={run.get('error_code')}")
        return
    report.pass_(name)

    name = "评测结果结构（组数/指标字段/参数组一致）"
    results = run.get("results") or []
    if len(results) != len(_EVAL_GROUPS):
        report.fail(name, f"结果组数 {len(results)} != {len(_EVAL_GROUPS)}")
        return
    expected_groups = {(g["parent_chunk_chars"], g["child_chunk_chars"]) for g in _EVAL_GROUPS}
    actual_groups = {
        (r["group"]["parent_chunk_chars"], r["group"]["child_chunk_chars"])
        for r in results
    }
    if actual_groups != expected_groups:
        report.fail(name, f"参数组不一致: {actual_groups}")
        return
    required_metrics = {
        "total", "completed", "failed", "refused",
        "ttft_p50_ms", "ttft_p95_ms", "input_tokens", "output_tokens",
    }
    for index, result in enumerate(results):
        missing = required_metrics - set(result.get("metrics", {}))
        if missing:
            report.fail(name, f"第 {index + 1} 组缺指标字段: {missing}")
            return
        if result["metrics"]["total"] != len(_EVAL_QUESTIONS):
            report.fail(name, f"第 {index + 1} 组问题数不符: {result['metrics']['total']}")
            return
    report.pass_(name)

    name = "评测后切片参数恢复"
    restored = _data(client.get("/api/v1/config/chunking"))
    if (
        restored["parent_chunk_chars"] == original["parent_chunk_chars"]
        and restored["child_chunk_chars"] == original["child_chunk_chars"]
    ):
        report.pass_(name)
    else:
        report.fail(name, f"参数未恢复: {restored}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="切片参数与评测链路真机验收")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument(
        "--with-evaluation",
        action="store_true",
        help="执行评测全流程（产生嵌入与生成调用，建议小知识库）",
    )
    args = parser.parse_args(argv)

    report = Report()
    with _client(args.base) as client:
        try:
            check_health(client, report)
            check_chunking_roundtrip(client, report)
            check_debug_search(client, report)
            if args.with_evaluation:
                check_evaluation_flow(client, report)
            else:
                report.skip(
                    "评测全流程",
                    "未指定 --with-evaluation（评测产生嵌入与生成调用）",
                )
        except httpx.ConnectError:
            print(f"[FAIL] 后端不可达: {args.base}（请先启动 python main.py）")
            return 2
    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
