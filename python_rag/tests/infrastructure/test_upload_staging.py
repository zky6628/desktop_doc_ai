# -*- coding: utf-8 -*-
"""上传流式暂存与文件安全测试：
大小/类型/容器校验、原子落位、磁盘预检与过期清理"""
import hashlib
import io
import os
import time
import zipfile

import pytest

from app.domain import file_policy
from app.domain.errors import (
    EmptyFileError,
    FileTooLargeError,
    InsufficientDiskSpaceError,
    UnsupportedFormatError,
)
from app.infrastructure.storage import UploadStagingStore


@pytest.fixture()
def store(tmp_path):
    """临时受控暂存目录"""
    root = str(tmp_path / "staging")
    return UploadStagingStore(root), root


def _minimal_docx_bytes() -> bytes:
    """构造可被 magic 检测识别的最小 DOCX（ZIP 容器 + Word 内容目录）"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


def test_stage_text_file_records_facts_and_no_temp_residue(store):
    """文本文件暂存：哈希/大小/类型事实完整，临时文件无残留"""
    stager, root = store
    content = b"hello "
    staged = stager.stage([content, b"world"], "报告.txt")

    assert staged.sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert staged.size_bytes == 11
    assert staged.extension == "txt"
    assert staged.mime_type == "text/plain"
    assert staged.display_name == "报告.txt"
    assert os.path.isfile(staged.staging_path)
    with open(staged.staging_path, "rb") as f:
        assert f.read() == b"hello world"

    # 原子落位后无 .part 残留
    assert [name for name in os.listdir(root) if name.endswith(".part")] == []


def test_stage_rejects_oversize_stream_without_residue(store, monkeypatch):
    """流式超限即时中断：抛大小超限错误且暂存目录无残留"""
    stager, root = store
    monkeypatch.setattr(file_policy, "MAX_FILE_BYTES", 8)

    with pytest.raises(FileTooLargeError):
        stager.stage([b"12345", b"67890"], "数据.txt")

    assert os.listdir(root) == []


def test_stage_rejects_empty_content(store):
    """空内容拒绝暂存"""
    stager, root = store
    with pytest.raises(EmptyFileError):
        stager.stage([], "空.txt")
    assert os.listdir(root) == []


def test_stage_rejects_disallowed_or_missing_extension(store):
    """扩展名不在白名单或缺失时前置拒绝，不写任何文件"""
    stager, root = store
    with pytest.raises(UnsupportedFormatError):
        stager.stage([b"binary"], "程序.exe")
    with pytest.raises(UnsupportedFormatError):
        stager.stage([b"text"], "无扩展名")
    assert os.listdir(root) == []


def test_stage_rejects_forged_extension(store):
    """伪造扩展名：文本内容伪装成 PDF 被真实格式校验拒绝"""
    stager, root = store
    with pytest.raises(UnsupportedFormatError):
        stager.stage([b"just plain text"], "伪装.pdf")
    assert os.listdir(root) == []


def test_stage_accepts_real_pdf(store):
    """真实 PDF（%PDF 头）通过校验"""
    stager, _ = store
    staged = stager.stage([b"%PDF-1.4\n1 0 obj\n%%EOF\n"], "文档.pdf")
    assert staged.extension == "pdf"
    assert staged.mime_type == "application/pdf"
    assert os.path.isfile(staged.staging_path)


def test_stage_rejects_encrypted_pdf(store):
    """携带加密字典的 PDF 明确拒绝"""
    stager, _ = store
    encrypted = b"%PDF-1.4\n/Encrypt 2 0 R\n1 0 obj\n%%EOF\n"
    with pytest.raises(UnsupportedFormatError):
        stager.stage([encrypted], "加密.pdf")


def test_stage_docx_roundtrip(store):
    """真实 DOCX（ZIP 容器 + Word 内容目录）通过校验"""
    stager, _ = store
    staged = stager.stage([_minimal_docx_bytes()], "文档.docx")
    assert staged.extension == "docx"
    assert staged.mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert os.path.isfile(staged.staging_path)


def test_stage_rejects_docx_renamed_text(store):
    """文本内容伪装成 DOCX：无 ZIP 容器特征被拒绝"""
    stager, _ = store
    with pytest.raises(UnsupportedFormatError):
        stager.stage([b"plain text"], "伪装.docx")


def test_stage_mime_consistency(store):
    """声明 MIME 与扩展名矛盾才拒绝；通用类型与缺失放行"""
    stager, _ = store
    with pytest.raises(UnsupportedFormatError):
        stager.stage([b"text"], "a.txt", declared_mime="application/pdf")

    # 通用二进制类型与未声明均放行
    staged = stager.stage([b"text"], "a.txt", declared_mime="application/octet-stream")
    assert staged.mime_type == "text/plain"
    staged = stager.stage([b"text"], "a.txt", declared_mime=None)
    assert staged.mime_type == "text/plain"


def test_stage_prechecks_free_disk(store, monkeypatch):
    """磁盘预检：剩余空间不足时拒绝接收"""
    stager, root = store
    monkeypatch.setattr(file_policy, "MIN_FREE_DISK_BYTES", 1 << 40)

    with pytest.raises(InsufficientDiskSpaceError):
        stager.stage([b"content"], "a.txt")
    assert os.listdir(root) == []


def test_staging_names_are_random_and_contained(store):
    """落盘名随机（不拼用户文件名）且全部位于受控目录内"""
    stager, root = store
    first = stager.stage([b"same"], "同名.txt")
    second = stager.stage([b"same"], "同名.txt")

    assert first.staging_path != second.staging_path
    assert "同名" not in first.staging_path
    for staged in (first, second):
        assert os.path.realpath(staged.staging_path).startswith(
            os.path.realpath(root) + os.sep
        )


def test_cleanup_removes_only_stale_entries(store):
    """过期清理：只删除超期条目，新暂存保留；占用条目跳过不报错"""
    stager, root = store
    staged = stager.stage([b"fresh"], "新.txt")
    stale_path = os.path.join(root, "00000000-0000-7000-8000-000000000000.txt")
    with open(stale_path, "wb") as f:
        f.write(b"stale")
    stale_time = time.time() - (file_policy.STALE_STAGING_MAX_AGE_HOURS + 1) * 3600
    os.utime(stale_path, (stale_time, stale_time))

    assert stager.cleanup_stale_staging() == 1
    assert not os.path.exists(stale_path)
    assert os.path.isfile(staged.staging_path)

    # 重复清理幂等
    assert stager.cleanup_stale_staging() == 0
