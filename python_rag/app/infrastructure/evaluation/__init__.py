# -*- coding: utf-8 -*-
"""评测编排：切片参数组对比评测的运行事实、执行与聚合"""
from .evaluation_service import (
    MAX_EVALUATION_CHUNKS,
    EvaluationCancelled,
    EvaluationRunService,
)

__all__ = [
    "MAX_EVALUATION_CHUNKS",
    "EvaluationCancelled",
    "EvaluationRunService",
]
