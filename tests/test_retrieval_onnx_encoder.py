from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


class _Tokenizer:
    model_max_length = 16

    def __call__(self, _text, **_kwargs):
        return {}


class _Session:
    def __init__(self, output):
        self.output = output

    def get_inputs(self):
        return []

    def run(self, names, feeds):
        assert names == ["sentence_embedding"]
        assert feeds == {}
        return [self.output]


class _Matrix:
    def __init__(self, values):
        self.values = values
        self.shape = (len(values), len(values[0]) if values else 0)

    def __getitem__(self, index):
        row, columns = index
        return self.values[row][columns]


class _MalformedVector:
    shape = (2,)


class OnnxEncoderTests(unittest.TestCase):
    def test_encode_many_preserves_order_and_normalizes_each_row(self):
        from daem0nmcp.retrieval.onnx_encoder import OnnxSentenceEmbeddingModel

        encoder = OnnxSentenceEmbeddingModel(
            _Session(_Matrix([[3.0, 4.0], [0.0, 5.0]])), _Tokenizer(), 2
        )

        self.assertEqual([[0.6, 0.8], [0.0, 1.0]], encoder.encode_many(["a", "b"]))

    def test_artifact_fingerprint_changes_with_model_bytes(self):
        from daem0nmcp.retrieval.onnx_encoder import fingerprint_model_directory

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "onnx").mkdir()
            artifact = root / "onnx" / "model_quantized.onnx"
            artifact.write_bytes(b"first")
            (root / "tokenizer.json").write_bytes(b"tokenizer")
            first = fingerprint_model_directory(root)
            artifact.write_bytes(b"second")
            second = fingerprint_model_directory(root)

        self.assertNotEqual(first, second)

    def test_truncates_then_normalizes_and_rejects_bad_pooled_output(self):
        from daem0nmcp.retrieval.onnx_encoder import OnnxSentenceEmbeddingModel

        encoder = OnnxSentenceEmbeddingModel(
            _Session(_Matrix([[3.0, 4.0, 99.0]])), _Tokenizer(), 2
        )
        self.assertEqual([0.6, 0.8], encoder.encode("x"))
        for output in (
            _MalformedVector(),
            _Matrix([[0.0, 0.0]]),
            _Matrix([[float("nan"), 1.0]]),
        ):
            with self.assertRaisesRegex(RuntimeError, "DENSE_ENCODER_INVALID"):
                OnnxSentenceEmbeddingModel(_Session(output), _Tokenizer(), 2).encode(
                    "x"
                )

    def test_close_fails_closed(self):
        from daem0nmcp.retrieval.onnx_encoder import OnnxSentenceEmbeddingModel

        encoder = OnnxSentenceEmbeddingModel(
            _Session(_Matrix([[1.0, 0.0]])), _Tokenizer(), 2
        )
        encoder.close()
        with self.assertRaisesRegex(RuntimeError, "DENSE_ENCODER_CLOSED"):
            encoder.encode("x")

    def test_local_pooled_graph_and_token_only_fallback_never_use_network(self):
        try:
            import onnx
            from onnx import TensorProto, helper
        except ImportError:
            self.skipTest("optional ONNX profile is unavailable")
        from daem0nmcp.retrieval.onnx_encoder import load_pooled_onnx_model

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "onnx").mkdir()
            (root / "vocab.txt").write_text(
                "[PAD]\n[UNK]\n[CLS]\n[SEP]\nhello\n", encoding="utf-8"
            )
            (root / "config.json").write_text(
                '{"model_type":"bert","vocab_size":5}', encoding="utf-8"
            )
            (root / "tokenizer_config.json").write_text(
                '{"model_max_length":8}', encoding="utf-8"
            )
            constant = helper.make_node(
                "Constant",
                [],
                ["sentence_embedding"],
                value=helper.make_tensor(
                    "value", TensorProto.FLOAT, [1, 3], [3.0, 4.0, 9.0]
                ),
            )
            graph = helper.make_graph(
                [constant],
                "tiny",
                [],
                [
                    helper.make_tensor_value_info(
                        "sentence_embedding", TensorProto.FLOAT, [1, 3]
                    )
                ],
            )
            onnx.save(
                helper.make_model(
                    graph,
                    opset_imports=[helper.make_operatorsetid("", 18)],
                    ir_version=8,
                ),
                root / "onnx" / "model_quantized.onnx",
            )
            model = load_pooled_onnx_model(str(root), 2)
            assert model is not None
            self.assertRegex(str(model.artifact_fingerprint), r"^[0-9a-f]{64}$")
            vector = model.encode("hello")
            self.assertEqual(2, len(vector))
            self.assertTrue(
                math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0)
            )
            model.close()

            token_graph = helper.make_graph(
                [],
                "token",
                [],
                [
                    helper.make_tensor_value_info(
                        "token_embeddings", TensorProto.FLOAT, [1, 1, 3]
                    )
                ],
            )
            onnx.save(
                helper.make_model(
                    token_graph,
                    opset_imports=[helper.make_operatorsetid("", 18)],
                    ir_version=8,
                ),
                root / "onnx" / "model_quantized.onnx",
            )
            self.assertIsNone(load_pooled_onnx_model(str(root), 2))


class _FakeGraphOutput:
    name = "sentence_embedding"


class _FakeGraph:
    def __init__(self, external_locations=()):
        self.output = [_FakeGraphOutput()]
        self.initializer = [
            SimpleNamespace(
                data_location=1,
                external_data=[
                    SimpleNamespace(key="location", value=location),
                    SimpleNamespace(key="offset", value="0"),
                ],
            )
            for location in external_locations
        ] + [SimpleNamespace(data_location=0, external_data=[])]


class _EntryNotFoundError(Exception):
    pass


def _fake_loader_modules(snapshot, *, tokenizer_files, external_locations=()):
    """Stand in for the optional ONNX stack with a hub snapshot on disk.

    The weights are already in the snapshot; the tokenizer files arrive only
    when the tokenizer is loaded, as on a first, cold load from the hub.
    """

    artifact = snapshot / "onnx" / "model_quantized.onnx"
    loaded = []

    def hf_hub_download(model_id, filename):
        assert filename == "onnx/model_quantized.onnx"
        return str(artifact)

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(model_id, *, revision, trust_remote_code):
            assert revision == snapshot.name
            assert trust_remote_code is False
            for name, payload in tokenizer_files.items():
                (snapshot / name).write_bytes(payload)
            return _Tokenizer()

    def load(path, *, load_external_data):
        assert load_external_data is False
        loaded.append(Path(path))
        return SimpleNamespace(graph=_FakeGraph(external_locations))

    onnx = ModuleType("onnx")
    onnx.load = load
    onnx.TensorProto = SimpleNamespace(EXTERNAL=1)
    runtime = ModuleType("onnxruntime")
    runtime.SessionOptions = lambda: SimpleNamespace()
    runtime.InferenceSession = lambda path, **_kwargs: _Session(_Matrix([[3.0, 4.0]]))
    hub = ModuleType("huggingface_hub")
    hub.hf_hub_download = hf_hub_download
    hub_errors = ModuleType("huggingface_hub.errors")
    hub_errors.EntryNotFoundError = _EntryNotFoundError
    hub.errors = hub_errors
    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = AutoTokenizer
    return {
        "onnx": onnx,
        "onnxruntime": runtime,
        "huggingface_hub": hub,
        "huggingface_hub.errors": hub_errors,
        "transformers": transformers,
    }


_TOKENIZER_FILES = {
    "tokenizer.json": b'{"version":"1.0"}',
    "tokenizer_config.json": b'{"model_max_length":8}',
    "special_tokens_map.json": b"{}",
}


class OnnxEncoderFingerprintTests(unittest.TestCase):
    def _snapshot(self, temporary, weights=b"weights"):
        snapshot = Path(temporary) / "snapshots" / ("0" * 40)
        (snapshot / "onnx").mkdir(parents=True)
        (snapshot / "onnx" / "model_quantized.onnx").write_bytes(weights)
        return snapshot

    def _load(self, snapshot, **kwargs):
        from daem0nmcp.retrieval.onnx_encoder import load_pooled_onnx_model

        kwargs.setdefault("tokenizer_files", _TOKENIZER_FILES)
        with patch.dict(sys.modules, _fake_loader_modules(snapshot, **kwargs)):
            model = load_pooled_onnx_model("example/pooled-encoder", 2)
        assert model is not None
        self.assertRegex(str(model.artifact_fingerprint), r"^[0-9a-f]{64}$")
        return model.artifact_fingerprint

    def test_cold_load_survives_tokenizer_files_landing_during_the_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot(temporary)

            cold = self._load(snapshot)
            warm = self._load(snapshot)

        self.assertEqual(cold, warm)

    def test_unrelated_later_downloads_keep_the_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot(temporary)
            first = self._load(snapshot)
            (snapshot / "README.md").write_bytes(b"model card")
            (snapshot / "model.safetensors").write_bytes(b"torch weights")
            (snapshot / "onnx" / "model.onnx").write_bytes(b"full precision")
            (snapshot / "1_Pooling").mkdir()
            (snapshot / "1_Pooling" / "config.json").write_bytes(b"{}")

            second = self._load(snapshot)

        self.assertEqual(first, second)

    def test_fingerprint_follows_the_weights_and_the_tokenizer(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot(temporary)
            baseline = self._load(snapshot)
            changed_tokenizer = self._load(
                snapshot,
                tokenizer_files={**_TOKENIZER_FILES, "tokenizer.json": b"{}"},
            )
            self.assertEqual(baseline, self._load(snapshot))
            (snapshot / "onnx" / "model_quantized.onnx").write_bytes(b"retrained")
            changed_weights = self._load(snapshot)

        self.assertEqual(3, len({baseline, changed_tokenizer, changed_weights}))

    def test_external_tensor_files_named_by_the_graph_are_part_of_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot(temporary)
            data = snapshot / "onnx" / "model_quantized.onnx_data"
            data.write_bytes(b"tensors")
            inline = self._load(snapshot)
            external = self._load(
                snapshot, external_locations=("model_quantized.onnx_data",)
            )
            data.write_bytes(b"other tensors")
            changed = self._load(
                snapshot, external_locations=("model_quantized.onnx_data",)
            )
            self.assertEqual(inline, self._load(snapshot))
            (Path(temporary) / "outside.bin").write_bytes(b"not in the snapshot")
            for location in ("missing.onnx_data", "../../../outside.bin"):
                with (
                    self.subTest(location=location),
                    self.assertRaisesRegex(RuntimeError, "DENSE_ENCODER_UNAVAILABLE"),
                ):
                    self._load(snapshot, external_locations=(location,))

        self.assertEqual(3, len({inline, external, changed}))

    def test_snapshot_of_only_encoder_files_keeps_its_directory_fingerprint(self):
        from daem0nmcp.retrieval.onnx_encoder import fingerprint_model_directory

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot(temporary)
            narrowed = self._load(
                snapshot,
                tokenizer_files={
                    **_TOKENIZER_FILES,
                    "config.json": b'{"model_type":"bert"}',
                    "vocab.txt": b"[PAD]\n[UNK]\n",
                },
            )
            whole_directory = fingerprint_model_directory(snapshot)

        # Dense manifests written before the fingerprint was narrowed stay
        # valid when the snapshot held nothing but the encoder's own files.
        self.assertEqual(whole_directory, narrowed)


if __name__ == "__main__":
    unittest.main()
