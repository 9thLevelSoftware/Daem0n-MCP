"""Public ONNX Runtime adapter for models exporting pooled sentence vectors.

Some Sentence Transformers exports name their token output ``token_embeddings``
rather than Optimum's ``last_hidden_state``. Their ``sentence_embedding`` output
already includes the model's pooling, so it must not be pooled a second time.
All optional imports stay behind model initialization.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


class OnnxSentenceEmbeddingModel:
    def __init__(
        self,
        session: Any,
        tokenizer: Any,
        dimension: int,
        *,
        artifact_fingerprint: str | None = None,
    ) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._dimension = dimension
        self._inputs = tuple(item.name for item in session.get_inputs())
        self.artifact_fingerprint = artifact_fingerprint

    def encode(self, text: str, *, convert_to_numpy: bool = True) -> list[float]:
        return self.encode_many([text], convert_to_numpy=convert_to_numpy)[0]

    def encode_many(
        self, texts: list[str], *, convert_to_numpy: bool = True
    ) -> list[list[float]]:
        del convert_to_numpy
        if self._session is None or self._tokenizer is None:
            raise RuntimeError("DENSE_ENCODER_CLOSED")
        if not texts or any(not isinstance(text, str) for text in texts):
            raise RuntimeError("DENSE_ENCODER_INVALID")
        tokens = self._tokenizer(
            texts,
            return_tensors="np",
            truncation=True,
            padding=True,
            max_length=min(int(self._tokenizer.model_max_length), 8192),
        )
        feeds = {name: tokens[name] for name in self._inputs}
        outputs = self._session.run(["sentence_embedding"], feeds)
        vector = outputs[0]
        if (
            len(vector.shape) != 2
            or vector.shape[0] != len(texts)
            or vector.shape[1] < self._dimension
        ):
            raise RuntimeError("DENSE_ENCODER_INVALID")
        encoded: list[list[float]] = []
        for index in range(len(texts)):
            values = [float(value) for value in vector[index, : self._dimension]]
            if not all(map(math.isfinite, values)):
                raise RuntimeError("DENSE_ENCODER_INVALID")
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isfinite(norm) or norm == 0:
                raise RuntimeError("DENSE_ENCODER_INVALID")
            encoded.append([value / norm for value in values])
        return encoded

    def close(self) -> None:
        # ONNX Runtime releases the native session when its final owning Python
        # reference is released; it exposes no public close method.
        self._session = None
        self._tokenizer = None


# Root-level files that decide how text becomes model input.  Those present
# in a snapshot take part in the pooled encoder's identity, together with the
# vocabulary files the loaded tokenizer names itself.
_TOKENIZER_IDENTITY_FILES = (
    "added_tokens.json",
    "config.json",
    "merges.txt",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "spiece.model",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
    "vocab.txt",
)


def fingerprint_model_directory(root: Path) -> str:
    """Fingerprint stable bytes under a resolved model snapshot directory."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
    entries = tuple(root.rglob("*"))
    if any(path.is_symlink() and path.is_dir() for path in entries):
        raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
    return _fingerprint_files(root, (path for path in entries if path.is_file()))


def tokenizer_vocabulary_files(tokenizer: object) -> tuple[str, ...]:
    """Return the vocabulary file names a loaded tokenizer reads.

    The tokenizer library publishes them per tokenizer class, so a format
    with its own file name (``spm.model`` of a sentencepiece checkpoint, for
    one) is covered without being listed here by hand.
    """

    names = getattr(tokenizer, "vocab_files_names", None)
    if not isinstance(names, Mapping):
        return ()
    return tuple(sorted({name for name in names.values() if isinstance(name, str)}))


def fingerprint_encoder_artifacts(
    root: Path,
    artifact: Path,
    external_data: Iterable[str] = (),
    *,
    tokenizer_files: Iterable[str],
) -> str:
    """Fingerprint only the files that determine a pooled encoder's vectors.

    These are the loaded graph, the external tensor files it names, and the
    tokenizer files present directly under *root*: the well-known ones and
    those in *tokenizer_files*.  Anything else in the
    snapshot (other exports, weights for other backends, documentation) is
    ignored, so an unrelated download into the same snapshot leaves the
    identity alone.  A snapshot holding nothing but these files keeps the
    fingerprint :func:`fingerprint_model_directory` gives it.
    """

    # Only lexical locations are compared: hub snapshots link every file to
    # a blob stored outside the snapshot directory.
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
    files = {
        path
        for path in (
            Path(os.path.abspath(root / name))
            for name in (*_TOKENIZER_IDENTITY_FILES, *tokenizer_files)
        )
        if path.parent == root and path.is_file()
    }
    for path in (artifact, *(artifact.parent / name for name in external_data)):
        located = Path(os.path.abspath(path))
        if root not in located.parents or not located.is_file():
            raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
        files.add(located)
    return _fingerprint_files(root, files)


def _fingerprint_files(root: Path, paths: Iterable[Path]) -> str:
    digest = hashlib.sha256(b"daem0nmcp-embedding-artifacts-v1\0")
    files = sorted(paths, key=lambda path: path.relative_to(root).as_posix())
    if not files:
        raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            with path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                file_digest = hashlib.sha256()
                while chunk := handle.read(1024 * 1024):
                    file_digest.update(chunk)
                after = os.fstat(handle.fileno())
        except OSError as exc:
            raise RuntimeError("DENSE_ENCODER_UNAVAILABLE") from exc
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(before.st_size.to_bytes(8, "big"))
        digest.update(file_digest.digest())
    return digest.hexdigest()


def load_pooled_onnx_model(
    model_id: str, dimension: int
) -> OnnxSentenceEmbeddingModel | None:
    """Load a pooled export, or leave token-only exports to Sentence Transformers."""
    import onnx
    import onnxruntime as ort  # type: ignore[import-untyped]
    from huggingface_hub import hf_hub_download
    from transformers import AutoTokenizer

    model_root = Path(model_id)
    revision = None
    if model_root.is_dir():
        artifact = model_root / "onnx" / "model_quantized.onnx"
        if not artifact.is_file():
            return None
    else:
        from huggingface_hub.errors import EntryNotFoundError

        try:
            artifact = Path(hf_hub_download(model_id, "onnx/model_quantized.onnx"))
        except EntryNotFoundError:
            return None
        # Freeze the tokenizer to the same downloaded snapshot as the graph.
        revision = artifact.parent.parent.name
    model_root = artifact.parent.parent
    graph = onnx.load(artifact, load_external_data=False)
    pooled = any(output.name == "sentence_embedding" for output in graph.graph.output)
    external_data = sorted(
        {
            entry.value
            for initializer in graph.graph.initializer
            if initializer.data_location == onnx.TensorProto.EXTERNAL
            for entry in initializer.external_data
            if entry.key == "location"
        }
    )
    del graph
    if not pooled:
        return None
    # A first load from the hub downloads the tokenizer into the snapshot
    # here, so the identity is taken once every file it covers is in place.
    tokenizer = AutoTokenizer.from_pretrained(
        model_id, revision=revision, trust_remote_code=False
    )
    tokenizer_files = tokenizer_vocabulary_files(tokenizer)
    fingerprint_before = fingerprint_encoder_artifacts(
        model_root, artifact, external_data, tokenizer_files=tokenizer_files
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(artifact), sess_options=options, providers=["CPUExecutionProvider"]
    )
    fingerprint_after = fingerprint_encoder_artifacts(
        model_root, artifact, external_data, tokenizer_files=tokenizer_files
    )
    if fingerprint_after != fingerprint_before:
        raise RuntimeError("DENSE_ENCODER_UNAVAILABLE")
    return OnnxSentenceEmbeddingModel(
        session,
        tokenizer,
        dimension,
        artifact_fingerprint=fingerprint_after,
    )


__all__ = [
    "OnnxSentenceEmbeddingModel",
    "fingerprint_encoder_artifacts",
    "fingerprint_model_directory",
    "load_pooled_onnx_model",
    "tokenizer_vocabulary_files",
]
