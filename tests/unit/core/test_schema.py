from __future__ import annotations

import time
from typing import Any

import pytest

from treadle.expr.ast import Template
from treadle.schema import (
    MISSING,
    AnyJSON,
    AnyOf,
    Bool,
    Duration,
    Field,
    Int,
    ListOf,
    MapOf,
    Num,
    Obj,
    Size,
    Spec,
    Str,
    kind_of,
    suggest,
    validate,
)
from treadle.types import Issue, Severity

TEMPLATE = Template("${{ inputs.x }}", ("${{ inputs.x }}",))
RETRY = Obj(
    "retry",
    {"attempts": Field(Int(min=1, max=20), default=1), "delay": Field(Duration(), default=1.0)},
    shorthand="attempts",
)


def problems(spec: Spec, value: object, **kwargs: Any) -> list[tuple[str, str, tuple[Any, ...]]]:
    """Validate and return (code, message, path) for each issue."""
    _, issues = validate(spec, value, **kwargs)
    return [(issue.code, issue.message, issue.path) for issue in issues]


def checked(spec: Spec, value: object, **kwargs: Any) -> object:
    """Validate a value that must pass, and return the normalized value."""
    result, issues = validate(spec, value, **kwargs)
    assert issues == []
    return result


def test_issues_are_errors_with_paths_and_hints() -> None:
    _, issues = validate(Str(choices=["queue", "skip"]), "skp", path=("concurrency",))

    assert issues == [
        Issue(
            Severity.ERROR,
            "E104",
            "must be one of: queue, skip",
            ("concurrency",),
            hint="did you mean 'skip'?",
        )
    ]


class TestStr:
    def test_accepts_strings_only(self) -> None:
        assert checked(Str(), "hello") == "hello"
        assert problems(Str(), 3) == [("E103", "expected a string, got a number", ())]
        assert problems(Str(), None) == [("E103", "expected a string, got null", ())]

    def test_choices_without_a_close_match_have_no_hint(self) -> None:
        _, issues = validate(Str(choices=["bash", "sh"]), "powershell")

        assert [(issue.message, issue.hint) for issue in issues] == [
            ("must be one of: bash, sh", None)
        ]

    def test_pattern_must_match_the_whole_string(self) -> None:
        spec = Str(pattern=r"[a-z]+")

        assert checked(spec, "abc") == "abc"
        assert problems(spec, "abc1") == [("E104", "must match the pattern [a-z]+", ())]
        assert problems(spec, "abc\n") == [("E104", "must match the pattern [a-z]+", ())]

    def test_length_limits(self) -> None:
        assert problems(Str(min_len=1), "") == [("E104", "must not be empty", ())]
        assert problems(Str(min_len=3), "ab") == [("E104", "must be at least 3 characters", ())]
        assert problems(Str(max_len=2), "abc") == [("E104", "must be at most 2 characters", ())]


class TestNumbers:
    def test_int_rejects_booleans_and_floats(self) -> None:
        assert checked(Int(), 3) == 3
        assert problems(Int(), True) == [("E103", "expected an integer, got a boolean", ())]
        assert problems(Int(), 1.0) == [("E103", "expected an integer, got a number", ())]

    @pytest.mark.parametrize(
        ("spec", "value", "message"),
        [
            (Int(min=1, max=20), 0, "must be between 1 and 20"),
            (Int(min=1, max=20), 21, "must be between 1 and 20"),
            (Int(min=1), 0, "must be at least 1"),
            (Int(max=5), 6, "must be at most 5"),
            (Num(min=1.0, max=10.0), 0.5, "must be between 1 and 10"),
            (Duration(min=1, max=7 * 86400), 0.5, "must be between 1s and 7d"),
            (Duration(max=3600), "2h", "must be between 0ms and 1h"),
            (Size(min=1024, max=10 << 20), 10, "must be between 1.0 KiB and 10.0 MiB"),
        ],
    )
    def test_ranges(self, spec: Spec, value: object, message: str) -> None:
        assert problems(spec, value) == [("E104", message, ())]

    def test_num_returns_floats_and_rejects_non_finite_values(self) -> None:
        assert checked(Num(), 2) == 2.0
        assert isinstance(checked(Num(), 2), float)
        assert problems(Num(), False) == [("E103", "expected a number, got a boolean", ())]
        assert problems(Num(), float("nan")) == [("E104", "must be a finite number", ())]
        assert problems(Num(), 10**400) == [("E104", "must be a finite number", ())]

    def test_bool(self) -> None:
        assert checked(Bool(), False) is False
        assert problems(Bool(), "yes") == [
            ("E103", "expected a boolean (true or false), got a string", ())
        ]

    def test_duration_parses_strings_and_numbers(self) -> None:
        assert checked(Duration(), "1h30m") == 5400.0
        assert checked(Duration(), 30) == 30.0
        assert problems(Duration(), [1]) == [
            ("E103", "expected a duration such as 30s, got a list", ())
        ]
        assert problems(Duration(), True) == [
            ("E103", "expected a duration such as 30s, got a boolean", ())
        ]
        [(code, message, _)] = problems(Duration(), "5 minutes")
        assert (code, message.startswith("invalid duration '5 minutes'")) == ("E104", True)

    def test_size_parses_strings_and_integers(self) -> None:
        assert checked(Size(), "64KiB") == 65536
        assert checked(Size(), 100) == 100
        assert problems(Size(), 1.5) == [
            ("E103", "expected a size such as 64KiB, got a number", ())
        ]
        [(code, message, _)] = problems(Size(), "lots")
        assert (code, message.startswith("invalid size 'lots'")) == ("E104", True)


class TestListOf:
    def test_checks_each_item_at_its_index(self) -> None:
        spec = ListOf(Int())

        assert checked(spec, [1, 2]) == [1, 2]
        assert problems(spec, [1, "x"], path=("needs",)) == [
            ("E103", "expected an integer, got a string", ("needs", 1))
        ]
        assert problems(spec, "x") == [("E103", "expected a list, got a string", ())]

    def test_scalar_ok_wraps_a_single_value_at_the_same_path(self) -> None:
        spec = ListOf(Str(), scalar_ok=True)

        assert checked(spec, "build") == ["build"]
        assert problems(spec, 3, path=("needs",)) == [
            ("E103", "expected a string, got a number", ("needs",))
        ]
        assert problems(spec, None) == [
            ("E103", "expected a string or a list of them, got null", ())
        ]

    def test_scalar_ok_also_wraps_one_mapping(self) -> None:
        spec = ListOf(RETRY, scalar_ok=True)

        assert checked(spec, {"attempts": 2}) == [{"attempts": 2, "delay": 1.0}]
        assert checked(spec, 3) == [{"attempts": 3, "delay": 1.0}]

    def test_lengths_and_uniqueness(self) -> None:
        assert problems(ListOf(Str(), min_len=1), []) == [("E104", "must have at least 1 item", ())]
        assert problems(ListOf(Str(), max_len=2), ["a", "b", "c"]) == [
            ("E104", "must have at most 2 items", ())
        ]
        assert problems(ListOf(Str(), unique=True), ["a", "b", "a"], path=("options",)) == [
            ("E104", "duplicate value 'a'", ("options", 2))
        ]

    def test_uniqueness_compares_only_the_items_that_passed_their_check(self) -> None:
        assert problems(ListOf(Str(), unique=True), [1, 1, "a", "a"]) == [
            ("E103", "expected a string, got a number", (0,)),
            ("E103", "expected a string, got a number", (1,)),
            ("E104", "duplicate value 'a'", (3,)),
        ]

    def test_uniqueness_compares_lists_and_mappings_too(self) -> None:
        value = [[1], {"a": 1}, "x", [1], {"a": 1}, [2]]

        assert problems(ListOf(AnyJSON(), unique=True), value) == [
            ("E104", "duplicate value [1]", (3,)),
            ("E104", "duplicate value {'a': 1}", (4,)),
        ]

    def test_uniqueness_takes_linear_time(self) -> None:
        items = [f"item-{index}" for index in range(20_000)]

        started = time.perf_counter()
        found = problems(ListOf(Str(), unique=True), [*items, "item-7"])
        elapsed = time.perf_counter() - started

        assert found == [("E104", "duplicate value 'item-7'", (20_000,))]
        assert elapsed < 0.5


class TestMapOf:
    def test_checks_keys_and_values(self) -> None:
        spec = MapOf(Str(), key_pattern=r"[A-Za-z_][A-Za-z0-9_]*", max_len=2)

        assert checked(spec, {"HOME": "/home/ada"}) == {"HOME": "/home/ada"}
        assert problems(spec, {"1BAD": "x"}, path=("env",)) == [
            (
                "E104",
                "invalid key '1BAD': must match the pattern [A-Za-z_][A-Za-z0-9_]*",
                ("env", "1BAD"),
            )
        ]
        assert problems(spec, {"A": 1}, path=("env",)) == [
            ("E103", "expected a string, got a number", ("env", "A"))
        ]
        assert problems(spec, {"A": "1", "B": "2", "C": "3"}) == [
            ("E104", "must have at most 2 entries", ())
        ]
        assert problems(spec, ["x"]) == [("E103", "expected a mapping, got a list", ())]

    def test_keys_must_be_strings(self) -> None:
        assert problems(MapOf(Int()), {1: 2}) == [
            ("E103", "expected a string key, got a number", ("1",))
        ]

    def test_issues_about_keys_are_also_key_issues(self) -> None:
        spec = MapOf(Str(), key_pattern=r"[A-Z]+")
        keys: list[Issue] = []

        _, issues = validate(spec, {"bad": None, 1: "x"}, path=("env",), key_issues=keys)

        assert [(issue.code, issue.path) for issue in issues] == [
            ("E104", ("env", "bad")),
            ("E103", ("env", "bad")),
            ("E103", ("env", "1")),
            ("E104", ("env", "1")),
        ]
        assert keys == [issues[0], issues[2], issues[3]]


class TestAnyOf:
    def test_returns_the_first_alternative_without_issues(self) -> None:
        spec = AnyOf(Str(choices=["bash", "sh"]), ListOf(Str(), min_len=1))

        assert checked(spec, "sh") == "sh"
        assert checked(spec, ["zsh", "-c"]) == ["zsh", "-c"]

    def test_reports_one_type_error_without_the_alternatives_issues(self) -> None:
        spec = AnyOf(Int(), Bool())

        assert problems(spec, "x", path=("if",)) == [
            ("E103", "expected an integer or a boolean (true or false), got a string", ("if",))
        ]


class TestAnyJSON:
    def test_accepts_nested_json_values(self) -> None:
        value = {"a": [1, 2.5, "x", None, True, {"b": []}]}

        assert checked(AnyJSON(), value) == value

    def test_rejects_non_json_values(self) -> None:
        assert problems(AnyJSON(), {"a": [float("inf")]}) == [
            ("E104", "must be a finite number", ("a", 0))
        ]
        assert problems(AnyJSON(), {"a": {1, 2}}) == [
            ("E103", "expected a JSON value, got set", ("a",))
        ]
        assert problems(AnyJSON(), {True: 1}) == [
            ("E103", "expected a string key, got a boolean", ("True",))
        ]


class TestObj:
    STEP = Obj(
        "step",
        {
            "id": Field(Str(), required=True, help="every step needs an ID"),
            "run": Field(Str()),
            "argv": Field(ListOf(Str())),
            "continue_on_error": Field(Bool(), default=False),
            "env": Field(MapOf(Str()), default={}),
            "timestamp_header": Field(Str(), default=None),
        },
        hints={"runs-on": "Treadle runs on this machine. Remove the key."},
        exclusive=[("run", "argv")],
        require_one=[("run", "argv")],
    )

    def test_fills_defaults_and_keeps_optional_keys_absent(self) -> None:
        result = checked(self.STEP, {"id": "build", "run": "make"})

        assert result == {
            "id": "build",
            "run": "make",
            "continue_on_error": False,
            "env": {},
            "timestamp_header": None,
        }

    def test_defaults_are_copied(self) -> None:
        first = checked(self.STEP, {"id": "a", "run": "x"})
        assert isinstance(first, dict)
        first["env"]["MUTATED"] = "1"

        assert checked(self.STEP, {"id": "b", "run": "x"}) == {
            "id": "b",
            "run": "x",
            "continue_on_error": False,
            "env": {},
            "timestamp_header": None,
        }

    def test_an_explicit_null_passes_when_the_default_is_null(self) -> None:
        result = checked(self.STEP, {"id": "a", "run": "x", "timestamp_header": None})

        assert isinstance(result, dict)
        assert result["timestamp_header"] is None
        assert problems(self.STEP, {"id": "a", "run": None}) == [
            ("E103", "expected a string, got null", ("run",))
        ]

    def test_missing_required_key_uses_the_field_help_as_the_hint(self) -> None:
        _, issues = validate(self.STEP, {"run": "x"}, path=("steps", 0))

        assert [(i.code, i.message, i.path, i.hint) for i in issues] == [
            ("E102", "missing required key 'id'", ("steps", 0), "every step needs an ID")
        ]

    @pytest.mark.parametrize(
        ("key", "hint"),
        [
            ("runs-on", "Treadle runs on this machine. Remove the key."),
            ("continue-on-error", "use `continue_on_error`"),
            ("timout", None),
            ("rnu", "did you mean 'run'?"),
        ],
    )
    def test_unknown_keys_get_hints(self, key: str, hint: str | None) -> None:
        _, issues = validate(self.STEP, {"id": "a", "run": "x", key: 1})

        assert [(i.code, i.message, i.path, i.hint) for i in issues] == [
            ("E101", f"unknown key '{key}' in step", (key,), hint)
        ]

    def test_exclusive_and_require_one_groups(self) -> None:
        assert problems(self.STEP, {"id": "a", "run": "x", "argv": ["y"]}) == [
            ("E105", "'run' and 'argv' can't be used together", ("argv",))
        ]
        assert problems(self.STEP, {"id": "a"}) == [
            ("E106", "one of 'run' or 'argv' is required", ())
        ]

    def test_unknown_and_exclusive_keys_are_key_issues(self) -> None:
        keys: list[Issue] = []
        value = {"id": "a", "run": "x", "argv": ["y"], "rnu": 1}

        _, issues = validate(self.STEP, value, path=("steps", 0), key_issues=keys)

        assert keys == issues
        assert [(issue.code, issue.path) for issue in keys] == [
            ("E101", ("steps", 0, "rnu")),
            ("E105", ("steps", 0, "argv")),
        ]

    def test_value_issues_arent_key_issues(self) -> None:
        keys: list[Issue] = []

        _, issues = validate(self.STEP, {"id": 5, "run": "x"}, key_issues=keys)

        assert [issue.code for issue in issues] == ["E103"]
        assert keys == []

    def test_groups_of_three_list_every_key(self) -> None:
        spec = Obj("x", {k: Field(Int()) for k in "abc"}, exclusive=[("a", "b", "c")])

        assert problems(spec, {"a": 1, "b": 2, "c": 3}) == [
            ("E105", "'a', 'b', and 'c' can't be used together", ("b",))
        ]

    def test_requires_a_mapping(self) -> None:
        assert problems(self.STEP, "make") == [
            ("E103", "expected a step mapping, got a string", ())
        ]

    def test_shorthand_sets_the_primary_field_at_the_same_path(self) -> None:
        assert checked(RETRY, 3) == {"attempts": 3, "delay": 1.0}
        assert problems(RETRY, 0, path=("retry",)) == [
            ("E104", "must be between 1 and 20", ("retry",))
        ]
        assert problems(RETRY, [3]) == [
            ("E103", "expected an integer or a retry mapping, got a list", ())
        ]

    def test_non_string_keys_are_unknown(self) -> None:
        assert problems(RETRY, {True: 1}) == [("E101", "unknown key 'True' in retry", ("True",))]

    def test_fields_and_missing(self) -> None:
        assert list(RETRY.fields) == ["attempts", "delay"]
        assert Field(Int()).default is MISSING


class TestTemplates:
    @pytest.mark.parametrize(
        "spec", [Str(), Int(), Duration(), ListOf(Str()), MapOf(Str()), RETRY, AnyJSON()]
    )
    def test_templates_pass_any_spec_when_allowed(self, spec: Spec) -> None:
        assert checked(spec, TEMPLATE, allow_templates=True) is TEMPLATE

    def test_templates_pass_at_any_depth(self) -> None:
        spec = Obj("http", {"headers": Field(MapOf(Str())), "json": Field(AnyJSON())})
        value = {"headers": {"X-Ref": TEMPLATE}, "json": {"ids": [TEMPLATE]}}

        assert checked(spec, value, allow_templates=True) == value

    def test_templates_are_a_wrong_type_otherwise(self) -> None:
        assert problems(Str(), TEMPLATE) == [("E103", "expected a string, got a template", ())]


def test_suggest() -> None:
    assert suggest("startswith", ["startsWith", "endsWith"]) == "startsWith"
    assert suggest("timout", ["timeout", "retry"]) == "timeout"
    assert suggest("xyz", ["timeout", "retry"]) is None


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (None, "null"),
        (True, "a boolean"),
        (3, "a number"),
        (1.5, "a number"),
        ("x", "a string"),
        ([1], "a list"),
        ({"a": 1}, "a mapping"),
        (TEMPLATE, "a template"),
        ({1}, "set"),
    ],
)
def test_kind_of_names_the_type_of_a_value_for_messages(value: object, kind: str) -> None:
    assert kind_of(value) == kind
