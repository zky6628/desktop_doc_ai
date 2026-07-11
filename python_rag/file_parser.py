# -*- coding: utf-8 -*-
"""
文件解析器模块
支持 TXT、DOCX、PDF、图片（JPEG/PNG/BMP/WEBP/TIFF）的文本提取
- TXT: 自动识别编码（UTF-8/GBK），使用 txtai 进行文本分段
- DOCX: 使用 python-docx 提取段落文本
- PDF: 使用 pypdf 提取文本
- 图片: 使用 RapidOCR（基于 ONNX Runtime）进行文字识别，支持中英文，无需 paddlepaddle
"""

import os
import sys
from pathlib import Path
from typing import List

# txtai 文本分段管道
from txtai.pipeline import Segmentation

# python-docx 文档解析
from docx import Document

# pypdf PDF 解析
from pypdf import PdfReader

# Pillow 图片处理
from PIL import Image

# RapidOCR 全局单例（懒加载，首次使用时初始化）
_ocr_reader = None


# ===================== 支持的文件格式 =====================

SUPPORTED_EXTENSIONS: List[str] = ['.txt', '.docx', '.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff']

# 图片格式集合
IMAGE_EXTENSIONS: set = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff'}

# OCR 语言设置：中文简体 + 英文（easyocr 语言代码）
OCR_LANGS: List[str] = ['ch_sim', 'en']


# ===================== OCR 懒加载 =====================

def _get_ocr_reader():
    """
    获取 OCR 读取器（懒加载单例）
    首次调用时初始化 RapidOCR，之后复用
    RapidOCR 基于 ONNX Runtime，无需安装 paddlepaddle，模型内置
    :return: RapidOCR 实例
    :raises ValueError: RapidOCR 未安装或初始化失败时抛出
    """
    global _ocr_reader
    if _ocr_reader is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_reader = RapidOCR()
        except ImportError:
            raise ValueError(
                "RapidOCR 未安装，请先执行 pip install rapidocr_onnxruntime 后再使用图片识别功能"
            )
        except Exception as e:
            raise ValueError(f"图片 OCR 初始化失败: {str(e)}")
    return _ocr_reader


# ===================== 编码检测 =====================

def _detect_encoding(file_path: str) -> str:
    """
    自动检测文本文件编码（UTF-8 / GBK）
    依次尝试 UTF-8 和 GBK，返回第一个能正确解码的编码
    :param file_path: 文件路径
    :return: 编码名称字符串
    :raises ValueError: 无法识别编码时抛出
    """
    # 尝试 UTF-8
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            f.read()
        return 'utf-8'
    except UnicodeDecodeError:
        pass

    # 尝试 GBK
    try:
        with open(file_path, 'r', encoding='gbk') as f:
            f.read()
        return 'gbk'
    except UnicodeDecodeError:
        pass

    raise ValueError("无法解析文件编码，请使用 UTF-8 或 GBK 编码的文本文件")


# ===================== TXT 文件解析 =====================

def read_txt(file_path: str) -> str:
    """
    读取 TXT 文件，自动识别编码（UTF-8/GBK）
    使用 txtai 的 Segmentation 管道进行段落分割，保留文本结构
    :param file_path: TXT 文件路径
    :return: 提取的文本内容
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 文件为空或编码无法识别
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if os.path.getsize(file_path) == 0:
        raise ValueError("文件内容为空")

    # 自动检测编码
    encoding = _detect_encoding(file_path)

    # 读取文件内容
    with open(file_path, 'r', encoding=encoding) as f:
        text = f.read()

    if not text.strip():
        raise ValueError("文件内容为空")

    # 使用 txtai 的 Segmentation 进行段落分割
    segmenter = Segmentation(paragraphs=True)
    segments = segmenter(text)

    # 将分段结果合并为完整文本
    return "\n\n".join(segments)


# ===================== DOCX 文件解析 =====================

def read_docx(file_path: str) -> str:
    """
    读取 DOCX 文件，提取段落文本
    使用 python-docx 库遍历所有段落，过滤空段落
    :param file_path: DOCX 文件路径
    :return: 提取的文本内容（段落间以空行分隔）
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 文件为空或解析失败
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    try:
        doc = Document(file_path)
    except Exception as e:
        raise ValueError(f"DOCX 文件解析失败: {str(e)}")

    # 提取所有非空段落文本
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)

    if not paragraphs:
        raise ValueError("文件内容为空")

    return "\n\n".join(paragraphs)


# ===================== PDF 文件解析 =====================

def read_pdf(file_path: str) -> str:
    """
    读取 PDF 文件，提取文本
    使用 pypdf 库逐页提取文本内容
    :param file_path: PDF 文件路径
    :return: 提取的文本内容（页间以空行分隔）
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 文件为空或解析失败
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    try:
        reader = PdfReader(file_path)
    except Exception as e:
        raise ValueError(f"PDF 文件解析失败: {str(e)}")

    if len(reader.pages) == 0:
        raise ValueError("文件内容为空")

    # 逐页提取文本
    text_parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text and text.strip():
            text_parts.append(text.strip())

    if not text_parts:
        raise ValueError("文件内容为空（可能是扫描版 PDF，无文本层）")

    return "\n\n".join(text_parts)


# ===================== 图片 OCR 解析 =====================

def read_image(file_path: str) -> str:
    """
    读取图片文件，通过 OCR 提取文本
    使用 RapidOCR（基于 ONNX Runtime）进行文字识别，支持中英文
    :param file_path: 图片文件路径
    :return: 提取的文本内容
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 文件为空或 OCR 识别失败
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    if os.path.getsize(file_path) == 0:
        raise ValueError("文件内容为空")

    try:
        # 获取 OCR 读取器（懒加载）
        reader = _get_ocr_reader()

        # 使用 RapidOCR 进行文字识别
        # results 格式: [[边界框, 文本, 置信度], ...]
        results, elapse = reader(file_path)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"图片 OCR 识别失败: {str(e)}")

    if not results:
        raise ValueError("图片中未识别到文字内容")

    # 提取文本内容（results 中每项为 [box, text, score]）
    text_lines = []
    for item in results:
        if len(item) >= 2 and item[1]:
            text = item[1].strip()
            if text:
                text_lines.append(text)

    if not text_lines:
        raise ValueError("图片中未识别到文字内容")

    return "\n".join(text_lines)


# ===================== 统一解析入口 =====================

def parse_file(file_path: str) -> str:
    """
    统一文件解析入口
    根据文件扩展名自动选择对应的解析器
    :param file_path: 文件路径
    :return: 提取的文本内容
    :raises FileNotFoundError: 文件不存在
    :raises ValueError: 不支持的格式或文件内容为空
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    ext = Path(file_path).suffix.lower()

    if ext == '.txt':
        return read_txt(file_path)
    elif ext == '.docx':
        return read_docx(file_path)
    elif ext == '.pdf':
        return read_pdf(file_path)
    elif ext in IMAGE_EXTENSIONS:
        return read_image(file_path)
    else:
        raise ValueError(
            f"不支持的文件格式: {ext}，支持的格式: {', '.join(SUPPORTED_EXTENSIONS)}"
        )
