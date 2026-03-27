"""Tests for document ingestion hardening."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class AsyncContextManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MockAsyncClient:
    def __init__(self, response=None, stream_error=None):
        self._response = response
        self._stream_error = stream_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        if self._stream_error:
            raise self._stream_error
        return AsyncContextManager(self._response)


class TestIngestDocHardening:
    """Test ingestion security and limits."""

    @pytest.mark.asyncio
    async def test_rejects_non_http_schemes(self, covenant_compliant_project):
        """Verify that file://, ftp://, etc. are rejected."""
        from daem0nmcp.server import ingest_doc

        # These should all be rejected
        bad_urls = [
            "file:///etc/passwd",
            "ftp://example.com/file.txt",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
        ]

        for url in bad_urls:
            result = await ingest_doc(
                url=url, topic="test", project_path=covenant_compliant_project
            )
            assert "error" in result, f"Should reject {url}"
            assert (
                "scheme" in result["error"].lower()
                or "invalid" in result["error"].lower()
            )

    @pytest.mark.asyncio
    async def test_enforces_content_size_limit(self):
        """Verify large responses are truncated."""
        from daem0nmcp.server import MAX_CONTENT_SIZE, _fetch_and_extract

        # Mock a response that's too large
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers.get = MagicMock(return_value=None)
        mock_response.encoding = "utf-8"
        mock_response.extensions = {}

        async def _aiter_bytes():
            yield b"x" * (MAX_CONTENT_SIZE + 1000)

        mock_response.aiter_bytes = _aiter_bytes

        with patch("httpx.AsyncClient", return_value=MockAsyncClient(mock_response)):
            result = await _fetch_and_extract("https://example.com/large")

        # Should be truncated or return None
        assert result is None or len(result) <= MAX_CONTENT_SIZE

    @pytest.mark.asyncio
    async def test_enforces_chunk_limit(self, covenant_compliant_project):
        """Verify total chunks are limited."""
        from daem0nmcp.server import MAX_CHUNKS, ingest_doc

        with patch(
            "daem0nmcp.server._fetch_and_extract", new_callable=AsyncMock
        ) as mock_fetch:
            # Return content that would create many chunks
            mock_fetch.return_value = "word " * 100000  # Lots of words

            result = await ingest_doc(
                url="https://example.com/huge",
                topic="test",
                chunk_size=100,
                project_path=covenant_compliant_project,
            )

            if "error" not in result:
                assert result["chunks_created"] <= MAX_CHUNKS

    @pytest.mark.asyncio
    async def test_rejects_ssrf_urls(self, covenant_compliant_project):
        """Verify SSRF protection blocks localhost and private IPs."""
        from daem0nmcp.server import ingest_doc

        ssrf_urls = [
            "http://localhost/admin",
            "http://127.0.0.1/",
            "http://localhost.localdomain/",
            "http://169.254.169.254/",  # AWS metadata endpoint
        ]

        for url in ssrf_urls:
            result = await ingest_doc(
                url=url, topic="test", project_path=covenant_compliant_project
            )
            assert "error" in result, f"Should reject {url}: {result}"
            error_msg = result["error"].lower()
            assert any(
                term in error_msg
                for term in [
                    "localhost",
                    "private",
                    "internal",
                    "metadata",
                    "not allowed",
                ]
            ), f"Error message should mention security issue: {result['error']}"

    @pytest.mark.asyncio
    async def test_validates_chunk_size(self, covenant_compliant_project):
        """Verify chunk_size is validated."""
        from daem0nmcp.server import ingest_doc

        # Test negative chunk_size
        result = await ingest_doc(
            url="https://example.com",
            topic="test",
            chunk_size=-1,
            project_path=covenant_compliant_project,
        )
        assert "error" in result
        assert "positive" in result["error"].lower()

        # Test zero chunk_size
        result = await ingest_doc(
            url="https://example.com",
            topic="test",
            chunk_size=0,
            project_path=covenant_compliant_project,
        )
        assert "error" in result
        assert "positive" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_validates_topic(self, covenant_compliant_project):
        """Verify topic is validated."""
        from daem0nmcp.server import ingest_doc

        # Test empty topic
        result = await ingest_doc(
            url="https://example.com", topic="", project_path=covenant_compliant_project
        )
        assert "error" in result
        assert "empty" in result["error"].lower()

        # Test whitespace-only topic
        result = await ingest_doc(
            url="https://example.com",
            topic="   ",
            project_path=covenant_compliant_project,
        )
        assert "error" in result
        assert "empty" in result["error"].lower()


class TestIngestDocMocked:
    """Test ingest_doc with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_ingest_success_with_mocked_response(
        self, covenant_compliant_project
    ):
        """Verify successful ingestion with mocked HTTP."""
        from unittest.mock import patch

        from daem0nmcp.server import ingest_doc

        mock_content = "This is documentation about API usage. Use the /users endpoint for user operations."

        with patch(
            "daem0nmcp.server._fetch_and_extract", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = mock_content
            result = await ingest_doc(
                url="https://example.com/docs",
                topic="api-docs",
                project_path=covenant_compliant_project,
            )

        assert result.get("status") == "success"
        assert result["chunks_created"] >= 1
        assert result["topic"] == "api-docs"

    @pytest.mark.asyncio
    async def test_ingest_handles_timeout(self, covenant_compliant_project):
        """Verify timeout is handled gracefully."""
        from unittest.mock import patch

        from daem0nmcp.server import ingest_doc

        # When _fetch_and_extract returns None, ingest_doc returns an error
        with patch(
            "daem0nmcp.server._fetch_and_extract", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = None
            result = await ingest_doc(
                url="https://slow.example.com/docs",
                topic="slow-docs",
                project_path=covenant_compliant_project,
            )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_ingest_handles_http_error(self, covenant_compliant_project):
        """Verify HTTP errors are handled gracefully."""
        from unittest.mock import patch

        from daem0nmcp.server import ingest_doc

        # When _fetch_and_extract returns None, ingest_doc returns an error
        with patch(
            "daem0nmcp.server._fetch_and_extract", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = None
            result = await ingest_doc(
                url="https://example.com/missing",
                topic="missing",
                project_path=covenant_compliant_project,
            )

        assert "error" in result


class TestFetchAndExtract:
    """Test _fetch_and_extract HTTP handling directly."""

    @pytest.mark.asyncio
    async def test_fetch_handles_timeout(self):
        """Verify _fetch_and_extract handles httpx.TimeoutException."""
        from unittest.mock import patch

        import httpx

        from daem0nmcp.server import _fetch_and_extract

        with patch(
            "httpx.AsyncClient",
            return_value=MockAsyncClient(
                stream_error=httpx.TimeoutException("timeout")
            ),
        ):
            result = await _fetch_and_extract("https://slow.example.com/docs")

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_handles_http_error(self):
        """Verify _fetch_and_extract handles HTTPStatusError."""
        from unittest.mock import MagicMock, patch

        import httpx

        from daem0nmcp.server import _fetch_and_extract

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=MagicMock()
        )
        mock_response.headers.get = MagicMock(return_value=None)
        mock_response.encoding = "utf-8"
        mock_response.extensions = {}

        async def _aiter_bytes():
            if False:
                yield b""

        mock_response.aiter_bytes = _aiter_bytes

        with patch("httpx.AsyncClient", return_value=MockAsyncClient(mock_response)):
            result = await _fetch_and_extract("https://example.com/missing")

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_extracts_html_content(self):
        """Verify _fetch_and_extract properly extracts text from HTML."""
        from unittest.mock import MagicMock, patch

        from daem0nmcp.server import _fetch_and_extract

        mock_response = MagicMock()
        # Mock headers.get() to return None for content-length
        mock_response.headers.get = MagicMock(return_value=None)
        mock_response.raise_for_status = MagicMock()
        mock_response.encoding = "utf-8"
        mock_response.extensions = {}

        async def _aiter_bytes():
            yield b"<html><body><p>API documentation content</p></body></html>"

        mock_response.aiter_bytes = _aiter_bytes

        with patch("httpx.AsyncClient", return_value=MockAsyncClient(mock_response)):
            result = await _fetch_and_extract("https://example.com/docs")

        assert result is not None
        assert "API documentation content" in result
