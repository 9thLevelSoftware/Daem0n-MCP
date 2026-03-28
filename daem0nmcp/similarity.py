"""
Similarity Engine - TF-IDF based semantic matching for Daem0nMCP.

This replaces the naive keyword matching with actual text similarity:
- TF-IDF vectorization for term importance
- Cosine similarity for matching
- Memory decay for time-based relevance
- Conflict detection for contradicting decisions
"""

import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# =============================================================================
# Constants for decay and similarity calculations
# =============================================================================

# Memory decay defaults
DEFAULT_DECAY_HALF_LIFE_DAYS = 30.0  # Days until memory relevance is halved
MIN_DECAY_WEIGHT = 0.3  # Floor for decayed memory weight (never goes below this)

# Extended stop words for better signal extraction
STOP_WORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "between",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "and",
    "but",
    "if",
    "or",
    "because",
    "until",
    "while",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "we",
    "they",
    "them",
    "what",
    "which",
    "who",
    "whom",
    "i",
    "you",
    "he",
    "she",
    "use",
    "using",
    "also",
    "like",
    "get",
    "got",
    "make",
    "made",
    "take",
    "took",
    "come",
    "came",
    "go",
    "went",
    "see",
    "saw",
    "know",
    "knew",
    "think",
    "thought",
    "want",
    "wanted",
    "look",
    "looked",
    "give",
    "gave",
    "tell",
    "told",
    "work",
    "worked",
    "call",
    "called",
    "try",
    "tried",
    "ask",
    "asked",
    "put",
    "keep",
    "let",
    "begin",
    "seem",
    "help",
    "show",
    "hear",
    "play",
    "run",
    "move",
    "live",
    "believe",
}


def extract_code_symbols(text: str) -> list[str]:
    """
    Extract code symbols from text - function names, class names, variables.

    Looks for:
    - Backtick-enclosed code: `functionName`, `ClassName`, `CONSTANT`
    - CamelCase identifiers: getUserById, UserService
    - snake_case identifiers: get_user_by_id, user_service
    - SCREAMING_SNAKE: MAX_RETRIES, API_KEY
    - Method calls: .methodName(), .property

    Returns symbols as-is (preserves case for exact matching) plus lowercased versions.
    """
    if not text:
        return []

    symbols: set[str] = set()

    # Extract backtick-enclosed code (highest confidence)
    backtick_matches = re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*(?:\([^)]*\))?)`", text)
    for match in backtick_matches:
        # Remove function call parens for the symbol
        clean = re.sub(r"\([^)]*\)", "", match)
        if len(clean) >= 2:
            symbols.add(clean)

    # Extract CamelCase identifiers (likely class/function names)
    # Must start with letter, contain at least one lowercase and one uppercase
    camel_matches = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b", text)
    for match in camel_matches:
        if len(match) >= 3:
            symbols.add(match)

    # Extract lowerCamelCase (likely function/method names)
    lower_camel = re.findall(r"\b([a-z]+(?:[A-Z][a-z0-9]*)+)\b", text)
    for match in lower_camel:
        if len(match) >= 3:
            symbols.add(match)

    # Extract snake_case identifiers
    snake_matches = re.findall(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", text)
    for match in snake_matches:
        if len(match) >= 3 and match not in STOP_WORDS:
            symbols.add(match)

    # Extract SCREAMING_SNAKE_CASE (constants)
    screaming = re.findall(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", text)
    for match in screaming:
        if len(match) >= 3:
            symbols.add(match)

    # Extract method/property access: .methodName or .property
    method_matches = re.findall(r"\.([a-zA-Z_][a-zA-Z0-9_]*)", text)
    for match in method_matches:
        if len(match) >= 2 and match.lower() not in STOP_WORDS:
            symbols.add(match)

    # Return both original case and lowercased versions for matching flexibility
    result = []
    for sym in symbols:
        result.append(sym)
        lower = sym.lower()
        if lower != sym:
            result.append(lower)

    return result


def tokenize(text: str) -> list[str]:
    """
    Tokenize text into meaningful terms.

    - Lowercases
    - Extracts alphanumeric tokens
    - Preserves technical terms (e.g., 'api', 'jwt', 'oauth')
    - Filters stop words
    - Handles snake_case and camelCase
    - Extracts code symbols (function/class names)
    """
    if not text:
        return []

    # First, extract code symbols (preserves exact identifiers)
    symbols = extract_code_symbols(text)

    # Normalize common language tokens
    text = text.replace("C++", "cpp").replace("c++", "cpp")
    text = text.replace("C#", "csharp").replace("c#", "csharp")

    # Handle camelCase -> separate words
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)

    # Handle snake_case -> separate words
    text = text.replace("_", " ")

    # Extract words
    words = re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)

    # Filter stop words and very short words (but keep technical acronyms like 'db', 'ui', 'api')
    tokens = []
    max_len = 64
    for w in words:
        if w in STOP_WORDS:
            continue
        if len(w) < 2:
            continue
        if len(w) > max_len:
            w = w[:max_len]
        # Keep short technical terms
        if len(w) == 2 and w not in {
            "db",
            "ui",
            "id",
            "io",
            "os",
            "ip",
            "vm",
            "ai",
            "ml",
        }:
            continue
        tokens.append(w)

    # Add extracted symbols (both original case and lowercased)
    tokens.extend([s.lower() for s in symbols])

    return tokens


def extract_keywords(text: str, tags: list[str] | None = None) -> str:
    """
    Extract searchable keywords from text and optional tags.

    Uses the tokenizer under the hood for consistent text processing.
    Returns a sorted, deduplicated string of keywords for storage/indexing.

    Args:
        text: The text to extract keywords from
        tags: Optional list of tags to include (with boosted weight)

    Returns:
        Space-separated string of sorted unique keywords
    """
    tokens = tokenize(text)
    if tags:
        for tag in tags:
            tokens.extend(tokenize(tag))
    return " ".join(sorted(set(tokens)))


class TFIDFIndex:
    """
    A simple but effective TF-IDF index for document similarity.

    This is intentionally lightweight - no external ML deps required.
    """

    def __init__(self, max_query_cache_size: int = 100):
        self.documents: dict[int, list[str]] = {}  # doc_id -> tokens
        self.document_vectors: dict[
            int, dict[str, float]
        ] = {}  # doc_id -> {term: tfidf}
        self.idf_cache: dict[str, float] = {}
        self.doc_count = 0
        # Query vector cache with LRU eviction
        self._query_cache: dict[str, dict[str, float]] = {}
        self._query_cache_order: list[str] = []  # Track insertion order for LRU
        self._max_query_cache_size: int = max_query_cache_size

    def add_document(
        self, doc_id: int, text: str, tags: list[str] | None = None
    ) -> None:
        """Add a document to the index."""
        tokens = tokenize(text)

        # Add tags with boosted weight (add them multiple times)
        if tags:
            for tag in tags:
                tag_tokens = tokenize(tag)
                tokens.extend(tag_tokens * 3)  # Tags get 3x weight

        self.documents[doc_id] = tokens
        self.doc_count = len(self.documents)
        self._invalidate_cache()

    def remove_document(self, doc_id: int) -> None:
        """Remove a document from the index."""
        if doc_id in self.documents:
            del self.documents[doc_id]
            if doc_id in self.document_vectors:
                del self.document_vectors[doc_id]
            self.doc_count = len(self.documents)
            self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        """Clear cached computations."""
        self.idf_cache.clear()
        self.document_vectors.clear()
        self._query_cache.clear()
        self._query_cache_order.clear()

    def _compute_idf(self, term: str) -> float:
        """Compute IDF for a term."""
        if term in self.idf_cache:
            return self.idf_cache[term]

        doc_freq = sum(1 for tokens in self.documents.values() if term in tokens)

        if doc_freq == 0:
            idf = 0.0
        else:
            # Standard IDF with smoothing
            idf = math.log((self.doc_count + 1) / (doc_freq + 1)) + 1

        self.idf_cache[term] = idf
        return idf

    def _get_tfidf_vector(self, doc_id: int) -> dict[str, float]:
        """Get TF-IDF vector for a document."""
        if doc_id in self.document_vectors:
            return self.document_vectors[doc_id]

        tokens = self.documents.get(doc_id, [])
        if not tokens:
            return {}

        # Compute TF
        tf = Counter(tokens)
        max_tf = max(tf.values()) if tf else 1

        # Compute TF-IDF
        vector = {}
        for term, count in tf.items():
            tf_normalized = 0.5 + 0.5 * (count / max_tf)  # Augmented TF
            idf = self._compute_idf(term)
            vector[term] = tf_normalized * idf

        self.document_vectors[doc_id] = vector
        return vector

    def _query_vector(
        self, query: str, tags: list[str] | None = None
    ) -> dict[str, float]:
        """Convert a query to a TF-IDF vector with caching."""
        # Build cache key (query + sorted tags for consistency)
        cache_key = query
        if tags:
            cache_key = f"{query}|{','.join(sorted(tags))}"

        # Check cache first
        if cache_key in self._query_cache:
            # Move to end for LRU (most recently used)
            if cache_key in self._query_cache_order:
                self._query_cache_order.remove(cache_key)
                self._query_cache_order.append(cache_key)
            return self._query_cache[cache_key]

        tokens = tokenize(query)

        if tags:
            for tag in tags:
                tokens.extend(tokenize(tag) * 3)

        if not tokens:
            return {}

        tf = Counter(tokens)
        max_tf = max(tf.values()) if tf else 1

        vector = {}
        for term, count in tf.items():
            tf_normalized = 0.5 + 0.5 * (count / max_tf)
            idf = self._compute_idf(term)
            vector[term] = tf_normalized * idf

        # Cache the result with LRU eviction
        if len(self._query_cache) >= self._max_query_cache_size:
            # Evict oldest entry (LRU)
            oldest_key = self._query_cache_order.pop(0)
            del self._query_cache[oldest_key]

        self._query_cache[cache_key] = vector
        self._query_cache_order.append(cache_key)

        return vector

    def cosine_similarity(
        self, vec1: dict[str, float], vec2: dict[str, float]
    ) -> float:
        """Compute cosine similarity between two vectors."""
        if not vec1 or not vec2:
            return 0.0

        # Get common terms
        common_terms = set(vec1.keys()) & set(vec2.keys())

        if not common_terms:
            return 0.0

        # Dot product
        dot_product = sum(vec1[t] * vec2[t] for t in common_terms)

        # Magnitudes
        mag1 = math.sqrt(sum(v**2 for v in vec1.values()))
        mag2 = math.sqrt(sum(v**2 for v in vec2.values()))

        if mag1 == 0 or mag2 == 0:
            return 0.0

        return dot_product / (mag1 * mag2)

    def search(
        self,
        query: str,
        tags: list[str] | None = None,
        top_k: int = 10,
        threshold: float = 0.1,
    ) -> list[tuple[int, float]]:
        """
        Search for documents similar to the query.

        Returns: List of (doc_id, similarity_score) tuples, sorted by score descending.
        """
        query_vec = self._query_vector(query, tags)

        if not query_vec:
            return []

        results = []
        for doc_id in self.documents:
            doc_vec = self._get_tfidf_vector(doc_id)
            score = self.cosine_similarity(query_vec, doc_vec)

            if score >= threshold:
                results.append((doc_id, score))

        # Sort by score descending
        results.sort(key=lambda x: x[1], reverse=True)

        return results[:top_k]

    def document_similarity(self, doc_id1: int, doc_id2: int) -> float:
        """Compute similarity between two indexed documents."""
        vec1 = self._get_tfidf_vector(doc_id1)
        vec2 = self._get_tfidf_vector(doc_id2)
        return self.cosine_similarity(vec1, vec2)


def calculate_memory_decay(
    created_at: datetime,
    half_life_days: float = DEFAULT_DECAY_HALF_LIFE_DAYS,
    min_weight: float = MIN_DECAY_WEIGHT,
) -> float:
    """
    Calculate time-based decay for memory relevance.

    Uses exponential decay with configurable half-life.
    Recent memories get full weight, older ones decay but never below min_weight.

    Args:
        created_at: When the memory was created
        half_life_days: Days until weight is halved (default: 30)
        min_weight: Minimum weight floor (default: 0.3)

    Returns:
        Weight multiplier between min_weight and 1.0
    """
    now = datetime.now(timezone.utc)

    # Handle naive datetime
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    age_days = (now - created_at).total_seconds() / 86400

    if age_days <= 0:
        return 1.0

    # Exponential decay
    decay_constant = math.log(2) / half_life_days
    weight = math.exp(-decay_constant * age_days)

    # Apply minimum floor
    return max(weight, min_weight)


def detect_conflict(
    new_content: str, existing_memories: list[dict], similarity_threshold: float = 0.6
) -> list[dict]:
    """
    Detect if new content conflicts with existing memories.

    A conflict is when:
    - Content is highly similar (>threshold)
    - But one worked and one didn't, or they're in opposing categories

    Args:
        new_content: The new memory content
        existing_memories: List of existing memories with 'content', 'worked', 'category' fields
        similarity_threshold: How similar content must be to consider conflict

    Returns:
        List of conflicting memories with conflict details
    """
    if not existing_memories:
        return []

    def _has_negation(text: str) -> bool:
        return bool(
            re.search(
                r"\b(no|not|never|avoid|dont|don't|cannot|can't|without)\b",
                text.lower(),
            )
        )

    # Build temporary index
    index = TFIDFIndex()
    for i, mem in enumerate(existing_memories):
        content = mem.get("content", "")
        tags = mem.get("tags", [])
        index.add_document(i, content, tags)

    # Find similar memories
    query_vec = index._query_vector(new_content)

    conflicts = []
    for i, mem in enumerate(existing_memories):
        doc_vec = index._get_tfidf_vector(i)
        similarity = index.cosine_similarity(query_vec, doc_vec)

        if similarity >= similarity_threshold:
            # Check for actual conflict
            worked = mem.get("worked")
            category = mem.get("category")

            conflict_info = {
                "memory_id": mem.get("id"),
                "content": mem.get("content"),
                "similarity": round(similarity, 3),
                "worked": worked,
                "category": category,
                "conflict_type": None,
            }

            # Determine conflict type
            if worked is False:
                conflict_info["conflict_type"] = "similar_failed"
                conflict_info["warning"] = (
                    f"Similar approach failed before: {mem.get('outcome', 'no details')}"
                )
            elif category == "warning":
                conflict_info["conflict_type"] = "existing_warning"
                conflict_info["warning"] = (
                    f"Existing warning about this: {mem.get('content')}"
                )
            elif similarity > 0.8:
                conflict_info["conflict_type"] = "potential_duplicate"
                conflict_info["warning"] = (
                    "Very similar memory already exists - consider updating instead"
                )

            if conflict_info["conflict_type"] is None and _has_negation(new_content) != _has_negation(mem.get("content", "")):
                conflict_info["conflict_type"] = "polarity_conflict"
                conflict_info["warning"] = (
                    "Potential contradiction detected (negation mismatch)"
                )

            if conflict_info["conflict_type"]:
                conflicts.append(conflict_info)

    return conflicts


# Global index instance for the memory manager to use
_global_index: TFIDFIndex | None = None


def get_global_index() -> TFIDFIndex:
    """Get or create the global TF-IDF index."""
    global _global_index
    if _global_index is None:
        _global_index = TFIDFIndex()
    return _global_index


def reset_global_index() -> None:
    """Reset the global index (useful for testing)."""
    global _global_index
    _global_index = None
