"""T183 — compile YAML condition strings into callable predicates.

The condition is loaded from rules in our own repo (not user input)
but `eval()` is still a footgun — restricted-eval has a long
history of escapes (`().__class__.__bases__[0]…`). The compiler
walks the AST and rejects any node outside the arithmetic +
comparison whitelist.

`compile_condition(src)` returns a `CompiledCondition` exposing:
  - `predicate(ctx)`           → bool
  - `referenced_names`         → set[str] (used by T184/T185)
"""

import pytest

from coaching.library.condition_compiler import (
    ConditionCompileError,
    compile_condition,
)


def test_simple_less_than_true() -> None:
    cond = compile_condition("a < b")
    assert cond.predicate({"a": 1, "b": 2}) is True


def test_simple_less_than_false() -> None:
    cond = compile_condition("a < b")
    assert cond.predicate({"a": 5, "b": 2}) is False


def test_arithmetic_in_comparison() -> None:
    """The canonical ADR-013 condition shape:
    apex_speed < reference_apex_speed * 0.93"""
    cond = compile_condition(
        "apex_speed < reference_apex_speed * 0.93"
    )
    assert (
        cond.predicate(
            {"apex_speed": 92.0, "reference_apex_speed": 100.0}
        )
        is True
    )
    assert (
        cond.predicate(
            {"apex_speed": 95.0, "reference_apex_speed": 100.0}
        )
        is False
    )


def test_boolean_conjunction() -> None:
    cond = compile_condition("a > 0 and b > 0")
    assert cond.predicate({"a": 1, "b": 1}) is True
    assert cond.predicate({"a": 1, "b": -1}) is False


def test_boolean_disjunction() -> None:
    cond = compile_condition("a > 0 or b > 0")
    assert cond.predicate({"a": -1, "b": 1}) is True
    assert cond.predicate({"a": -1, "b": -1}) is False


def test_negation() -> None:
    cond = compile_condition("not (a < b)")
    assert cond.predicate({"a": 5, "b": 2}) is True


def test_equality_and_inequality() -> None:
    eq = compile_condition("a == b")
    ne = compile_condition("a != b")
    assert eq.predicate({"a": 1, "b": 1}) is True
    assert ne.predicate({"a": 1, "b": 1}) is False


def test_referenced_names_returned() -> None:
    cond = compile_condition(
        "apex_speed < reference_apex_speed * 0.93"
    )
    assert cond.referenced_names == {
        "apex_speed",
        "reference_apex_speed",
    }


def test_unknown_variable_raises_with_name(
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
) -> None:
    cond = compile_condition("a < missing_var")
    with pytest.raises(KeyError) as exc_info:
        cond.predicate({"a": 1})
    assert "missing_var" in str(exc_info.value)


def test_function_call_rejected() -> None:
    """The whole point of the AST-walk approach. `eval` with empty
    globals can still be escaped via attribute traversal — the
    compiler refuses to even compile."""
    with pytest.raises(ConditionCompileError):
        compile_condition("__import__('os').system('whoami')")


def test_attribute_access_rejected() -> None:
    with pytest.raises(ConditionCompileError):
        compile_condition("a.__class__")


def test_subscript_rejected() -> None:
    with pytest.raises(ConditionCompileError):
        compile_condition("a[0] < b")


def test_invalid_python_syntax_raises() -> None:
    with pytest.raises(ConditionCompileError):
        compile_condition("a <<<<< b")


def test_chained_comparison() -> None:
    """Python supports `a < b < c`. Our YAML rules will write
    these — must not be rejected."""
    cond = compile_condition("0 < x < 10")
    assert cond.predicate({"x": 5}) is True
    assert cond.predicate({"x": 11}) is False
