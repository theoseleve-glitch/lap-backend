"""T203 — coaching rule compiler: YAML/RuleSpec → portable JSON.

The compiler emits a JSON tree any client (Rust desktop, JS
mobile, Python edge) can evaluate without re-implementing
Python's AST. Every detection condition becomes a tagged tree of
five node types — `var`, `const`, `binop`, `unaryop`, `compare`,
`boolop` — plus a `format_version` envelope so clients can pin
their evaluator and the server can ship breaking changes
deliberately.

Per-rule output mirrors the YAML shape (id/version/category/...)
but replaces `detection.condition` (a string) with
`detection.condition_ast` (the JSON tree). Everything else
serializes through Pydantic's JSON mode — strings, numbers,
nested models — so clients render explanation templates and
prescription directly.
"""

import json
from pathlib import Path

import pytest
import yaml

from coaching.library.compiler import (
    FORMAT_VERSION,
    CompileError,
    compile_condition_to_json,
    compile_library,
    compile_rule,
)
from coaching.library.rule_loader import load_rules
from coaching.library.rule_schema import RuleSpec

LIBRARY_DIR = (
    Path(__file__).resolve().parents[1]
    / "coaching"
    / "library"
    / "rules"
)


# ── condition compilation ──────────────────────────────────────


def test_compile_variable_emits_var_node() -> None:
    assert compile_condition_to_json("apex_speed") == {
        "type": "var", "name": "apex_speed",
    }


def test_compile_numeric_constant_emits_const_node() -> None:
    assert compile_condition_to_json("0.93") == {
        "type": "const", "value": 0.93,
    }


def test_compile_boolean_constant_emits_const_node() -> None:
    assert compile_condition_to_json("True") == {
        "type": "const", "value": True,
    }


def test_compile_simple_comparison_emits_compare_node() -> None:
    """`a < b` — single op, two operands. Must be a `compare`
    node (not split into nested binops) so clients evaluate it
    with one operator."""
    assert compile_condition_to_json("a < b") == {
        "type": "compare",
        "ops": ["lt"],
        "operands": [
            {"type": "var", "name": "a"},
            {"type": "var", "name": "b"},
        ],
    }


def test_compile_chained_comparison_preserves_order() -> None:
    """`a < b < c` — Python evaluates this as `a < b and b < c`.
    The compare node carries N+1 operands and N ops; client
    evaluator pairs them up in order."""
    result = compile_condition_to_json("a < b < c")
    assert result["type"] == "compare"
    assert result["ops"] == ["lt", "lt"]
    assert [op["name"] for op in result["operands"]] == ["a", "b", "c"]


def test_compile_binary_arithmetic_emits_binop_node() -> None:
    assert compile_condition_to_json("a * 0.93") == {
        "type": "binop",
        "op": "mul",
        "left": {"type": "var", "name": "a"},
        "right": {"type": "const", "value": 0.93},
    }


def test_compile_realistic_rule_condition_round_trips() -> None:
    """Mirrors the actual T188 detection condition — the most
    common shape in the library: `var < var * const`."""
    result = compile_condition_to_json(
        "apex_speed < reference_apex_speed * 0.93"
    )
    assert result == {
        "type": "compare",
        "ops": ["lt"],
        "operands": [
            {"type": "var", "name": "apex_speed"},
            {
                "type": "binop",
                "op": "mul",
                "left": {
                    "type": "var", "name": "reference_apex_speed",
                },
                "right": {"type": "const", "value": 0.93},
            },
        ],
    }


def test_compile_unary_negation_emits_unaryop_node() -> None:
    assert compile_condition_to_json("-x") == {
        "type": "unaryop",
        "op": "neg",
        "operand": {"type": "var", "name": "x"},
    }


def test_compile_boolean_and_emits_boolop_node() -> None:
    """Multi-operand bool ops survive flat: `a and b and c`
    becomes one node with three values, not nested 2-op trees."""
    result = compile_condition_to_json("a and b and c")
    assert result["type"] == "boolop"
    assert result["op"] == "and"
    assert [v["name"] for v in result["values"]] == ["a", "b", "c"]


def test_compile_disallowed_call_raises_compile_error() -> None:
    """Same posture as the runtime compiler — function calls,
    attribute access, subscripts all rejected. Defends the AST
    even though the YAML is in-tree, because YAML is also where
    a future operator might paste an unreviewed snippet."""
    with pytest.raises(CompileError):
        compile_condition_to_json("foo()")


def test_compile_disallowed_attribute_raises_compile_error() -> None:
    with pytest.raises(CompileError):
        compile_condition_to_json("obj.attr")


def test_compile_invalid_syntax_raises_compile_error() -> None:
    with pytest.raises(CompileError):
        compile_condition_to_json("a <")


def test_compile_condition_output_is_json_serializable() -> None:
    """Bytes/sets/etc would break the bundle endpoint silently.
    Cheap to assert at compile time so the bundle endpoint can
    trust that any compiled rule serializes."""
    result = compile_condition_to_json(
        "apex_speed < reference_apex_speed * 0.93"
    )
    json.dumps(result)  # raises if any node carries non-JSON types


# ── rule-level compilation ─────────────────────────────────────


def _t188() -> RuleSpec:
    """Helper: load T188's RuleSpec via the production loader so
    the test exercises the same parse path as runtime."""
    rules = load_rules(LIBRARY_DIR / "braking")
    for r in rules:
        if r.id == "br.late_braking.apex_speed_loss":
            return r
    raise RuntimeError("T188 missing from library")


def test_compile_rule_carries_top_level_metadata() -> None:
    rule = _t188()
    out = compile_rule(rule)
    assert out["id"] == "br.late_braking.apex_speed_loss"
    assert out["version"] == 1
    assert out["category"] == "technique"
    assert out["subcategory"] == "late_braking"
    assert out["difficulty"] == "beginner"


def test_compile_rule_replaces_condition_string_with_ast() -> None:
    """The whole point of compilation: clients receive a JSON
    tree, not a Python expression string. Asserts both that the
    AST is present AND that the source string is gone."""
    rule = _t188()
    out = compile_rule(rule)
    assert "condition_ast" in out["detection"]
    assert "condition" not in out["detection"]
    assert out["detection"]["condition_ast"]["type"] == "compare"


def test_compile_rule_preserves_detection_signal_and_context() -> None:
    """Signal name and detection context (channels, corner
    priority) ride along unchanged — clients use them to decide
    whether a session has the channels needed to evaluate the
    rule before running it."""
    rule = _t188()
    out = compile_rule(rule)
    det = out["detection"]
    assert det["signal"] == "apex_speed"
    assert det["context"]["min_corner_priority"] == "medium"
    assert "brake" in det["context"]["must_have_channels"]


def test_compile_rule_preserves_explanation_templates() -> None:
    """All three difficulty branches must survive — clients
    re-evaluate per session based on driver level."""
    rule = _t188()
    out = compile_rule(rule)
    expl = out["explanation_template"]
    assert "{{delta_kmh}}" in expl["beginner"]
    assert "{{delta_kmh}}" in expl["intermediate"]
    assert "{{release_ms}}" in expl["advanced"]


def test_compile_rule_preserves_prescription() -> None:
    rule = _t188()
    out = compile_rule(rule)
    pres = out["prescription"]
    assert pres["drill_template_id"] == "drill.trail_brake_later_release"
    assert pres["expected_gain_kmh"] == "2-5"
    assert pres["success_condition"]["metric"] == "apex_speed"
    assert pres["success_condition"]["comparison"] == "gte"


def test_compile_rule_omits_transfer_profile_when_none() -> None:
    """If a YAML doesn't declare transfer_profile, the compiled
    output must NOT carry a `null` field — keeps the wire JSON
    clean and lets clients use `'transfer_profile' in rule` as
    the presence check."""
    minimal_yaml = """
id: "test.no_transfer.minimal"
version: 1
category: technique
subcategory: minimal_test
difficulty: beginner
detection:
  signal: x
  condition: "x > 0"
  context:
    must_have_channels: ["brake"]
    min_corner_priority: low
  evidence_required:
    min_occurrences_in_session: 1
    min_sessions_in_window: 1
    window_days: 1
severity_rule: "constant"
explanation_template:
  beginner: "b"
  intermediate: "i"
  advanced: "a"
prescription:
  drill_template_id: "drill.x"
  expected_gain_kmh: "1"
  success_condition:
    metric: "x"
    comparison: "gte"
    target_pct: 1.0
    measurement_window: "next_3_sessions"
"""
    rule = RuleSpec.model_validate(yaml.safe_load(minimal_yaml))
    out = compile_rule(rule)
    assert "transfer_profile" not in out


def test_compile_rule_includes_transfer_profile_when_present() -> None:
    rule = _t188()
    assert rule.transfer_profile is not None  # T188 declares it
    out = compile_rule(rule)
    assert "transfer_profile" in out
    assert (
        out["transfer_profile"]["sim_vs_real"]["likely_transfers"]
        is True
    )


def test_compile_rule_output_is_json_serializable() -> None:
    rule = _t188()
    out = compile_rule(rule)
    json.dumps(out)  # raises if any field carries non-JSON types


# ── library-level compilation ──────────────────────────────────


def test_compile_library_wraps_rules_in_versioned_envelope() -> None:
    """`format_version` lets clients pin their evaluator. If
    we add a new node type or rename an op, we bump this
    constant — old clients keep using the old shape they were
    pinned to until they update."""
    rules = load_rules(LIBRARY_DIR)
    bundle = compile_library(rules)
    assert bundle["format_version"] == FORMAT_VERSION
    assert isinstance(bundle["rules"], list)


def test_compile_library_emits_one_entry_per_rule() -> None:
    rules = load_rules(LIBRARY_DIR)
    bundle = compile_library(rules)
    assert len(bundle["rules"]) == len(rules)
    bundle_ids = {r["id"] for r in bundle["rules"]}
    source_ids = {r.id for r in rules}
    assert bundle_ids == source_ids


def test_compile_library_is_json_serializable_end_to_end() -> None:
    """The whole bundle round-trips through json.dumps/loads
    with no loss. This is the wire-format guarantee the bundle
    endpoint depends on."""
    rules = load_rules(LIBRARY_DIR)
    bundle = compile_library(rules)
    serialized = json.dumps(bundle)
    parsed = json.loads(serialized)
    assert parsed == bundle


def test_format_version_is_a_positive_integer() -> None:
    """Format versions are integers, not semver — clients
    compare with ==, not with semver ranges. T204 will document
    the bump policy."""
    assert isinstance(FORMAT_VERSION, int)
    assert FORMAT_VERSION >= 1
