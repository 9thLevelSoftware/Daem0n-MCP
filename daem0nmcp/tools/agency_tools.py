"""Agency tools: execute_python, compress_context, ingest_doc."""

import asyncio
import logging
import re
from functools import partial
from typing import Any

try:
    from .. import __version__
    from ..agency import (
        CapabilityManager,
        CapabilityScope,
        SandboxExecutor,
        check_capability,
    )
    from ..bounded_workers import BoundedWorkerPool
    from ..config import settings
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from ..covenant import legacy_entrypoint
    from ..logging_config import with_request_id
    from ..mcp_instance import mcp
except ImportError:
    from daem0nmcp import __version__
    from daem0nmcp.agency import (
        CapabilityManager,
        CapabilityScope,
        SandboxExecutor,
        check_capability,
    )
    from daem0nmcp.bounded_workers import BoundedWorkerPool
    from daem0nmcp.config import settings
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
    from daem0nmcp.covenant import legacy_entrypoint
    from daem0nmcp.logging_config import with_request_id
    from daem0nmcp.mcp_instance import mcp

logger = logging.getLogger(__name__)

# Agency globals
_sandbox_executor = SandboxExecutor(timeout_seconds=30)
_capability_manager = CapabilityManager()

# Ingestion limits
MAX_CONTENT_SIZE = settings.max_content_size
MAX_CHUNKS = settings.max_chunks
INGEST_TIMEOUT = settings.ingest_timeout
ALLOWED_URL_SCHEMES = settings.allowed_url_schemes
_HTML_WORKER_POOL = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-html",
)


def _extract_html_text(
    body: bytes,
    encoding: str | None,
    beautiful_soup: Any,
) -> str:
    """Decode, parse, and normalize one bounded HTML response off-loop."""
    text = body.decode(encoding or "utf-8", errors="replace")
    soup = beautiful_soup(text, "html.parser")

    for element in soup(["script", "style", "nav", "footer", "header"]):
        element.decompose()

    text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


async def _validate_url(url: str, *, resolver: Any | None = None) -> str | None:
    """Validate syntax and all admission-time answers without authorizing a dial."""
    try:
        from ..pinned_http import validate_public_url
    except ImportError:
        try:
            from daem0nmcp.pinned_http import validate_public_url
        except ImportError:
            return "URL ingestion dependencies are not installed"

    return await validate_public_url(
        url,
        allowed_schemes=ALLOWED_URL_SCHEMES,
        resolver=resolver,
    )


async def _fetch_and_extract(
    url: str,
    *,
    resolver: Any | None = None,
    delegate: Any | None = None,
) -> str | None:
    """Fetch URL and extract text content with size limits."""
    try:
        import httpx
        from bs4 import BeautifulSoup

        from ..pinned_http import (
            PinnedAsyncHTTPTransport,
            pinned_dependency_log_scope,
            read_bounded_identity_body,
        )
    except ImportError:
        try:
            import httpx
            from bs4 import BeautifulSoup

            from daem0nmcp.pinned_http import (
                PinnedAsyncHTTPTransport,
                pinned_dependency_log_scope,
                read_bounded_identity_body,
            )
        except ImportError:
            return None

    response = None
    try:
        transport = PinnedAsyncHTTPTransport(
            resolver=resolver,
            delegate=delegate,
        )
        with pinned_dependency_log_scope():
            async with (
                httpx.AsyncClient(
                    timeout=float(INGEST_TIMEOUT),
                    follow_redirects=False,
                    trust_env=False,
                    headers={"Accept-Encoding": "identity"},
                    transport=transport,
                ) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                body = await read_bounded_identity_body(
                    response,
                    max_bytes=MAX_CONTENT_SIZE,
                )

        encoding = response.encoding if response else "utf-8"
        return await _HTML_WORKER_POOL.run(
            partial(_extract_html_text, body, encoding, BeautifulSoup)
        )

    except Exception as error:
        logger.error("URL ingestion fetch failed (%s)", type(error).__name__)
        return None


async def _validate_and_fetch_with_deadline(
    url: str,
    *,
    timeout_seconds: float | None = None,
) -> tuple[str | None, str | None]:
    """Share one wall-clock deadline across admission DNS and response fetch."""
    timeout = float(INGEST_TIMEOUT if timeout_seconds is None else timeout_seconds)

    async def operation() -> tuple[str | None, str | None]:
        url_error = await _validate_url(url)
        if url_error:
            return url_error, None
        return None, await _fetch_and_extract(url)

    try:
        return await asyncio.wait_for(operation(), timeout=timeout)
    except asyncio.TimeoutError:
        return None, None


def _chunk_markdown_content(
    content: str, chunk_size: int, max_chunks: int
) -> list[str]:
    """
    Chunk content with markdown awareness.

    Splits at markdown headers first (##, ###, etc.) to keep related content together,
    then further splits oversized sections by size.

    Args:
        content: The text content to chunk
        chunk_size: Maximum characters per chunk
        max_chunks: Maximum number of chunks to create

    Returns:
        List of content chunks
    """
    # First, split at markdown headers
    header_pattern = re.compile(r"\n(?=#{1,6}\s)")
    sections = header_pattern.split(content)

    chunks = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        if len(section) <= chunk_size:
            chunks.append(section)
        else:
            # Section is too large - split by paragraphs first
            paragraphs = re.split(r"\n\n+", section)
            current_chunk = []
            current_size = 0

            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue

                para_len = len(para) + 2

                if current_size + para_len > chunk_size and current_chunk:
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk = []
                    current_size = 0

                if para_len > chunk_size:
                    words = para.split()
                    word_chunk = []
                    word_size = 0

                    for word in words:
                        word_len = len(word) + 1
                        if word_size + word_len > chunk_size and word_chunk:
                            if current_chunk:
                                chunks.append("\n\n".join(current_chunk))
                                current_chunk = []
                                current_size = 0
                            chunks.append(" ".join(word_chunk))
                            word_chunk = [word]
                            word_size = word_len
                        else:
                            word_chunk.append(word)
                            word_size += word_len

                    if word_chunk:
                        current_chunk.append(" ".join(word_chunk))
                        current_size += word_size
                else:
                    current_chunk.append(para)
                    current_size += para_len

            if current_chunk:
                chunks.append("\n\n".join(current_chunk))

        if len(chunks) >= max_chunks:
            logger.warning(f"Reached max chunks ({max_chunks}), stopping")
            break

    return chunks[:max_chunks]


# ============================================================================
# Tool: COMPRESS_CONTEXT - Intelligent context compression
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
@legacy_entrypoint("compress_context")
async def compress_context(
    context: str,
    rate: float | None = None,
    content_type: str | None = None,
    preserve_code: bool = True,
) -> str:
    """
    Compress context using LLMLingua-2 for token reduction.

    Achieves 3x-6x compression while preserving meaning. Useful for:
    - Reducing large context before sending to LLM
    - Optimizing token usage in long conversations
    - Compressing retrieved memories for efficiency

    Args:
        context: Text to compress
        rate: Compression rate (0.2-0.5). Lower = more aggressive. Auto-detects if None.
        content_type: "code", "narrative", or "mixed". Auto-detects if None.
        preserve_code: Whether to preserve code syntax (function names, etc.)

    Returns:
        Compressed context as string.
    """
    try:
        from ..compression import AdaptiveCompressor, ContentType
    except ImportError:
        try:
            from daem0nmcp.compression import AdaptiveCompressor, ContentType
        except ImportError:
            return "[ERROR] Compression dependencies not installed. Run: pip install llmlingua tiktoken"

    try:
        adaptive = AdaptiveCompressor()

        # Parse content type if provided
        ct = None
        if content_type:
            ct = ContentType(content_type.lower())

        # Compress
        result = adaptive.compress(
            context,
            content_type=ct,
            rate_override=rate,
        )

        # Log stats
        if not result.get("skipped"):
            logger.info(
                f"Compressed context: {result['original_tokens']} -> "
                f"{result['compressed_tokens']} tokens ({result['ratio']:.1f}x)"
            )

        return result["compressed_prompt"]

    except Exception as e:
        logger.error(f"Compression failed: {e}")
        return f"[ERROR] Compression failed: {e}"


# ============================================================================
# Tool 45: EXECUTE_PYTHON - Sandboxed code execution
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
@legacy_entrypoint("execute_python")
async def execute_python(
    code: str,
    project_path: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """
    Execute Python code in an isolated sandbox.

    The code runs in a Firecracker microVM with:
    - No access to host filesystem
    - No network access
    - Hard timeout enforcement
    - Resource limits

    Args:
        code: Python code to execute
        project_path: Project root (required for capability check)
        timeout_seconds: Override default timeout (max 60s)

    Returns:
        Dict with:
        - success: bool - Whether execution succeeded
        - output: str - Captured stdout/print output
        - error: str|None - Error message if failed
        - execution_time_ms: int - Execution time in milliseconds
        - logs: list - Execution logs
    """
    # Require project_path
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    effective_path = project_path or _default_project_path

    # Check capability
    violation = check_capability(
        effective_path,
        CapabilityScope.EXECUTE_CODE,
        _capability_manager,
    )
    if violation:
        return violation

    # Check sandbox availability
    if not _sandbox_executor.available:
        return {
            "status": "error",
            "error": "SANDBOX_UNAVAILABLE",
            "message": (
                "Sandboxed execution is not available. "
                "Ensure E2B_API_KEY is set and e2b-code-interpreter is installed."
            ),
        }

    # Validate timeout
    actual_timeout = min(timeout_seconds or 30, 60)  # Cap at 60s

    # Log execution for anomaly detection
    logger.info(
        f"execute_python: project={effective_path}, "
        f"code_len={len(code)}, timeout={actual_timeout}s"
    )

    # Create executor with requested timeout
    executor = SandboxExecutor(timeout_seconds=actual_timeout)
    result = await executor.execute(code)

    # Log result for anomaly detection
    logger.info(
        f"execute_python result: success={result.success}, "
        f"time={result.execution_time_ms}ms, output_len={len(result.output)}"
    )

    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "execution_time_ms": result.execution_time_ms,
        "logs": result.logs,
    }


# ============================================================================
# Tool 14: INGEST_DOC - Import external documentation
# ============================================================================
@mcp.tool(version=__version__)
@with_request_id
@legacy_entrypoint("ingest_doc")
async def ingest_doc(
    url: str, topic: str, chunk_size: int = 2000, project_path: str | None = None
) -> dict[str, Any]:
    """
    Fetch external docs from URL and store as learnings. Content is chunked.

    Args:
        url: URL to fetch
        topic: Tag for organizing
        chunk_size: Max chars per chunk
        project_path: Project root
    """
    # Require project_path for multi-project support
    if not project_path and not _default_project_path:
        return _missing_project_path_error()

    # Validate input parameters
    if chunk_size <= 0:
        return {"error": "chunk_size must be positive", "url": url}

    if chunk_size > MAX_CONTENT_SIZE:
        return {"error": f"chunk_size cannot exceed {MAX_CONTENT_SIZE}", "url": url}

    if not topic or not topic.strip():
        return {"error": "topic cannot be empty", "url": url}

    ctx = await get_project_context(project_path)

    # Admission DNS and the fetch share one total wall-clock deadline.
    url_error, content = await _validate_and_fetch_with_deadline(url)
    if url_error:
        return {"error": url_error, "url": url}

    if content is None:
        return {
            "error": f"Failed to fetch URL. Ensure httpx and beautifulsoup4 are installed, "
            f"content is under {MAX_CONTENT_SIZE} bytes, and URL is accessible.",
            "url": url,
        }

    if not content.strip():
        return {"error": "No text content found at URL", "url": url}

    # Chunk the content with markdown-aware splitting
    chunks = _chunk_markdown_content(content, chunk_size, MAX_CHUNKS)

    if not chunks:
        return {"error": "Failed to chunk content", "url": url}

    # Store each chunk as a learning
    memories_created = []
    for i, chunk in enumerate(chunks):
        memory = await ctx.memory_manager.remember(
            category="learning",
            content=chunk[:500] + "..." if len(chunk) > 500 else chunk,
            rationale=f"Ingested from {url} (chunk {i + 1}/{len(chunks)})",
            tags=["docs", "ingested", topic],
            context={"source_url": url, "chunk_index": i, "total_chunks": len(chunks)},
            project_path=ctx.project_path,
        )
        memories_created.append(memory)

    return {
        "status": "success",
        "url": url,
        "topic": topic,
        "chunks_created": len(chunks),
        "total_chars": len(content),
        "truncated": len(chunks) >= MAX_CHUNKS,
        "message": f"Ingested {len(chunks)} chunks from {url}. Use recall('{topic}') to retrieve.",
        "memory_ids": [m.get("id") for m in memories_created if "id" in m],
    }
