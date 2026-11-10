"""The Masker: every masked form of a value, ordering, JSON values, and copies."""

from __future__ import annotations

import json
import time
from urllib.parse import quote, quote_plus

import pytest

from treadle.engine.masking import MASK, MIN_SECRET_LENGTH, Masker


def test_the_mask_and_the_minimum_length_are_the_documented_values() -> None:
    assert (MASK, MIN_SECRET_LENGTH) == ("***", 4)


def test_a_raw_value_is_masked_everywhere_it_appears() -> None:
    masker = Masker(["hunter22"])

    assert masker.mask("pw=hunter22, again hunter22.") == "pw=***, again ***."


def test_text_without_values_comes_back_unchanged() -> None:
    assert Masker().mask("nothing to hide") == "nothing to hide"
    assert Masker(["abcd"]).mask("nothing to hide") == "nothing to hide"


@pytest.mark.parametrize("value", ["abc", "", "    ", " \t\n "])
def test_short_and_blank_values_are_ignored(value: str) -> None:
    masker = Masker([value])
    masker.add(value)

    assert masker.longest == 0
    assert masker.mask(f"x{value}y    z") == f"x{value}y    z"


def test_a_value_of_exactly_four_characters_is_masked() -> None:
    assert Masker(["abcd"]).mask("xabcdx") == "x***x"


def test_each_line_of_a_multi_line_value_is_masked_when_long_enough() -> None:
    value = "-----BEGIN KEY-----\nAAAABBBB\nok\n    \n-----END KEY-----"
    masker = Masker([value])

    text = "line AAAABBBB, short ok, blank    , end -----END KEY-----"
    assert masker.mask(text) == "line ***, short ok, blank    , end ***"
    assert masker.mask(f"[{value}]") == "[***]"


@pytest.mark.parametrize(
    "value",
    ['say "hi"', "back\\slash", "tab\there", "line\nbreak", "café crème", "emoji \U0001f512"],
)
def test_the_json_escaped_form_is_masked(value: str) -> None:
    masker = Masker([value])
    document = json.dumps({"token": value})

    assert value not in masker.mask(document)
    assert masker.mask(document) == '{"token": "***"}'


def test_both_url_encoded_forms_are_masked() -> None:
    value = "p@ss word/&="
    masker = Masker([value])

    url = f"https://x.test/?a={quote(value, safe='')}&b={quote_plus(value)}"
    assert masker.mask(url) == "https://x.test/?a=***&b=***"


def test_the_longest_value_wins_where_values_overlap() -> None:
    masker = Masker(["abcd", "abcdefgh"])

    assert masker.mask("abcdefgh abcd") == "*** ***"


def test_regex_characters_in_values_are_literal() -> None:
    masker = Masker(["a.b*c+(d)"])

    assert masker.mask("a.b*c+(d) aXbbbc+(d)") == "*** aXbbbc+(d)"


def test_add_masks_later_text_only() -> None:
    masker = Masker()
    before = masker.mask("value-1234")

    masker.add("value-1234")

    assert (before, masker.mask("value-1234")) == ("value-1234", "***")


def test_many_values_added_in_a_row_compile_once() -> None:
    masker = Masker(["base-secret"])
    values = [f"tok-{index:05d}-0123456789" for index in range(1000)]
    started = time.perf_counter()

    added = [masker.add(value) for value in values]
    masked = [masker.mask(f"line {value}") for value in values]

    assert time.perf_counter() - started < 3  # compiling 1,000 times takes over 7 seconds
    assert all(added)
    assert masked == ["line ***"] * 1000
    assert masker.longest == len(values[0])


def test_add_reports_whether_the_value_was_new() -> None:
    masker = Masker(["known-value"])

    assert masker.add("fresh-value")
    assert not masker.add("fresh-value")
    assert not masker.add("known-value")
    assert not masker.add("abc")


def test_knows_tells_whether_masking_a_value_needs_anything_new() -> None:
    masker = Masker(["known-value", "p@ss word"])

    assert masker.knows("known-value")
    assert masker.knows("p@ss word")
    assert masker.knows("abc")  # too short to mask
    assert masker.knows("    ")
    assert not masker.knows("other-value")
    assert not masker.knows("known-value\nsecond line")


def test_adding_a_known_value_again_keeps_masking_it() -> None:
    masker = Masker(["value-1234"])
    masker.add("value-1234")

    assert masker.mask("value-1234") == "***"


def test_mask_json_masks_keys_and_strings_at_any_depth_and_copies() -> None:
    masker = Masker(["s3cret"])
    value = {
        "s3cret": "x",
        "list": ["a s3cret", 1, 2.5, True, None, {"deep": ["s3cret"]}],
        "n": 7,
    }

    masked = masker.mask_json(value)

    assert masked == {
        "***": "x",
        "list": ["a ***", 1, 2.5, True, None, {"deep": ["***"]}],
        "n": 7,
    }
    assert value["s3cret"] == "x"  # the original is untouched
    assert masker.mask_json("plain") == "plain"
    assert masker.mask_json(None) is None


def test_mask_json_masks_a_number_whose_json_text_holds_a_value() -> None:
    masker = Masker(["834512907", "2.718"])
    value = {
        "id": 834512907,
        "negative": -834512907,
        "longer": 98345129070,
        "float": 2.718,
        "as_float": 834512907.0,
        "other": 12,
        "flag": True,
        "nothing": None,
    }

    assert masker.mask_json(value) == {
        "id": MASK,
        "negative": MASK,
        "longer": MASK,
        "float": MASK,
        "as_float": MASK,
        "other": 12,
        "flag": True,
        "nothing": None,
    }
    assert masker.mask_json([834512907]) == [MASK]
    assert Masker().mask_json(834512907) == 834512907


def test_mask_json_keeps_booleans_even_when_a_value_is_their_json_text() -> None:
    masker = Masker(["true", "false"])

    assert masker.mask_json([True, False, "true"]) == [True, False, MASK]


def test_mask_json_copies_containers_even_without_values() -> None:
    value = {"a": [1, {"b": "c"}]}

    masked = Masker().mask_json(value)

    assert masked == value
    assert masked is not value


def test_a_copy_is_independent_of_the_original() -> None:
    original = Masker(["first-value"])
    clone = original.copy()

    clone.add("second-value")
    original.add("third-value")

    assert clone.mask("first-value second-value third-value") == "*** *** third-value"
    assert original.mask("first-value second-value third-value") == "*** second-value ***"
    assert clone.longest == len("second-value")


def test_longest_is_the_longest_masked_form() -> None:
    value = "a b/c"  # its quote() form "a%20b%2Fc" is longer than the value itself

    assert Masker().longest == 0
    assert Masker([value]).longest == len(quote(value, safe=""))
