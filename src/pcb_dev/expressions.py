"""Bounded numeric expression evaluation without eval, exec or Python calls.

Equations accept numbers, local/inherited parameter names, ``+ - * / // % **``,
``abs``, ``min``, ``max``, ``round`` and ``sqrt``.  ``pi`` and ``e`` are built-in
constants unless explicitly supplied as parameters.  Constraints additionally
accept numeric comparisons, chained comparisons, and ``and/or/not`` on booleans.
Attributes, indexing, comprehensions, imports, strings and arbitrary calls are
always rejected, including in a branch that would not be evaluated.
"""

from __future__ import annotations

import ast
import copy
import heapq
import math
import operator
from typing import Any, Mapping

from .model import Design, ModelError


class ExpressionError(ModelError):
    """Unsafe, undefined, cyclic, or non-finite numeric expression."""


MAX_EXPRESSION_LENGTH = 4096
MAX_EXPRESSION_NODES = 256
MAX_EXPRESSION_DEPTH = 32
MAX_EXPONENT = 32
_FUNCTIONS = {"abs", "min", "max", "round", "sqrt"}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_COMPARE = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}


def _numeric(value: Any, context: str) -> int | float:
    if type(value) not in (int, float):
        raise ExpressionError(f"{context}: expected a number; booleans and other values are not numeric")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (type(value) is int and value.bit_length() > 1024):
        raise ExpressionError(f"{context}: result must be a finite, bounded number")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ExpressionError("logical operators require comparisons, not numeric truthiness")
    return value


def _parse(expression: str, *, constraint: bool) -> ast.Expression:
    if type(expression) is not str or not expression.strip():
        raise ExpressionError("expression must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"expression exceeds {MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ExpressionError(f"invalid expression syntax: {exc}") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_EXPRESSION_NODES:
        raise ExpressionError(f"expression exceeds {MAX_EXPRESSION_NODES} syntax nodes")
    _validate(tree.body, constraint=constraint, depth=0)
    return tree


def _validate(node: ast.AST, *, constraint: bool, depth: int) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise ExpressionError(f"expression nesting exceeds {MAX_EXPRESSION_DEPTH}")
    children: list[ast.AST]
    if isinstance(node, ast.Constant):
        _numeric(node.value, "literal")
        return
    if isinstance(node, ast.Name):
        if node.id.startswith("__"):
            raise ExpressionError("private names are not permitted")
        return
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        children = [node.left, node.right]
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        children = [node.operand]
    elif isinstance(node, ast.UnaryOp) and constraint and isinstance(node.op, ast.Not):
        children = [node.operand]
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ExpressionError("only abs, min, max, round and sqrt calls are permitted")
        if node.keywords:
            raise ExpressionError("function keyword arguments are not permitted")
        argc = len(node.args)
        if ((node.func.id in {"abs", "sqrt"} and argc != 1)
                or (node.func.id == "round" and argc not in (1, 2))
                or (node.func.id in {"min", "max"} and not 1 <= argc <= 16)):
            raise ExpressionError(f"invalid number of arguments for {node.func.id}")
        children = list(node.args)
    elif isinstance(node, ast.Compare) and constraint and all(type(op) in _COMPARE for op in node.ops):
        children = [node.left, *node.comparators]
    elif isinstance(node, ast.BoolOp) and constraint and isinstance(node.op, (ast.And, ast.Or)):
        children = list(node.values)
    else:
        raise ExpressionError(f"unsupported expression syntax: {type(node).__name__}")
    for child in children:
        _validate(child, constraint=constraint, depth=depth + 1)


def _names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Call):
        return set().union(*(_names(arg) for arg in node.args))
    return set().union(*(_names(child) for child in ast.iter_child_nodes(node)))


def referenced_names(expression: str, *, constraint: bool = False) -> set[str]:
    """Return referenced parameter names after validating safe syntax."""
    return _names(_parse(expression, constraint=constraint).body)


def _evaluate(node: ast.AST, parameters: Mapping[str, Any]) -> int | float | bool:
    if isinstance(node, ast.Constant):
        return _numeric(node.value, "literal")
    if isinstance(node, ast.Name):
        if node.id in parameters:
            return _numeric(parameters[node.id], f"parameter {node.id!r}")
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ExpressionError(f"unknown parameter {node.id!r}")
    if isinstance(node, ast.BinOp):
        left = _numeric(_evaluate(node.left, parameters), "left operand")
        right = _numeric(_evaluate(node.right, parameters), "right operand")
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ExpressionError(f"power exponent must be between {-MAX_EXPONENT} and {MAX_EXPONENT}")
        return _numeric(_BINARY[type(node.op)](left, right), "arithmetic")
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, parameters)
        if isinstance(node.op, ast.Not):
            return not _boolean(value)
        value = _numeric(value, "unary operand")
        return _numeric(-value if isinstance(node.op, ast.USub) else value, "unary result")
    if isinstance(node, ast.Call):
        name = node.func.id
        args = [_numeric(_evaluate(arg, parameters), f"{name} argument") for arg in node.args]
        if name == "abs":
            result = abs(args[0])
        elif name == "sqrt":
            result = math.sqrt(args[0])
        elif name == "min":
            result = min(args)
        elif name == "max":
            result = max(args)
        else:  # round
            if len(args) == 2:
                if type(args[1]) is not int or abs(args[1]) > 15:
                    raise ExpressionError("round precision must be an integer between -15 and 15")
                result = round(args[0], args[1])
            else:
                result = round(args[0])
        return _numeric(result, f"{name} result")
    if isinstance(node, ast.Compare):
        left = _numeric(_evaluate(node.left, parameters), "comparison operand")
        for op, right_node in zip(node.ops, node.comparators):
            right = _numeric(_evaluate(right_node, parameters), "comparison operand")
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        for child in node.values:
            value = _boolean(_evaluate(child, parameters))
            if isinstance(node.op, ast.And) and not value:
                return False
            if isinstance(node.op, ast.Or) and value:
                return True
        return isinstance(node.op, ast.And)
    raise ExpressionError(f"unsupported expression syntax: {type(node).__name__}")


def _evaluate_tree(tree: ast.Expression, parameters: Mapping[str, Any]) -> int | float | bool:
    try:
        return _evaluate(tree.body, parameters)
    except ExpressionError:
        raise
    except (ArithmeticError, ValueError, TypeError, RecursionError) as exc:
        raise ExpressionError(f"numeric evaluation failed: {exc}") from exc


def evaluate_expression(expression: str, parameters: Mapping[str, Any]) -> int | float:
    """Evaluate a safe arithmetic expression and require a finite number."""
    return _numeric(_evaluate_tree(_parse(expression, constraint=False), parameters), "equation")


def evaluate_constraint(expression: str, parameters: Mapping[str, Any]) -> bool:
    """Evaluate numeric comparisons and require an actual boolean result."""
    return _boolean(_evaluate_tree(_parse(expression, constraint=True), parameters))


def _resolve_block(path: str, local: Mapping[str, Any], equations: Mapping[str, str],
                   inherited: Mapping[str, Any]) -> dict[str, Any]:
    values = copy.deepcopy(dict(inherited))
    values.update(copy.deepcopy(dict(local)))
    trees: dict[str, ast.Expression] = {}
    dependencies: dict[str, set[str]] = {}
    for target, expression in equations.items():
        try:
            trees[target] = _parse(expression, constraint=False)
            dependencies[target] = _names(trees[target].body) & set(equations)
        except ExpressionError as exc:
            raise ExpressionError(f"{path}.equations.{target}: {exc}") from exc
    remaining = {target: len(deps) for target, deps in dependencies.items()}
    reverse = {target: set() for target in equations}
    for target, deps in dependencies.items():
        for dependency in deps:
            reverse[dependency].add(target)
    ready = [target for target, count in remaining.items() if not count]
    heapq.heapify(ready)
    count = 0
    while ready:
        target = heapq.heappop(ready)
        try:
            values[target] = _numeric(_evaluate_tree(trees[target], values), "equation")
        except ExpressionError as exc:
            raise ExpressionError(f"{path}.equations.{target}: {exc}") from exc
        count += 1
        for dependent in sorted(reverse[target]):
            remaining[dependent] -= 1
            if not remaining[dependent]:
                heapq.heappush(ready, dependent)
    if count != len(equations):
        unresolved = {target for target, degree in remaining.items() if degree}
        target = min(unresolved)
        chain: list[str] = []
        positions: dict[str, int] = {}
        while target not in positions:
            positions[target] = len(chain)
            chain.append(target)
            target = min(dependencies[target] & unresolved)
        cycle = chain[positions[target]:] + [target]
        raise ExpressionError(f"{path}: equation dependency cycle: {' -> '.join(cycle)}")
    return values


def resolve_parameters(design: Design) -> dict[str, dict[str, Any]]:
    """Resolve inherited parameters and each block's equations without mutation.

    Local values override inherited ones.  Equations override local values and
    are evaluated in dependency order, independently of JSON member order.
    A child inherits the parent's resolved outputs.  Explicit block dependencies
    describe build/change impact; equation names address the local scope only.
    """
    from .graph import dependency_order
    dependency_order(design)
    result: dict[str, dict[str, Any]] = {}
    for path, block in design.walk_blocks():
        parent = path.rsplit(".", 1)[0] if "." in path else None
        inherited = result[parent] if parent is not None else {}
        result[path] = _resolve_block(path, block.parameters, block.equations, inherited)
    return result


def evaluate_constraints(
    design: Design, resolved_parameters: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[bool]]:
    """Return per-block constraint results in declaration order."""
    values = resolve_parameters(design) if resolved_parameters is None else resolved_parameters
    results = {}
    for path, block in design.walk_blocks():
        results[path] = []
        for index, constraint in enumerate(block.constraints):
            try:
                results[path].append(evaluate_constraint(constraint.expression, values[path]))
            except ExpressionError as exc:
                raise ExpressionError(f"{path}.constraints[{index}]: {exc}") from exc
    return results
