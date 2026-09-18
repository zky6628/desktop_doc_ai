# -*- coding: utf-8 -*-
"""结果压缩包安全解压测试：穿越/链接/炸弹/白名单/阈值防御"""
import zipfile

import pytest

from app.domain.errors import ArchiveRejectedError
from app.infrastructure.mineru import archive as archive_module
from app.infrastructure.mineru.archive import extract_archive


def _make_zip(path, entries: dict[str, bytes], external_attrs: dict[str, int] | None = None):
    """构造测试压缩包（deflate 压缩，压缩比校验依赖真实压缩后的尺寸）

    :param path: 目标 zip 路径
    :param entries: 条目名 -> 内容
    :param external_attrs: 条目名 -> 外部属性（模拟 unix 模式位）
    """
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (external_attrs or {}).get(name, 0) << 16
            zf.writestr(info, content)
    return str(path)


class TestNormalArchive:
    """正常产物解压"""

    def test_expects_products_extracted(self, tmp_path):
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {
                "full.md": "# 标题\n".encode(),
                "layout.json": b"{}",
                "images/p1.jpg": b"\xff\xd8fake-jpeg",
                "images/p2.png": b"\x89PNG",
                "main.html": b"<p>html</p>",
            },
        )
        dest = tmp_path / "extracted"
        extracted = extract_archive(zip_path, str(dest))
        assert extracted == [
            "full.md",
            "layout.json",
            "images/p1.jpg",
            "images/p2.png",
            "main.html",
        ]
        assert (dest / "full.md").read_text(encoding="utf-8") == "# 标题\n"
        assert (dest / "images" / "p1.jpg").read_bytes() == b"\xff\xd8fake-jpeg"

    def test_origin_input_echo_skipped(self, tmp_path):
        # 供应方随包回传原始输入副本（任意扩展名）：跳过不落盘，
        # 也不影响解析内容的解压
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {
                "full.md": "# 标题\n".encode(),
                "01e00cfb-89af-4d63-9460-96e8be9ac584_origin.pdf": b"%PDF-echo",
            },
        )
        dest = tmp_path / "extracted"
        extracted = extract_archive(zip_path, str(dest))
        assert extracted == ["full.md"]
        assert not (dest / "01e00cfb-89af-4d63-9460-96e8be9ac584_origin.pdf").exists()

    def test_content_file_with_origin_like_name_kept(self, tmp_path):
        # 名称中包含 origin 但不符合回传副本命名（<hash>_origin.<ext>）的
        # 普通内容文件不受跳过规则影响
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {"my_origin_notes.md": "笔记".encode()},
        )
        extracted = extract_archive(zip_path, str(tmp_path / "extracted"))
        assert extracted == ["my_origin_notes.md"]


class TestPathSafety:
    """路径穿越与绝对路径拒绝"""

    @pytest.mark.parametrize(
        "entry", ["../evil.txt", "a/../../evil.txt", "/abs/evil.txt", "C:/evil.txt"]
    )
    def test_traversal_entries_rejected(self, tmp_path, entry):
        zip_path = _make_zip(tmp_path / "result.zip", {entry: b"x"})
        dest = tmp_path / "extracted"
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(dest))
        assert not dest.exists()

    def test_escape_after_normalization_rejected(self, tmp_path):
        # 归一化后仍逃出目标目录的深层穿越
        zip_path = _make_zip(tmp_path / "result.zip", {"a/../../evil.md": b"x"})
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))


class TestEntryTypeSafety:
    """非常规条目类型拒绝"""

    def test_symlink_entry_rejected(self, tmp_path):
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {"link.md": b"target"},
            external_attrs={"link.md": 0o120777},
        )
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))

    def test_device_entry_rejected(self, tmp_path):
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {"dev.md": b"device"},
            external_attrs={"dev.md": 0o060666},
        )
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))


class TestSizeLimits:
    """大小/数量/压缩比阈值（测试用小阈值注入验证行为）"""

    def test_bomb_ratio_rejected(self, tmp_path):
        # 8MB 全零内容压缩后远小于 4KB 阈值，压缩比超限
        zip_path = _make_zip(tmp_path / "bomb.zip", {"big.md": b"\x00" * (8 * 1024 * 1024)})
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))

    def test_entry_size_limit_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(archive_module, "MAX_ENTRY_BYTES", 10)
        zip_path = _make_zip(tmp_path / "result.zip", {"big.md": b"x" * 11})
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))

    def test_total_size_limit_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(archive_module, "MAX_TOTAL_BYTES", 100)
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {"a.md": b"x" * 60, "b.md": b"y" * 60},
        )
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))

    def test_entry_count_limit_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(archive_module, "MAX_ENTRY_COUNT", 2)
        zip_path = _make_zip(
            tmp_path / "result.zip",
            {"a.md": b"1", "b.md": b"2", "c.md": b"3"},
        )
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))


class TestExtensionAllowlist:
    """扩展名白名单"""

    def test_disallowed_extension_rejected(self, tmp_path):
        zip_path = _make_zip(tmp_path / "result.zip", {"run.exe": b"MZ"})
        with pytest.raises(ArchiveRejectedError):
            extract_archive(zip_path, str(tmp_path / "extracted"))

    def test_extension_case_insensitive(self, tmp_path):
        zip_path = _make_zip(tmp_path / "result.zip", {"FULL.MD": b"# ok"})
        extracted = extract_archive(zip_path, str(tmp_path / "extracted"))
        assert extracted == ["FULL.MD"]
