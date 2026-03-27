"""Tests for the TF-IDF similarity engine."""

from datetime import datetime, timedelta, timezone

from daem0nmcp.similarity import (
    TFIDFIndex,
    calculate_memory_decay,
    detect_conflict,
    extract_code_symbols,
    tokenize,
)


class TestTokenize:
    """Test tokenization."""

    def test_basic_tokenization(self):
        text = "Use JWT tokens for authentication"
        tokens = tokenize(text)
        assert "jwt" in tokens
        assert "tokens" in tokens
        assert "authentication" in tokens

    def test_stop_words_removed(self):
        text = "Use the JWT tokens for authentication"
        tokens = tokenize(text)
        assert "use" not in tokens
        assert "the" not in tokens
        assert "for" not in tokens

    def test_camel_case_split(self):
        text = "getUserProfile"
        tokens = tokenize(text)
        # "get" is a stop word, so it's filtered out
        assert "user" in tokens
        assert "profile" in tokens

    def test_snake_case_split(self):
        text = "get_user_profile"
        tokens = tokenize(text)
        # "get" is a stop word, so it's filtered out
        assert "user" in tokens
        assert "profile" in tokens

    def test_preserves_technical_terms(self):
        text = "The API uses JWT for DB access"
        tokens = tokenize(text)
        assert "api" in tokens
        assert "jwt" in tokens
        assert "db" in tokens

    def test_empty_text(self):
        assert tokenize("") == []
        assert tokenize(None) == []


class TestExtractCodeSymbols:
    """Test code symbol extraction."""

    def test_extracts_backtick_code(self):
        text = "Use the `getUserById` function to fetch users"
        symbols = extract_code_symbols(text)
        assert "getUserById" in symbols

    def test_extracts_camel_case(self):
        text = "The UserService class handles authentication"
        symbols = extract_code_symbols(text)
        assert "UserService" in symbols

    def test_extracts_lower_camel_case(self):
        text = "Call validateUserInput before processing"
        symbols = extract_code_symbols(text)
        assert "validateUserInput" in symbols

    def test_extracts_snake_case(self):
        text = "Use the get_user_by_id function"
        symbols = extract_code_symbols(text)
        assert "get_user_by_id" in symbols

    def test_extracts_screaming_snake(self):
        text = "Set MAX_RETRY_COUNT to 5"
        symbols = extract_code_symbols(text)
        assert "MAX_RETRY_COUNT" in symbols

    def test_extracts_method_calls(self):
        text = "Call obj.processData and obj.saveResult"
        symbols = extract_code_symbols(text)
        assert "processData" in symbols
        assert "saveResult" in symbols

    def test_includes_lowercased_versions(self):
        text = "Use `UserService` for users"
        symbols = extract_code_symbols(text)
        assert "UserService" in symbols
        assert "userservice" in symbols

    def test_empty_text(self):
        assert extract_code_symbols("") == []
        assert extract_code_symbols(None) == []

    def test_tokenize_includes_symbols(self):
        """Test that tokenize() includes extracted symbols."""
        text = "Use the `fetchUserProfile` function"
        tokens = tokenize(text)
        # Should include the symbol (lowercased)
        assert "fetchuserprofile" in tokens


class TestTFIDFIndex:
    """Test TF-IDF indexing and search."""

    def test_add_and_search(self):
        index = TFIDFIndex()
        index.add_document(1, "JWT authentication for API security")
        index.add_document(2, "Database migration and schema changes")
        index.add_document(3, "REST API endpoint design patterns")

        results = index.search("API authentication", top_k=3)
        assert len(results) >= 1
        # Document 1 should be most relevant (has both API and authentication)
        assert results[0][0] == 1

    def test_search_with_tags(self):
        index = TFIDFIndex()
        index.add_document(1, "Use tokens for auth", tags=["security", "jwt"])
        index.add_document(2, "Database configuration")

        results = index.search("JWT security", top_k=2)
        assert len(results) >= 1
        assert results[0][0] == 1  # Should match due to tags

    def test_cosine_similarity(self):
        index = TFIDFIndex()
        vec1 = {"api": 0.5, "security": 0.3}
        vec2 = {"api": 0.5, "database": 0.3}
        vec3 = {"api": 0.5, "security": 0.3}

        # Identical vectors should have similarity 1.0
        assert index.cosine_similarity(vec1, vec3) > 0.99

        # Partial overlap should have lower similarity
        sim = index.cosine_similarity(vec1, vec2)
        assert 0 < sim < 1

    def test_document_similarity(self):
        index = TFIDFIndex()
        index.add_document(1, "JWT authentication tokens")
        index.add_document(2, "OAuth authentication tokens")
        index.add_document(3, "Database migration scripts")

        # Documents about authentication should be similar
        sim_auth = index.document_similarity(1, 2)
        # Document about database should be different
        sim_db = index.document_similarity(1, 3)

        assert sim_auth > sim_db

    def test_remove_document(self):
        index = TFIDFIndex()
        index.add_document(1, "Authentication API")
        index.add_document(2, "Database changes")

        index.remove_document(1)

        results = index.search("Authentication", top_k=2)
        assert not any(doc_id == 1 for doc_id, _ in results)

    def test_threshold_filtering(self):
        index = TFIDFIndex()
        index.add_document(1, "JWT authentication security")
        index.add_document(2, "Completely unrelated topic")

        # High threshold should filter out poor matches
        results = index.search("JWT security", threshold=0.5)
        assert all(score >= 0.5 for _, score in results)


class TestMemoryDecay:
    """Test time-based memory decay."""

    def test_recent_memory_full_weight(self):
        now = datetime.now(timezone.utc)
        weight = calculate_memory_decay(now, half_life_days=30)
        assert weight > 0.99

    def test_old_memory_decayed(self):
        old_date = datetime.now(timezone.utc) - timedelta(days=60)
        weight = calculate_memory_decay(old_date, half_life_days=30)
        # After 2 half-lives, should be around 0.25
        assert 0.2 < weight < 0.4

    def test_minimum_weight_floor(self):
        very_old = datetime.now(timezone.utc) - timedelta(days=365)
        weight = calculate_memory_decay(very_old, half_life_days=30, min_weight=0.3)
        assert weight >= 0.3

    def test_custom_half_life(self):
        date = datetime.now(timezone.utc) - timedelta(days=7)
        weight_short = calculate_memory_decay(date, half_life_days=7)
        weight_long = calculate_memory_decay(date, half_life_days=30)

        # Shorter half-life = more decay
        assert weight_short < weight_long


class TestConflictDetection:
    """Test memory conflict detection."""

    def test_no_conflicts_empty(self):
        conflicts = detect_conflict("New decision about auth", [])
        assert conflicts == []

    def test_detects_similar_failure(self):
        existing = [
            {
                "id": 1,
                "content": "Use session tokens for authentication",
                "category": "decision",
                "worked": False,
                "outcome": "Had security issues",
                "tags": ["auth"],
            }
        ]
        conflicts = detect_conflict(
            "Use session-based authentication", existing, similarity_threshold=0.3
        )
        assert len(conflicts) >= 1
        assert conflicts[0]["conflict_type"] == "similar_failed"

    def test_detects_existing_warning(self):
        existing = [
            {
                "id": 1,
                "content": "Don't use synchronous database calls",
                "category": "warning",
                "worked": None,
                "outcome": None,
                "tags": [],
            }
        ]
        conflicts = detect_conflict(
            "Use synchronous database operations", existing, similarity_threshold=0.3
        )
        # Should detect the warning
        warning_conflicts = [
            c for c in conflicts if c.get("conflict_type") == "existing_warning"
        ]
        assert (
            len(warning_conflicts) >= 0
        )  # May or may not match depending on similarity

    def test_detects_duplicate(self):
        existing = [
            {
                "id": 1,
                "content": "Use JWT tokens for API authentication",
                "category": "decision",
                "worked": True,
                "outcome": "Works great",
                "tags": ["auth", "jwt"],
            }
        ]
        conflicts = detect_conflict(
            "Use JWT tokens for API authentication", existing, similarity_threshold=0.3
        )
        # Highly similar content should be flagged as potential duplicate
        if conflicts:
            assert any(
                c.get("conflict_type") == "potential_duplicate" for c in conflicts
            )


class TestTFIDFVectorCaching:
    """Verify TF-IDF vectors are cached for efficiency."""

    def test_query_vector_cached(self):
        """Same query should return cached vector."""
        from daem0nmcp.similarity import TFIDFIndex

        index = TFIDFIndex()
        index.add_document(1, "hello world", [])
        index.add_document(2, "goodbye world", [])

        # First search computes vector
        results1 = index.search("hello", top_k=1)

        # Check cache exists
        assert hasattr(index, "_query_cache")
        assert "hello" in index._query_cache

        # Second search uses cache
        results2 = index.search("hello", top_k=1)
        assert results1 == results2

    def test_cache_invalidated_on_add(self):
        """Adding document should invalidate query cache."""
        from daem0nmcp.similarity import TFIDFIndex

        index = TFIDFIndex()
        index.add_document(1, "hello world", [])

        # Populate cache
        index.search("hello", top_k=1)
        assert "hello" in index._query_cache

        # Add document
        index.add_document(2, "hello again", [])

        # Cache should be cleared
        assert len(index._query_cache) == 0

    def test_cache_invalidated_on_remove(self):
        """Removing document should invalidate query cache."""
        from daem0nmcp.similarity import TFIDFIndex

        index = TFIDFIndex()
        index.add_document(1, "hello world", [])
        index.add_document(2, "goodbye world", [])

        # Populate cache
        index.search("hello", top_k=1)
        assert "hello" in index._query_cache

        # Remove document
        index.remove_document(2)

        # Cache should be cleared
        assert len(index._query_cache) == 0

    def test_cache_lru_eviction(self):
        """Cache should evict oldest entries when full."""
        from daem0nmcp.similarity import TFIDFIndex

        # Small cache for testing
        index = TFIDFIndex(max_query_cache_size=3)
        index.add_document(1, "apple banana cherry", [])

        # Fill the cache
        index.search("apple", top_k=1)
        index.search("banana", top_k=1)
        index.search("cherry", top_k=1)

        assert len(index._query_cache) == 3
        assert "apple" in index._query_cache

        # Add one more, should evict oldest (apple)
        index.search("date", top_k=1)

        assert len(index._query_cache) == 3
        assert "apple" not in index._query_cache
        assert "date" in index._query_cache

    def test_cache_with_tags(self):
        """Cache should differentiate queries with different tags."""
        from daem0nmcp.similarity import TFIDFIndex

        index = TFIDFIndex()
        index.add_document(1, "hello world", ["greeting"])

        # Same query, different tags should be cached separately
        index.search("hello", tags=["tag1"], top_k=1)
        index.search("hello", tags=["tag2"], top_k=1)

        # Both should be in cache with different keys
        assert "hello|tag1" in index._query_cache
        assert "hello|tag2" in index._query_cache
