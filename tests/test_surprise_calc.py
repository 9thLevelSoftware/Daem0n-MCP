# tests/test_surprise_calc.py
"""Tests for surprise score calculation."""

from daem0nmcp.surprise import SurpriseCalculator, calculate_surprise


class TestCalculateSurprise:
    """Test surprise calculation."""

    def test_novel_content_high_surprise(self):
        # Use vectors that point in different directions (not just different magnitudes)
        # Existing embeddings point in one direction
        existing_embeddings = [
            [1.0] * 128 + [-1.0] * 128,  # About topic A
            [1.0] * 128 + [-1.0] * 128,  # About topic A
        ]
        # New embedding points in a very different direction
        new_embedding = [-1.0] * 128 + [1.0] * 128  # Opposite direction

        surprise = calculate_surprise(new_embedding, existing_embeddings)
        assert surprise > 0.5  # Should be high surprise

    def test_similar_content_low_surprise(self):
        existing_embeddings = [
            [0.5] * 256,
            [0.5] * 256,
        ]
        new_embedding = [0.5] * 256  # Same

        surprise = calculate_surprise(new_embedding, existing_embeddings)
        assert surprise < 0.5  # Should be low surprise

    def test_empty_existing_returns_max_surprise(self):
        new_embedding = [0.5] * 256
        surprise = calculate_surprise(new_embedding, [])
        assert surprise == 1.0  # First memory is always surprising

    def test_surprise_bounded_0_to_1(self):
        existing = [[0.1] * 256, [0.9] * 256]
        new = [0.5] * 256

        surprise = calculate_surprise(new, existing)
        assert 0.0 <= surprise <= 1.0


class TestSurpriseCalculator:
    """Test SurpriseCalculator class."""

    def test_calculator_with_k_nearest(self):
        calc = SurpriseCalculator(k_nearest=2)

        existing = [
            [0.1] * 256,
            [0.2] * 256,
            [0.9] * 256,  # Outlier
        ]
        new = [0.15] * 256  # Similar to first two

        # Should only compare to 2 nearest, not the outlier
        surprise = calc.calculate(new, existing)
        assert surprise < 0.5
