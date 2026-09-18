# Real provider development evidence

This is development evidence from the uncommitted v7 completion worktree, not
release certification. The full performance, quality and supported-platform
gates remain open.

## Configured embedding backend

The configured `nomic-ai/modernbert-embed-base` quantized ONNX export ran with
ONNX Runtime on Windows/Python 3.12 and returned finite 256-dimensional vectors.
The artifact SHA-256 is
`11b1bd3398254d66a4539883965ea81eafe76e14a66d734421f63cb9211f890a`.
The adapter consumes its pooled `sentence_embedding` output and applies
Matryoshka truncation before L2 normalization. Its tokenizer is loaded from the
same Hugging Face snapshot as the graph. No remote Python code is trusted.

Offline regression tests include a generated local ONNX graph and tokenizer,
output validation, normalization order, fallback, and resource cleanup.
The all-profile focused run passed 19 cases.

## Authenticated local Qdrant

The owned local Qdrant service is described in [local-services.md](local-services.md).
An actual production MCP stdio client completed briefing, exact preflight,
canonical write, automatic dense projection activation and recall. The new
record had both lexical and dense evidence after 7.25 seconds on this tiny
fixture (`.tmp/probe_mcp_dense_fixed.log`). This is not a 100,000-record benchmark.

This run exposed an exact-float validation defect: Qdrant normalizes cosine
vectors and stores float32 components. Validation now compares cosine direction
within a bounded numerical tolerance while retaining exact payload, identity,
dimension and cardinality checks. The regression includes real in-process
Qdrant, malformed vectors and significant numerical corruption.
[Qdrant representation contract](https://qdrant.tech/documentation/manage-data/collections/).

The follow-up exposed two-slot limits in both dense search and canonical
hydration, plus a Windows native-module import/thread-creation deadlock on
first recall after restart. Both pools now admit the required four calls;
enabled model profiles initialize native library entry points before starting
projection or retrieval workers. Core-only startup keeps optional imports lazy.

The final development follow-up used ordinary production startup without the
diagnostic preload. Both transports returned dense evidence for all 20 warm
requests at concurrency four and recalled the canonical record after restart:

| Transport | Tiny-fixture warm p95 | Evidence |
|---|---:|---|
| stdio | 0.5024 seconds | `.tmp/probe_mcp_dense_production_stdio.log` |
| Streamable HTTP | 0.6068 seconds | `.tmp/probe_mcp_dense_production_streamable-http.log` |

Versions: Qdrant client 1.19.1, ONNX Runtime 1.30.0, Sentence Transformers 5.7.0,
FastMCP 3.4.7. These one-record runs establish production integration only.
Independent bounded lifecycle review passed (190 tests plus 50 subtests).
The full performance and remote-provider gates remain open.

Provider upload and validation now operate in 128-point batches. A follow-up
with the configured ONNX model and authenticated Qdrant activated 257 records
across three batches in 21.66 seconds (`.tmp/dense257-real.log`). Independent
batching tests passed 25 cases and 11 subtests, preserving exact global point
validation and cleanup after partial failure. The source changed during the
real run; this is bounded integration evidence, not a scale measurement.

## Portable vector round trip

The actual stdio MCP server exported two pages and imported one canonical event
and its vector through authenticated loopback Qdrant using ordinary production
startup with the local profile enabled. Evidence:
`.tmp/p5-production-qdrant.log`; retained databases:
`.tmp/p5-mcp-real-0c76cead7aeb4f008590385e36bd20c9`.
The projection was seeded with a synthetic three-dimensional vector, so this
check covers portability and transport, not configured-model quality.
Independent review found and verified repairs for the disabled-profile bypass,
attempt fencing, quota takeover, and failed-provider cleanup. The final bounded
recovery review passed all 25 portable tests; see
[P5 final review](p5-final-review.md). A later actual Qdrant/MCP rerun is recorded
in `.tmp/p5-mcp-qdrant-final-repair.log`. Final-release acceptance remains open.

## Public URL ingestion

The registered `document_ingest_url` tool fetched `https://example.com/` over
the real pinned HTTPS transport on both stdio and Streamable HTTP. Two process
tests verified canonical chunk events, source URL/content-hash provenance, and
idempotent replay without duplicate events. Evidence:
`.tmp/public-ingest-process-final.log` (2 passed, 25.76 seconds).
Run explicitly with `DAEM0NMCP_CERTIFY_PUBLIC_INGEST=1`; the default skip does
not certify external availability. The no-redirect contract remains in force.

## E2B

The interpreter is pinned to 2.10.0, matching the required lifecycle API. Real
SDK parsing with controlled streamed responses verifies byte limits before JSON
materialization, compressed/error response rejection, and bounded cleanup under
repeated cancellation. The independent bounded adapter review is accepted.

Live E2B staging was not run because credentials were unavailable. Isolation and
network restrictions against the staging provider therefore remain open.
