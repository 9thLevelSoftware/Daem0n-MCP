"""Token counting for scripts written without spaces between words."""

from __future__ import annotations

import re

import pytest

from daem0nmcp.retrieval.composer import EvidenceComposer
from daem0nmcp.retrieval.runtime import CoreTokenizer
from tests.test_retrieval_composer import _source

_HAN = "\u8a18\u61b6\u691c\u7d22\u306e\u4e88\u7b97"  # kanji with one hiragana
_HIRAGANA = "\u3053\u308c\u306f\u3066\u3059\u3068"
_KATAKANA = "\u30c8\u30fc\u30af\u30f3\u30ab\u30a6\u30f3\u30c8"
_THAI = "\u0e01\u0e02\u0e04\u0e07\u0e08\u0e09\u0e0a\u0e0b"  # consonants only
_HANGUL = "\uae30\uc5b5\uac80\uc0c9"
_EMOJI = "\U0001f600\U0001f680\U0001f389\U0001f525"


def _legacy_count(text: str) -> int:
    """The word/punctuation count Latin text has always had."""

    return len(re.findall(r"\w+|[^\w\s]", text))


@pytest.mark.parametrize(
    "text",
    [
        _HAN * 67,
        _HIRAGANA * 80,
        _KATAKANA * 60,
        _THAI * 60,
        _HANGUL * 100,
        "\u3400\u4dbf\U00020000\U0002a6df\uf900\uff66\uff9f" * 50,
        "\ua000\ua001\ua002\ua48c" * 100,
        "\U00017000\U00017001\U00018b00" * 120,
        "\U0001b000\U0001b001\U0001b170" * 120,
        "\u1950\u1951\u1980\u1981" * 100,
        "\u1a20\u1a21\u1a22" * 120,
        "\uaa80\uaa81\uaa82" * 120,
        "\ua984\ua985\ua98f" * 120,
        "\u1b05\u1b06\u1b13" * 120,
        "\u3005\u3006\u3007" * 120,
    ],
    ids=[
        "han",
        "hiragana",
        "katakana",
        "thai",
        "hangul",
        "han-extensions",
        "yi",
        "tangut-khitan",
        "kana-supplement-nushu",
        "tai-le-new-tai-lue",
        "tai-tham",
        "tai-viet",
        "javanese",
        "balinese",
        "cjk-marks",
    ],
)
def test_unsegmented_script_runs_count_every_character(text):
    # A model tokenizer spends about one token or more per such character,
    # so one token for the whole unbroken run under-counts by orders of
    # magnitude.
    assert len(text) >= 350
    assert CoreTokenizer().count_tokens(text) == len(text)


def test_emoji_count_at_least_one_token_per_code_point():
    assert CoreTokenizer().count_tokens(_EMOJI * 100) == 400
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert CoreTokenizer().count_tokens(family * 10) == 50


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abc" + _HAN[:2] + "def", 4),
        ("v2" + _KATAKANA[:3] + " ok", 5),
        (_HAN[:3] + "\u3002" + _HAN[3:5], 6),
        ("user_id\uff1a" + _HANGUL[:2], 4),
    ],
)
def test_mixed_runs_split_only_at_unsegmented_characters(text, expected):
    assert CoreTokenizer().count_tokens(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\t ",
        "Use signed session cookies; avoid server-side state.",
        "fix the crash in src/auth/session.py (see pkg.Router.apply_routes)",
        "snake_case_identifier CamelCase v1.2.3 0xDEADBEEF 3.14e-9",
        "na\u00efve caf\u00e9 \u00fcber stra\u00dfe \u0141\u00f3d\u017a \u00e7a",
        "\u041f\u0440\u0438\u0432\u0435\u0442, \u043c\u0438\u0440! \u0393\u03b5\u03b9\u03ac \u03c3\u03bf\u03c5",
        "\u05e9\u05dc\u05d5\u05dd \u0645\u0631\u062d\u0628\u0627 \u0928\u092e\u0938\u094d\u0924\u0947",
        "[E1] decision (score 0.42)\n\n[E2] warning: don't \u2014 really\u2026",
        "mem_" + "a" * 64 + " evt_" + "0" * 64,
        "x" * 5000,
    ],
)
def test_segmented_script_counts_are_unchanged(text):
    assert CoreTokenizer().count_tokens(text) == _legacy_count(text)


def test_token_budget_bounds_packed_cjk_evidence():
    budget = 256
    composer = EvidenceComposer(tokenizer=CoreTokenizer())
    selected = tuple(
        _source(digit, _HAN * 67, score=1.0 - index / 100)
        for index, digit in enumerate("123456789abcdef")
    )

    result = composer.compose(selected, token_budget=budget)

    packed = sum(
        1 for character in result.context.text if "\u3040" <= character <= "\u9fff"
    )
    assert result.context.rendered_tokens <= budget
    assert packed <= budget
