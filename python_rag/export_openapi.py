# -*- coding: utf-8 -*-
"""
OpenAPI 基线导出脚本

无需启动服务器，直接调用 FastAPI app.openapi() 导出当前 API 规范，
写入 docs/workbench/openapi-baseline.json 作为基线快照。

用法（在 python_rag 目录下执行）：
    python export_openapi.py

校验方式：API 变更后重新执行本脚本，用 git diff 检查 openapi-baseline.json
的差异是否与本次 API 变更一致。
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

OUT_PATH = os.path.join(
    os.path.dirname(BASE_DIR), "docs", "workbench", "openapi-baseline.json"
)


def main():
    # 导入 main 会完成服务初始化（只读打开 Chroma），但不会启动 HTTP 服务
    from main import app

    spec = app.openapi()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print("openapi exported: %s (paths=%d)" % (OUT_PATH, len(spec.get("paths", {}))))


if __name__ == "__main__":
    main()
