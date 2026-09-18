"""Leiden community detection with a bounded, killable native boundary."""

from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.025
_TERMINATE_GRACE_SECONDS = 1.0
_MAX_NATIVE_NODES = 100_000
_MAX_NATIVE_EDGES = 200_000
_MAX_NODE_ID_CHARS = 80
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_MAX_NATIVE_MEMORY_BYTES = 1024 * 1024 * 1024


class LeidenExecutionError(RuntimeError):
    """Stable failure raised by the isolated native graph phase."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LeidenResult:
    communities: dict[str, int]
    modularity: float


@dataclass(frozen=True, slots=True)
class LeidenConfig:
    """Configuration for Leiden algorithm."""

    resolution: float = 1.0
    seed: int = 42
    n_iterations: int = -1
    partition_type: str = "modularity"


class _WindowsMemoryJob:
    """Own a Windows Job Object containing only the spawned native child."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._handle: int | None = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
        limits.ProcessMemoryLimit = _MAX_NATIVE_MEMORY_BYTES
        process_handle = getattr(process, "_handle", None)
        if (
            not isinstance(process_handle, int)
            or not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            or not kernel32.AssignProcessToJobObject(handle, process_handle)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "native child memory job setup failed")
        self._handle = int(handle)

    def close(self) -> None:
        if self._handle is None:
            return
        import ctypes

        ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None


def _validate_config(config: LeidenConfig) -> None:
    if (
        isinstance(config.resolution, bool)
        or not isinstance(config.resolution, (int, float))
        or not math.isfinite(float(config.resolution))
        or not 0 < float(config.resolution) <= 100
        or isinstance(config.seed, bool)
        or not isinstance(config.seed, int)
        or isinstance(config.n_iterations, bool)
        or not isinstance(config.n_iterations, int)
        or config.n_iterations < -1
        or config.partition_type not in {"modularity", "cpm"}
    ):
        raise LeidenExecutionError("INVALID_ARGUMENT")


def _native_leiden(
    nodes: Sequence[str],
    edges: Sequence[tuple[str, str]],
    config: LeidenConfig,
) -> LeidenResult:
    """Build native graphs and run Leiden inside the current process."""

    try:
        import igraph as ig
        import leidenalg as la
        import networkx as nx
    except ImportError:
        raise LeidenExecutionError("CAPABILITY_DEGRADED") from None

    graph = nx.Graph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from(edges)
    if graph.number_of_nodes() == 0:
        return LeidenResult({}, 0.0)
    ig_graph = ig.Graph.from_networkx(graph)
    partition_class = (
        la.CPMVertexPartition
        if config.partition_type == "cpm"
        else la.RBConfigurationVertexPartition
    )
    partition = la.find_partition(
        ig_graph,
        partition_class,
        seed=config.seed,
        n_iterations=config.n_iterations,
        resolution_parameter=float(config.resolution),
    )
    node_list = list(graph.nodes())
    communities = {
        str(node_list[index]): int(partition.membership[index])
        for index in range(len(node_list))
    }
    grouped: dict[int, set[str]] = {}
    for node_id, community_id in communities.items():
        grouped.setdefault(community_id, set()).add(node_id)
    modularity = 0.0
    if graph.number_of_edges() and grouped:
        modularity = float(
            nx.algorithms.community.quality.modularity(graph, grouped.values())
        )
    if not math.isfinite(modularity) or not -1.0 <= modularity <= 1.0:
        raise LeidenExecutionError("PROJECTION_VALIDATION_FAILED")
    return LeidenResult(communities, modularity)


def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    """Stop only the exact child handle created for this invocation."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)


def _request_bytes(
    nodes: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
    config: LeidenConfig,
) -> bytes:
    value = json.dumps(
        {
            "config": asdict(config),
            "edges": edges,
            "memory_limit_bytes": _MAX_NATIVE_MEMORY_BYTES,
            "nodes": nodes,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(value) > _MAX_REQUEST_BYTES:
        raise LeidenExecutionError("TASK_REQUIRED")
    return value


def _decode_result(value: bytes, nodes: tuple[str, ...]) -> LeidenResult:
    if len(value) > _MAX_RESULT_BYTES:
        raise LeidenExecutionError("PROJECTION_BUILD_FAILED")
    try:
        message = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise LeidenExecutionError("PROJECTION_BUILD_FAILED") from None
    if isinstance(message, dict) and set(message) == {"error"}:
        code = message.get("error")
        raise LeidenExecutionError(
            code if isinstance(code, str) else "PROJECTION_BUILD_FAILED"
        )
    if not isinstance(message, dict) or set(message) != {"communities", "modularity"}:
        raise LeidenExecutionError("PROJECTION_BUILD_FAILED")
    mapping = message["communities"]
    modularity = message["modularity"]
    if (
        not isinstance(mapping, dict)
        or set(mapping) != set(nodes)
        or any(
            not isinstance(key, str)
            or isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            for key, item in mapping.items()
        )
        or isinstance(modularity, bool)
        or not isinstance(modularity, (int, float))
        or not math.isfinite(float(modularity))
        or not -1.0 <= float(modularity) <= 1.0
    ):
        raise LeidenExecutionError("PROJECTION_VALIDATION_FAILED")
    return LeidenResult(
        {str(key): int(item) for key, item in mapping.items()}, float(modularity)
    )


def run_leiden_bounded(
    nodes: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
    config: LeidenConfig,
    *,
    cancelled: threading.Event | None,
    deadline: float,
    worker_command: tuple[str, ...] | None = None,
) -> LeidenResult:
    """Run native graph construction behind a cancellable child process."""

    _validate_config(config)
    if (
        not isinstance(nodes, tuple)
        or not isinstance(edges, tuple)
        or len(nodes) > _MAX_NATIVE_NODES
        or len(edges) > _MAX_NATIVE_EDGES
        or any(
            not isinstance(node, str) or not 1 <= len(node) <= _MAX_NODE_ID_CHARS
            for node in nodes
        )
        or any(
            not isinstance(edge, tuple)
            or len(edge) != 2
            or not all(
                isinstance(node, str) and 1 <= len(node) <= _MAX_NODE_ID_CHARS
                for node in edge
            )
            for edge in edges
        )
        or isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        raise LeidenExecutionError("INVALID_ARGUMENT")
    node_set = set(nodes)
    if len(node_set) != len(nodes) or any(
        source == target or source not in node_set or target not in node_set
        for source, target in edges
    ):
        raise LeidenExecutionError("INVALID_ARGUMENT")
    if cancelled is not None and cancelled.is_set():
        raise LeidenExecutionError("CANCELLED")
    if time.monotonic() >= deadline:
        raise LeidenExecutionError("DEADLINE_EXCEEDED")
    command = worker_command or (
        sys.executable,
        str(Path(__file__).resolve()),
        "--bounded-worker",
    )
    request = _request_bytes(nodes, edges, config)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    try:
        memory_job = _WindowsMemoryJob(process)
    except OSError:
        _stop_owned_process(process)
        raise LeidenExecutionError("CAPABILITY_DEGRADED") from None
    first = True
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise LeidenExecutionError("CANCELLED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeidenExecutionError("DEADLINE_EXCEEDED")
            try:
                output, _stderr = process.communicate(
                    input=request if first else None,
                    timeout=min(_POLL_SECONDS, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                first = False
        if process.returncode != 0:
            raise LeidenExecutionError("PROJECTION_BUILD_FAILED")
        return _decode_result(output, nodes)
    finally:
        _stop_owned_process(process)
        memory_job.close()


def _apply_self_memory_limit(limit_bytes: int) -> None:
    if sys.platform == "win32":
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, OSError, ValueError):
        raise LeidenExecutionError("CAPABILITY_DEGRADED") from None


def _bounded_worker() -> int:
    """Read one bounded request and emit one path-free result."""

    try:
        raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(raw) > _MAX_REQUEST_BYTES:
            raise LeidenExecutionError("TASK_REQUIRED")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {
            "config",
            "edges",
            "memory_limit_bytes",
            "nodes",
        }:
            raise ValueError
        memory_limit = request["memory_limit_bytes"]
        if memory_limit != _MAX_NATIVE_MEMORY_BYTES:
            raise ValueError
        _apply_self_memory_limit(memory_limit)
        config = LeidenConfig(**request["config"])
        _validate_config(config)
        nodes = tuple(request["nodes"])
        edges = tuple(tuple(edge) for edge in request["edges"])
        if (
            len(nodes) > _MAX_NATIVE_NODES
            or len(edges) > _MAX_NATIVE_EDGES
            or any(
                not isinstance(node, str) or not 1 <= len(node) <= _MAX_NODE_ID_CHARS
                for node in nodes
            )
            or any(
                len(edge) != 2
                or not all(
                    isinstance(node, str) and 1 <= len(node) <= _MAX_NODE_ID_CHARS
                    for node in edge
                )
                for edge in edges
            )
        ):
            raise ValueError
        node_set = set(nodes)
        if any(
            edge[0] == edge[1] or edge[0] not in node_set or edge[1] not in node_set
            for edge in edges
        ):
            raise ValueError
        result = _native_leiden(nodes, edges, config)
        response: object = {
            "communities": result.communities,
            "modularity": result.modularity,
        }
    except LeidenExecutionError as error:
        response = {"error": error.code}
    except BaseException:
        response = {"error": "PROJECTION_BUILD_FAILED"}
    sys.stdout.buffer.write(
        json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sys.stdout.buffer.flush()
    return 0


def run_leiden_on_networkx(
    nx_graph: Any,
    config: LeidenConfig | None = None,
) -> dict[str, int]:
    """Run Leiden in-process for legacy callers that already own a graph."""

    selected = config or LeidenConfig()
    _validate_config(selected)
    try:
        nodes = tuple(str(node) for node in nx_graph.nodes())
        edges = tuple((str(source), str(target)) for source, target in nx_graph.edges())
    except (AttributeError, TypeError, ValueError):
        raise LeidenExecutionError("INVALID_ARGUMENT") from None
    result = _native_leiden(nodes, edges, selected)
    logger.info(
        "Leiden found %d communities in graph with %d nodes",
        len(set(result.communities.values())),
        len(result.communities),
    )
    return result.communities


def get_community_stats(community_map: dict[str, int]) -> dict[str, Any]:
    """Get statistics about detected communities."""

    if not community_map:
        return {"num_communities": 0, "sizes": [], "avg_size": 0}
    community_sizes = Counter(community_map.values())
    return {
        "num_communities": len(community_sizes),
        "sizes": sorted(community_sizes.values(), reverse=True),
        "avg_size": len(community_map) / len(community_sizes),
        "largest_community": max(community_sizes.values()),
        "smallest_community": min(community_sizes.values()),
    }


def get_nodes_in_community(
    community_map: dict[str, int],
    community_id: int,
) -> list[str]:
    """Get all node IDs belonging to a specific community."""

    return [
        node for node, community in community_map.items() if community == community_id
    ]


if __name__ == "__main__" and sys.argv[1:] == ["--bounded-worker"]:
    raise SystemExit(_bounded_worker())


__all__ = [
    "LeidenConfig",
    "LeidenExecutionError",
    "LeidenResult",
    "get_community_stats",
    "get_nodes_in_community",
    "run_leiden_bounded",
    "run_leiden_on_networkx",
]
