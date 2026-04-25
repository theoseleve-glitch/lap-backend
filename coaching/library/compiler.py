"""T203 — coaching rule compiler: RuleSpec → portable JSON.

Compiles every YAML rule into a self-contained JSON document
clients can evaluate without re-implementing Python's expression
parser. The detection condition is the only field that needs
transformation — everything else (id, templates, prescription,
context) ships through Pydantic's JSON mode unchanged.

Format spec (FORMAT_VERSION = 1):

    {
      "format_version": 1,
      "rules": [
        {
          "id": str,
          "version": int,
          "category": "technique" | "setup" | "strategy",
          "subcategory": str,
          "difficulty": "beginner" | "intermediate" | "advanced",
          "detection": {
            "signal": str,
            "condition_ast": <Node>,        # tagged JSON tree
            "context": {
              "must_have_channels": [str, ...],
              "min_corner_priority": "low" | "medium" | "high"
            },
            "evidence_required": {
              "min_occurrences_in_session": int,
              "min_sessions_in_window": int,
              "window_days": int
            }
          },
          "severity_rule": str,             # opaque, client renders
          "explanation_template": {
            "beginner": str,                # mustache {{var}}
            "intermediate": str,
            "advanced": str
          },
          "prescription": {
            "drill_template_id": str,
            "expected_gain_kmh": str,
            "success_condition": {
              "metric": str,
              "comparison": "gte"|"gt"|"lte"|"lt"|"eq",
              "target_pct": float,
              "measurement_window": str
            }
          },
          "transfer_profile"?: {            # optional — omit when absent
            "sim_vs_real": {
              "likely_transfers": bool,
              "caveats": [str, ...]
            }
          }
        },
        ...
      ]
    }

Node grammar (every condition_ast is one of these):

    {"type": "var",     "name": str}
    {"type": "const",   "value": int|float|bool}
    {"type": "compare", "ops": [str, ...], "operands": [<Node>, ...]}
    {"type": "binop",   "op": str, "left": <Node>, "right": <Node>}
    {"type": "unaryop", "op": str, "operand": <Node>}
    {"type": "boolop",  "op": "and"|"or", "values": [<Node>, ...]}

Operator names mirror the Python ast classes lowercase:
    compare: lt, lte, gt, gte, eq, ne
    binop:   add, sub, mul, div
    unaryop: neg, pos, not

Bumping FORMAT_VERSION is breaking — clients pin and the bundle
endpoint (T204) ships the matching version. Any new node type
or renamed op is a bump.
"""

import ast
from collections.abc import Iterable
from typing import Any

from coaching.library.rule_schema import RuleSpec

FORMAT_VERSION = 1


class CompileError(Exception):
    """Raised when a condition contains syntax outside the
    portable AST whitelist."""


_CMP_NAMES: dict[type[ast.cmpop], str] = {
    ast.Lt: "lt",
    ast.LtE: "lte",
    ast.Gt: "gt",
    ast.GtE: "gte",
    ast.Eq: "eq",
    ast.NotEq: "ne",
}

_BIN_NAMES: dict[type[ast.operator], str] = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Div: "div",
}

_UNARY_NAMES: dict[type[ast.unaryop], str] = {
    ast.USub: "neg",
    ast.UAdd: "pos",
    ast.Not: "not",
}


def compile_condition_to_json(src: str) -> dict[str, Any]:
    """Parse `src` and walk the AST, emitting a JSON-serializable
    tagged tree. Same whitelist as the runtime compiler — same
    rejections, different output. Raises `CompileError` on any
    disallowed node or syntax error."""
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise CompileError(
            f"invalid expression syntax: {src!r}"
        ) from exc
    return _node_to_json(tree.body)


def _node_to_json(node: ast.AST) -> dict[str, Any]:
    if isinstance(node, ast.Name):
        return {"type": "var", "name": node.id}
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return {"type": "const", "value": node.value}
        raise CompileError(
            f"unsupported constant type: {type(node.value).__name__}"
        )
    if isinstance(node, ast.Compare):
        op_names: list[str] = []
        for op in node.ops:
            name = _CMP_NAMES.get(type(op))
            if name is None:
                raise CompileError(
                    f"unsupported comparison operator: "
                    f"{type(op).__name__}"
                )
            op_names.append(name)
        operands = [_node_to_json(node.left)]
        operands.extend(_node_to_json(c) for c in node.comparators)
        return {
            "type": "compare", "ops": op_names, "operands": operands,
        }
    if isinstance(node, ast.BinOp):
        op_name = _BIN_NAMES.get(type(node.op))
        if op_name is None:
            raise CompileError(
                f"unsupported binary operator: {type(node.op).__name__}"
            )
        return {
            "type": "binop",
            "op": op_name,
            "left": _node_to_json(node.left),
            "right": _node_to_json(node.right),
        }
    if isinstance(node, ast.UnaryOp):
        op_name = _UNARY_NAMES.get(type(node.op))
        if op_name is None:
            raise CompileError(
                f"unsupported unary operator: {type(node.op).__name__}"
            )
        return {
            "type": "unaryop",
            "op": op_name,
            "operand": _node_to_json(node.operand),
        }
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            op_name = "and"
        elif isinstance(node.op, ast.Or):
            op_name = "or"
        else:
            raise CompileError(
                f"unsupported bool operator: {type(node.op).__name__}"
            )
        return {
            "type": "boolop",
            "op": op_name,
            "values": [_node_to_json(v) for v in node.values],
        }
    raise CompileError(
        f"disallowed expression node: {type(node).__name__}"
    )


def compile_rule(rule: RuleSpec) -> dict[str, Any]:
    """Compile one RuleSpec into the portable JSON shape. The
    detection condition string is replaced by `condition_ast`;
    every other field passes through Pydantic's JSON mode."""
    detection_dump = rule.detection.model_dump(mode="json")
    detection_dump.pop("condition")
    detection_dump["condition_ast"] = compile_condition_to_json(
        rule.detection.condition
    )
    out: dict[str, Any] = {
        "id": rule.id,
        "version": rule.version,
        "category": rule.category,
        "subcategory": rule.subcategory,
        "difficulty": rule.difficulty,
        "detection": detection_dump,
        "severity_rule": rule.severity_rule,
        "explanation_template": rule.explanation_template.model_dump(
            mode="json"
        ),
        "prescription": rule.prescription.model_dump(mode="json"),
    }
    if rule.transfer_profile is not None:
        out["transfer_profile"] = rule.transfer_profile.model_dump(
            mode="json"
        )
    return out


def compile_library(rules: Iterable[RuleSpec]) -> dict[str, Any]:
    """Compile a collection of RuleSpecs into the versioned
    bundle envelope the T204 endpoint will ship."""
    return {
        "format_version": FORMAT_VERSION,
        "rules": [compile_rule(r) for r in rules],
    }
