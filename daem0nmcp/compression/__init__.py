"""
Compression module for intelligent context compression.

Uses LLMLingua-2 for token classification-based compression,
achieving 3x-6x reduction while preserving critical information.
"""

from .adaptive import COMPRESSION_RATES, AdaptiveCompressor, ContentType
from .compressor import ContextCompressor
from .config import CompressionConfig
from .entity_preserver import CodeEntityPreserver
from .hierarchical import HierarchicalContextManager
from .jit import JITCompressionConfig, JITCompressor

__all__ = [
    "CompressionConfig",
    "ContextCompressor",
    "CodeEntityPreserver",
    "AdaptiveCompressor",
    "ContentType",
    "COMPRESSION_RATES",
    "HierarchicalContextManager",
    "JITCompressor",
    "JITCompressionConfig",
]
