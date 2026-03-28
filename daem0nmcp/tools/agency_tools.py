"""Agency tools: execute_python, compress_context, ingest_doc."""

import logging
import re
import sys
from typing import Any

try:
    from .. import __version__
    from ..agency import (
        CapabilityManager,
        CapabilityScope,
        SandboxExecutor,
        check_capability,
    )
    from ..config import settings
    from ..context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
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
    from daem0nmcp.config import settings
    from daem0nmcp.context_manager import (
        _default_project_path,
        _missing_project_path_error,
        get_project_context,
    )
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


def _resolve_public_ips(hostname: str) -> set[str]:
    """Resolve a hostname and ensure all IPs are public/global."""
    import ipaddress
    import socket

    try:
        addr_infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise ValueError("Host could not be resolved") from err

    if not addr_infos:
        raise ValueError("Host could not be resolved")

    ips: set[str] = set()
    for _, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise ValueError(f"Invalid IP address for host: {ip_str}") from exc
        if not ip_obj.is_global:
            raise ValueError(f"Non-public IP addresses are not allowed: {ip_obj}")
        ips.add(str(ip_obj))

    return ips


def _validate_url(url: str) -> tuple[str | None, set[str] | None]:
    """
    Validate URL for ingestion.
    Returns (error_message, resolved_public_ips).

    Security checks:
    - Scheme validation (no file://, etc.)
    - SSRF protection: Blocks localhost and private IPs
    - Cloud metadata endpoint protection
    """
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format", None

    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return (
            f"Invalid URL scheme '{parsed.scheme}'. Allowed: {ALLOWED_URL_SCHEMES}",
            None,
        )

    if not parsed.netloc:
        return "URL must have a host", None

    # Extract hostname from netloc (remove port)
    hostname = parsed.hostname
    if not hostname:
        return "URL must have a valid hostname", None

    # Block localhost
    if hostname.lower() in ["localhost", "localhost.localdomain", "127.0.0.1", "::1"]:
        return "Localhost URLs are not allowed", None

    # If hostname is an IP literal, validate directly
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if not ip_obj.is_global:
            return f"Non-public IP addresses are not allowed: {ip_obj}", None
        return None, {str(ip_obj)}
    except ValueError:
        pass

    try:
        allowed_ips = _resolve_public_ips(hostname)
    except ValueError as exc:
        return str(exc), None

    return None, allowed_ips


async def _fetch_and_extract(
    url: str, allowed_ips: set[str] | None = None
) -> str | None:
    """Fetch URL and extract text content with size limits."""
    try:
        import httpx
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    response = None
    try:
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
        async with (
            httpx.AsyncClient(
                timeout=float(INGEST_TIMEOUT),
                follow_redirects=False,
                trust_env=False,
                limits=limits,
                headers={"Accept-Encoding": "identity"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()

            # Check content length header first
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_CONTENT_SIZE:
                        logger.warning(f"Content too large: {content_length} bytes")
                        return None
                except ValueError:
                    pass

            size = 0
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_CONTENT_SIZE:
                    logger.warning(f"Content too large: {size} bytes")
                    return None
                chunks.append(chunk)

            stream = response.extensions.get("network_stream")
            if allowed_ips and stream and hasattr(stream, "get_extra_info"):
                peer = stream.get_extra_info("peername")
                peer_ip = None
                if isinstance(peer, (tuple, list)) and peer:
                    peer_ip = peer[0]
                elif peer:
                    peer_ip = str(peer)
                if peer_ip:
                    try:
                        import ipaddress

                        peer_ip = str(ipaddress.ip_address(peer_ip))
                    except ValueError:
                        peer_ip = None
                if peer_ip and peer_ip not in allowed_ips:
                    logger.warning(f"Resolved IP mismatch for {url}: {peer_ip}")
                    return None

        encoding = response.encoding if response else "utf-8"
        text = b"".join(chunks).decode(encoding or "utf-8", errors="replace")

        soup = BeautifulSoup(text, "html.parser")

        # Remove script and style elements
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        # Get text
        text = soup.get_text(separator="\n", strip=True)

        # Clean up whitespace
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


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

    # Validate URL
    url_error, allowed_ips = _validate_url(url)
    if url_error:
        return {"error": url_error, "url": url}

    ctx = await get_project_context(project_path)

    # Use module-level lookup so tests can patch via daem0nmcp.server._fetch_and_extract
    _mod = sys.modules.get("daem0nmcp.server", sys.modules[__name__])
    _fetch_fn = getattr(_mod, "_fetch_and_extract", _fetch_and_extract)
    content = await _fetch_fn(url, allowed_ips=allowed_ips)

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
