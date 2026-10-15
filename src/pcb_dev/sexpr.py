"""Bounded KiCad S-expression data with explicit quoted-string atoms.

Numbers and enums remain bare lexical atoms; quoted strings retain their class
even when their values look like numbers or enums. Whitespace and comments are
canonicalized on output. No token is evaluated, imported, or treated as code.
"""

from __future__ import annotations

import math
from typing import Any


class SExprError(ValueError):
    """Malformed, ambiguous, or excessively large S-expression data."""


class Quoted(str):
    """A string atom that must remain quoted when written to a KiCad file."""


MAX_BYTES = 32 * 1024 * 1024
MAX_TOKENS = 1_000_000
MAX_DEPTH = 128
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def _text_size(value: str) -> int:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise SExprError("S-expression contains an unpaired Unicode surrogate") from exc
    if size > MAX_BYTES:
        raise SExprError(f"S-expression exceeds {MAX_BYTES} bytes")
    return size


def _control(char: str) -> bool:
    return ord(char) < 32 and char not in "\n\r\t"


def parse_quoted(text: str) -> list:
    """Parse one balanced data expression and preserve lexical quotation.

    Recognized quoted escapes are backslash, quote, newline, carriage return
    and tab. Unknown escapes are rejected instead of silently corrupting paths
    or properties. Bare tokens remain strings, including numeric tokens.
    """
    if type(text) is not str:
        raise SExprError("S-expression input must be text")
    _text_size(text)
    stack: list[list] = []
    roots: list[list] = []
    index = tokens = 0
    while index < len(text):
        char = text[index]
        if _control(char):
            raise SExprError("control characters are not permitted in S-expressions")
        if char.isspace():
            index += 1
            continue
        if char == ";":
            end = text.find("\n", index)
            index = len(text) if end == -1 else end + 1
            continue
        tokens += 1
        if tokens > MAX_TOKENS:
            raise SExprError(f"S-expression exceeds {MAX_TOKENS} tokens")
        if char == "(":
            item: list = []
            (stack[-1] if stack else roots).append(item)
            stack.append(item)
            if len(stack) > MAX_DEPTH:
                raise SExprError(f"S-expression nesting exceeds {MAX_DEPTH} levels")
            index += 1
        elif char == ")":
            if not stack:
                raise SExprError("unbalanced S-expression parentheses")
            stack.pop()
            index += 1
        else:
            if not stack:
                raise SExprError("unexpected token outside S-expression root")
            if char == '"':
                index += 1
                value = []
                while index < len(text) and text[index] != '"':
                    char = text[index]
                    if _control(char):
                        raise SExprError("control character in quoted string")
                    if char == "\\":
                        index += 1
                        if index == len(text):
                            raise SExprError("truncated quoted escape")
                        if text[index] not in _ESCAPES:
                            raise SExprError(f"unsupported quoted escape: \\{text[index]}")
                        char = _ESCAPES[text[index]]
                    value.append(char)
                    index += 1
                if index == len(text):
                    raise SExprError("unterminated quoted string")
                index += 1
                if index < len(text) and not text[index].isspace() and text[index] not in "();":
                    raise SExprError("quoted atoms require a token separator")
                atom = Quoted("".join(value))
            else:
                end = index
                while end < len(text) and not text[end].isspace() and text[end] not in "();":
                    if text[end] in '"\\' or _control(text[end]):
                        raise SExprError("invalid character in bare atom; quote string data explicitly")
                    end += 1
                atom, index = text[index:end], end
            stack[-1].append(atom)
    if stack or len(roots) != 1:
        raise SExprError("S-expression must contain one balanced root expression")
    return roots[0]


def _quoted(value: str) -> str:
    if any(_control(char) for char in value):
        raise SExprError("unsupported control character in quoted atom")
    _text_size(value)
    encoded = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"' + encoded + '"'


def dumps(tree: list, *, indent: int = 2) -> str:
    """Write deterministic data, refusing unsafe unquoted strings and cycles.

    Programmatically created finite int/float atoms are accepted as a convenience;
    parsing always returns lexical strings. Booleans and arbitrary objects are
    rejected rather than converted to potentially meaningful KiCad tokens.
    """
    if type(tree) is not list:
        raise SExprError("S-expression root must be a list")
    if type(indent) is not int or not 0 <= indent <= 8:
        raise SExprError("indent must be an integer from zero to eight")
    active: set[int] = set()
    tokens = 0

    def render(value: Any, depth: int) -> str:
        nonlocal tokens
        tokens += 2 if type(value) is list else 1
        if tokens > MAX_TOKENS:
            raise SExprError(f"S-expression exceeds {MAX_TOKENS} tokens")
        if type(value) is list:
            if depth >= MAX_DEPTH:
                raise SExprError(f"S-expression nesting exceeds {MAX_DEPTH} levels")
            if id(value) in active:
                raise SExprError("cyclic S-expression lists are not permitted")
            active.add(id(value))
            items = [render(item, depth + 1) for item in value]
            active.remove(id(value))
            if not items:
                return "()"
            if not any(type(item) is list for item in value):
                return "(" + " ".join(items) + ")"
            padding = " " * (indent * (depth + 1))
            text = "(" + items[0]
            for original, item in zip(value[1:], items[1:]):
                text += ("\n" + padding if type(original) is list else " ") + item
            return text + ")"
        if isinstance(value, Quoted):
            return _quoted(str(value))
        if type(value) is str:
            if not value or any(char.isspace() or char in '()";\\' or _control(char) for char in value):
                raise SExprError("unsafe bare atom; use Quoted for string data")
            _text_size(value)
            return value
        if type(value) is int:
            try:
                return str(value)
            except ValueError as exc:
                raise SExprError("integer atom is too large") from exc
        if type(value) is float and math.isfinite(value):
            return repr(value)
        raise SExprError(f"unsupported S-expression atom type: {type(value).__name__}")

    encoded = render(tree, 0)
    _text_size(encoded)
    return encoded
