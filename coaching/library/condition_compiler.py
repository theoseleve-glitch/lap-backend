"""T183 — compile rule condition strings into safe predicates.

Conditions come from YAML in our own repo, but `eval()` is still
unsafe — `().__class__.__bases__[0].__subclasses__()` is the
classic restricted-eval escape that reaches `subprocess`. The
compiler walks the parsed AST and rejects every node outside the
arithmetic + comparison whitelist, so the worst a malformed rule
can do is reference an unknown variable.

`CompiledCondition.predicate(ctx)` returns bool.
`CompiledCondition.referenced_names` lists every variable the
expression reads — T184 (feature namespace) and T185 (runner)
use this to wire the right context.
"""

import ast
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Whitelist of comparison operators that may appear in conditions.
_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}


class ConditionCompileError(Exception):
    """Raised when a condition string contains disallowed syntax."""


@dataclass(frozen=True)
class CompiledCondition:
    predicate: Callable[[dict[str, Any]], bool]
    referenced_names: frozenset[str]


def compile_condition(src: str) -> CompiledCondition:
    """Parse `src` as a Python expression, walk the AST through a
    whitelist, and return a callable predicate."""
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ConditionCompileError(
            f"invalid expression syntax: {src!r}"
        ) from exc

    names: set[str] = set()
    _validate(tree.body, names)

    def predicate(ctx: dict[str, Any]) -> bool:
        return bool(_eval(tree.body, ctx))

    return CompiledCondition(
        predicate=predicate, referenced_names=frozenset(names)
    )


def _validate(node: ast.AST, names: set[str]) -> None:
    """Walk the tree and reject anything outside the whitelist.

    Collects variable names as a side effect — exposing them via
    `referenced_names` saves callers a second pass.
    """
    if isinstance(node, ast.Name):
        names.add(node.id)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return
        raise ConditionCompileError(
            f"unsupported constant type: {type(node.value).__name__}"
        )
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMP_OPS:
                raise ConditionCompileError(
                    f"unsupported comparison operator: {type(op).__name__}"
                )
        _validate(node.left, names)
        for cmp in node.comparators:
            _validate(cmp, names)
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ConditionCompileError(
                f"unsupported binary operator: {type(node.op).__name__}"
            )
        _validate(node.left, names)
        _validate(node.right, names)
        return
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ConditionCompileError(
                f"unsupported unary operator: {type(node.op).__name__}"
            )
        _validate(node.operand, names)
        return
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ConditionCompileError(
                f"unsupported bool operator: {type(node.op).__name__}"
            )
        for v in node.values:
            _validate(v, names)
        return
    raise ConditionCompileError(
        f"disallowed expression node: {type(node).__name__}"
    )


def _eval(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise KeyError(node.id)
        return ctx[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, right_node in zip(
            node.ops, node.comparators, strict=True
        ):
            right = _eval(right_node, ctx)
            if not _CMP_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        return _BIN_OPS[type(node.op)](
            _eval(node.left, ctx), _eval(node.right, ctx)
        )
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_eval(node.operand, ctx))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval(v, ctx) for v in node.values)
        return any(_eval(v, ctx) for v in node.values)
    # _validate would have rejected this — defensive.
    raise ConditionCompileError(
        f"unreachable: {type(node).__name__}"
    )
