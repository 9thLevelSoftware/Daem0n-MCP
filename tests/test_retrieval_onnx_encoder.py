from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path


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
                    graph, opset_imports=[helper.make_operatorsetid("", 18)]
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
                    token_graph, opset_imports=[helper.make_operatorsetid("", 18)]
                ),
                root / "onnx" / "model_quantized.onnx",
            )
            self.assertIsNone(load_pooled_onnx_model(str(root), 2))


if __name__ == "__main__":
    unittest.main()
