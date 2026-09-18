# -*- coding: utf-8 -*-
"""MinerU 客户端合同测试：提交/上传/轮询/下载的协议与安全行为

全部请求经 MockTransport 处理，零真实网络；断言覆盖请求形态
（认证头、批群体参数、上传无认证头）与错误映射（认证/配额/输入
拒绝/协议违规/瞬态）。
"""
import hashlib
import json

import httpx
import pytest

from app.domain.errors import (
    ArchiveRejectedError,
    CloudAuthError,
    CloudInputRejectedError,
    CloudProtocolViolationError,
    CloudQuotaError,
    CloudTransportError,
)
from app.infrastructure.mineru import (
    BatchFileEntry,
    MinerUClient,
)

TOKEN = "test-token"
ENTRIES = [
    BatchFileEntry(source_ref="ref-1", display_name="a.pdf"),
    BatchFileEntry(source_ref="ref-2", display_name="b.pdf"),
]


def _batch_response_body():
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "batch_id": "batch-1",
            "file_urls": ["https://oss.example.com/u1", "https://oss.example.com/u2"],
        },
    }


def _poll_response_body():
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "batch_id": "batch-1",
            "extract_result": [
                {"file_name": "b.pdf", "data_id": "ref-2", "state": "running"},
                {
                    "file_name": "a.pdf",
                    "data_id": "ref-1",
                    "state": "done",
                    "full_zip_url": "https://cdn.example.com/result.zip",
                },
            ],
        },
    }


def _client(handler) -> MinerUClient:
    return MinerUClient(
        api_token=TOKEN,
        transport=httpx.MockTransport(handler),
    )


def _write_temp_file(directory, name: str, content: bytes) -> str:
    """在临时目录写入字节文件并返回路径"""
    path = directory / name
    path.write_bytes(content)
    return str(path)


class TestCreateBatch:
    """批量提交：请求形态与错误映射"""

    def test_submit_maps_urls_by_position(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_batch_response_body())

        with _client(handler) as client:
            submission = client.create_batch(ENTRIES)
        assert submission.batch_id == "batch-1"
        assert submission.upload_urls[0].endswith("/u1")
        assert submission.upload_url_expires_at
        assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
        body = json.loads(requests[0].content)
        assert body["files"][0] == {"name": "a.pdf", "data_id": "ref-1"}
        assert body["files"][1]["data_id"] == "ref-2"

    def test_body_code_maps_to_categories(self):
        cases = [
            (-60018, CloudQuotaError),
            (-60005, CloudInputRejectedError),
            (-60009, CloudTransportError),
            (-99999, CloudProtocolViolationError),
        ]
        for code, expected in cases:
            def handler(request: httpx.Request, code=code) -> httpx.Response:
                return httpx.Response(200, json={"code": code, "msg": "x", "data": {}})

            with _client(handler) as client, pytest.raises(expected):
                client.create_batch(ENTRIES)

    def test_http_status_maps_to_categories(self):
        cases = [
            (401, CloudAuthError),
            (429, CloudTransportError),
            (500, CloudTransportError),
            (400, CloudProtocolViolationError),
        ]
        for status, expected in cases:
            def handler(request: httpx.Request, status=status) -> httpx.Response:
                return httpx.Response(status)

            with _client(handler) as client, pytest.raises(expected):
                client.create_batch(ENTRIES)

    def test_missing_batch_id_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": {}})

        with _client(handler) as client, pytest.raises(CloudProtocolViolationError):
            client.create_batch(ENTRIES)

    def test_url_count_mismatch_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _batch_response_body()
            body["data"]["file_urls"] = body["data"]["file_urls"][:1]
            return httpx.Response(200, json=body)

        with _client(handler) as client, pytest.raises(CloudProtocolViolationError):
            client.create_batch(ENTRIES)

    def test_network_failure_is_transient(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with _client(handler) as client, pytest.raises(CloudTransportError):
            client.create_batch(ENTRIES)


class TestUploadFile:
    """预签名上传：无认证头、显式长度、错误映射"""

    def test_put_streams_without_auth_header(self, tmp_path):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            captured["length"] = request.headers.get("Content-Length")
            captured["body"] = request.content
            return httpx.Response(200)

        path = _write_temp_file(tmp_path, "a.bin", b"x" * 2048)
        with _client(handler) as client:
            client.upload_file("https://oss.example.com/u1", path)
        assert captured["auth"] is None
        assert captured["length"] == "2048"
        assert captured["body"] == b"x" * 2048

    def test_upload_server_error_is_transient(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        path = _write_temp_file(tmp_path, "a.bin", b"content")
        with _client(handler) as client, pytest.raises(CloudTransportError):
            client.upload_file("https://oss.example.com/u1", path)


class TestPollBatch:
    """批量轮询：按 data_id 关联与状态映射"""

    def test_results_keyed_by_data_id(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/extract-results/batch/batch-1")
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json=_poll_response_body())

        with _client(handler) as client:
            result = client.poll_batch("batch-1")
        statuses = {status.source_ref: status for status in result.files}
        assert statuses["ref-1"].state == "done"
        assert statuses["ref-1"].full_zip_url == "https://cdn.example.com/result.zip"
        assert statuses["ref-2"].state == "running"
        # 状态摘要脱敏：不携带任何 URL
        assert "http" not in statuses["ref-1"].status_summary

    def test_running_progress_in_summary(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _poll_response_body()
            body["data"]["extract_result"][0]["state"] = "running"
            body["data"]["extract_result"][0]["extract_progress"] = {
                "extracted_pages": 1,
                "total_pages": 2,
            }
            return httpx.Response(200, json=body)

        with _client(handler) as client:
            result = client.poll_batch("batch-1")
        status = result.files[0]
        assert status.status_summary == "running 1/2 pages"

    def test_failed_state_carries_message(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _poll_response_body()
            body["data"]["extract_result"][0]["state"] = "failed"
            body["data"]["extract_result"][0]["err_msg"] = "文件格式不支持"
            return httpx.Response(200, json=body)

        with _client(handler) as client:
            result = client.poll_batch("batch-1")
        assert result.files[0].state == "failed"
        assert result.files[0].err_msg == "文件格式不支持"

    def test_missing_data_id_is_protocol_violation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _poll_response_body()
            del body["data"]["extract_result"][0]["data_id"]
            return httpx.Response(200, json=body)

        with _client(handler) as client, pytest.raises(CloudProtocolViolationError):
            client.poll_batch("batch-1")

    def test_unknown_state_is_protocol_violation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _poll_response_body()
            body["data"]["extract_result"][0]["state"] = "weird"
            return httpx.Response(200, json=body)

        with _client(handler) as client, pytest.raises(CloudProtocolViolationError):
            client.poll_batch("batch-1")


class TestDownloadResult:
    """结果下载：地址安全合同与大小限制"""

    # 官方结果托管域（默认允许列表成员）
    OFFICIAL_URL = "https://cdn-mineru.openxlab.org.cn/result.zip"

    @staticmethod
    def _download(handler, url, dest_path, max_bytes=10 * 1024 * 1024, **kwargs):
        with _client(handler) as client:
            return client.download_result(
                url, dest_path, max_bytes=max_bytes, **kwargs
            )

    def test_download_streams_and_hashes(self, tmp_path):
        content = b"zip-bytes" * 1000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=content)

        dest = tmp_path / "result.zip"
        digest = self._download(handler, self.OFFICIAL_URL, str(dest))
        assert digest == hashlib.sha256(content).hexdigest()
        assert dest.read_bytes() == content

    def test_non_https_rejected(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x")

        with pytest.raises(ArchiveRejectedError):
            self._download(
                handler,
                "http://cdn-mineru.openxlab.org.cn/result.zip",
                str(tmp_path / "a.zip"),
            )

    def test_host_outside_allowlist_rejected(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x")

        with pytest.raises(ArchiveRejectedError):
            self._download(
                handler,
                "https://evil.example.org/result.zip",
                str(tmp_path / "a.zip"),
            )

    def test_redirect_chain_validated(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cdn-mineru.openxlab.org.cn":
                return httpx.Response(
                    302, headers={"Location": "https://evil.example.org/x.zip"}
                )
            return httpx.Response(200, content=b"x")

        with pytest.raises(ArchiveRejectedError):
            self._download(
                handler, self.OFFICIAL_URL, str(tmp_path / "a.zip")
            )

    def test_declared_length_over_limit_rejected(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Length": "999999"}, content=b"tiny"
            )

        with pytest.raises(ArchiveRejectedError):
            self._download(
                handler,
                self.OFFICIAL_URL,
                str(tmp_path / "a.zip"),
                max_bytes=1024,
            )

    def test_redirect_count_over_limit_rejected(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"Location": "https://cdn-mineru.openxlab.org.cn/next.zip"},
            )

        with pytest.raises(ArchiveRejectedError):
            self._download(
                handler, self.OFFICIAL_URL, str(tmp_path / "a.zip")
            )

    def test_custom_suffix_list_honored(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"ok")

        dest = tmp_path / "a.zip"
        with _client(handler) as client:
            digest = client.download_result(
                "https://storage.internal/result.zip",
                str(dest),
                max_bytes=1024,
                host_suffixes=(".internal",),
            )
        assert digest == hashlib.sha256(b"ok").hexdigest()
