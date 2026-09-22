# -*- coding: utf-8 -*-
"""jieba 领域词典加载：专业术语切词完整性的可选扩展

jieba 默认词典会把领域专名与复合术语切碎（如"中芯国际"切成
"中芯/国际"），查询与索引两侧按碎片词元匹配会弱化关键词召回；
领域词典把术语注册为整词。词典内容来自知识库文档沉淀（外部数据，
不入代码），文件可选：缺失即使用默认词典。

格式：每行一个词条，空格分隔可选词频与词性；# 开头与空行为注释。
改变词典内容后，既有 FTS 索引仍按旧词元存储，需重建受影响索引
（重新导入或评测恢复重建）使两侧词元一致。
"""
from pathlib import Path

import jieba


def load_jieba_userdict(path: str) -> int:
    """加载领域词典到进程级 jieba 词典，返回加载词条数

    逐行解析而非直接使用 jieba.load_userdict：注释行与解析失败的行
    静默跳过（词典是用户手工维护的数据文件，个别坏行不应阻断启动）。
    词频缺省时由 jieba 按建议词频注册，保证整词不被再切分。

    :param path: 词典文件路径（UTF-8；文件不存在返回 0）
    :return: 成功注册的词条数
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return 0
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        word = parts[0]
        freq = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        tag = parts[2] if len(parts) > 2 else None
        jieba.add_word(word, freq=freq, tag=tag)
        count += 1
    return count
