"""Bounded YAML-subset parser for source conformance tooling.

The installed runtime venv does not carry PyYAML (it is a dev-only
dependency), yet ``plugin_policy.py`` must still audit plugin manifests on
clean installs. This module parses exactly the constructs Dispatch-owned
manifests use -- block mappings, block sequences, single-line flow
collections (``{...}`` / ``[...]``), plain/quoted scalars, and comments --
and rejects everything else by raising ``ValueError``.

Design rules:

* Fail closed. Constructs outside the subset (anchors, aliases, tags, block
  scalars, multi-line flow collections, tabs in indentation, duplicate keys,
  document markers beyond one leading ``---``) are errors, never guesses.
* When PyYAML is importable, callers should prefer it; this parser exists so
  the audit degrades to a narrower-but-correct check instead of crashing.
* Deterministic: identical input always yields identical output or the same
  rejection. No environment sensitivity.
"""
from __future__ import annotations

from typing import Any, BinaryIO

__all__ = ["load_subset", "parse_subset"]

_MAX_LINE_LENGTH = 4_096
_MAX_LINES = 10_000
_MAX_DEPTH = 32


def load_subset(stream: BinaryIO | Any) -> Any:
    """Mirror ``yaml.safe_load(stream)`` for file objects."""
    data = stream.read()
    if isinstance(data, bytes):
        text = data.decode("utf-8")
    else:
        text = data
    return parse_subset(text)


def parse_subset(text: str) -> Any:
    lines = _prepare(text)
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        number = lines[index][2]
        raise ValueError(f"line {number}: unexpected content outside the supported subset")
    return value


def _prepare(text: str) -> list[tuple[int, str, int]]:
    """Return (indent, stripped content, line number) tuples, comments removed."""
    if "\t" in text and any(
        line.startswith("\t") or (line[: len(line) - len(line.lstrip())].startswith("\t"))
        for line in text.splitlines()
    ):
        raise ValueError("tab characters are not allowed in indentation")
    raw_lines = text.splitlines()
    if len(raw_lines) > _MAX_LINES:
        raise ValueError("document exceeds the bounded line count")
    prepared: list[tuple[int, str, int]] = []
    started = False
    for number, raw in enumerate(raw_lines, start=1):
        if len(raw) > _MAX_LINE_LENGTH:
            raise ValueError(f"line {number}: line exceeds the bounded length")
        content = _strip_comment(raw)
        stripped = content.strip()
        if not started:
            if not stripped:
                continue
            if stripped == "---":
                started = True
                continue
            started = True
        if not stripped:
            continue
        if stripped in {"...", "---"}:
            raise ValueError(f"line {number}: document markers are outside the supported subset")
        indent = len(content) - len(content.lstrip(" "))
        prepared.append((indent, stripped, number))
    return prepared


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for position, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if position == 0 or line[position - 1] in {" ", "\t"}:
                return line[:position]
    return line


def _parse_block(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        raise ValueError("unexpected end of document")
    if lines[index][1].startswith("- ") or lines[index][1] == "-":
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    depth = 0
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(f"line {number}: unexpected indentation")
        if content.startswith("- ") or (content == "-" and line_indent == indent):
            break
        if line_indent == indent and content.startswith("- "):
            break
        key, separator, remainder = _split_key(content, number)
        if key in result:
            raise ValueError(f"line {number}: duplicate key {key!r}")
        depth += 1
        if depth > _MAX_DEPTH:
            raise ValueError(f"line {number}: mapping exceeds the bounded depth")
        remainder = remainder.strip()
        if separator and remainder:
            result[key] = _parse_value(remainder, number)
            index += 1
            continue
        index += 1
        child, index = _parse_child(lines, index, indent, number, key)
        result[key] = child
    return result, index


def _parse_child(
    lines: list[tuple[int, str, int]], index: int, indent: int, number: int, key: str
) -> tuple[Any, int]:
    if index < len(lines):
        child_indent, child_content, _ = lines[index]
        if child_indent > indent and not child_content.startswith("- "):
            return _parse_block(lines, index, child_indent)
        if child_indent > indent and child_content.startswith("- "):
            # Sequence items may sit at the parent's indent + 2 (or any deeper
            # indent); they belong to this key.
            return _parse_sequence(lines, index, child_indent)
        if child_indent == indent and (child_content.startswith("- ") or child_content == "-"):
            return _parse_sequence(lines, index, indent)
    return None, index


def _parse_sequence(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent != indent or not (content.startswith("- ") or content == "-"):
            if line_indent > indent:
                raise ValueError(f"line {number}: unexpected indentation in sequence")
            break
        item = content[2:].strip() if content.startswith("- ") else ""
        if not item and len(content) >= 2:
            item = content[1:].strip()  # "-x" glued form is invalid; keep empty
            item = ""
        depth_guard = len(result) + 1
        if depth_guard > _MAX_DEPTH:
            raise ValueError(f"line {number}: sequence exceeds the bounded length")
        if not item:
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                child, index = _parse_block(lines, index, lines[index][0])
                result.append(child)
            else:
                result.append(None)
            continue
        if item[0] in {"{", "["}:
            value, rest = _parse_flow(item, number)
            if rest.strip():
                raise ValueError(f"line {number}: trailing content after flow collection")
            result.append(value)
            index += 1
            continue
        separator = _has_key_separator(item)
        if separator:
            # Compact nested mapping on the dash line ("- a: 1") followed by
            # sibling keys at the content indent. Rewrite the dash line as an
            # indented mapping entry and parse the block from there. Sibling
            # lines belong to this mapping only until the next dash line at
            # the same indent (the following sequence item).
            rewritten = (indent + 2, " " * 2 + item, number)
            sibling: list[tuple[int, str, int]] = []
            for line in lines[index + 1 :]:
                # Any line at or below the sequence indent belongs to a
                # following sequence item or to the parent mapping.
                if line[0] <= indent:
                    break
                sibling.append(line)
            virtual = lines[:index] + [rewritten] + sibling
            mapping, next_index = _parse_mapping(virtual, index, indent + 2)
            consumed = next_index - index
            result.append(mapping)
            index += consumed
            continue
        result.append(_parse_value(item, number))
        index += 1
    return result, index


def _has_key_separator(content: str) -> bool:
    """Return whether content is a 'key: value' entry, without raising."""
    quote: str | None = None
    for position, char in enumerate(content):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == ":" and content[position + 1 : position + 2] in {"", " "}:
            return True
    return False


def _split_key(content: str, number: int) -> tuple[str, bool, str]:
    quote: str | None = None
    for position, char in enumerate(content):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == ":":
            after = content[position + 1 : position + 2]
            if after in {"", " "}:
                key = content[:position].strip()
                if not key:
                    raise ValueError(f"line {number}: empty mapping key")
                if quote is not None:
                    raise ValueError(f"line {number}: unterminated quote in key")
                if key[0] in {"'", '"'}:
                    key = _unquote(key, number)
                return key, True, content[position + 1 :]
    raise ValueError(f"line {number}: expected 'key: value' mapping entry")


def _parse_value(text: str, number: int) -> Any:
    text = text.strip()
    if not text:
        return None
    if text[0] in {"{", "["}:
        value, rest = _parse_flow(text, number)
        if rest.strip():
            raise ValueError(f"line {number}: trailing content after flow collection")
        return value
    first = text[0]
    if first in {"&", "*", "!"}:
        raise ValueError(f"line {number}: anchors, aliases, and tags are outside the supported subset")
    if first in {"|", ">"}:
        raise ValueError(f"line {number}: block scalars are outside the supported subset")
    return _parse_scalar(text, number)


def _parse_scalar(text: str, number: int) -> Any:
    if text and text[0] in {"'", '"'}:
        return _unquote(text, number)
    lowered = text.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text, 10)
    except ValueError:
        return text


def _unquote(text: str, number: int) -> str:
    if len(text) < 2:
        raise ValueError(f"line {number}: invalid quoted scalar")
    quote = text[0]
    if text[-1] != quote:
        raise ValueError(f"line {number}: unterminated quoted scalar")
    body = text[1:-1]
    if quote == "'":
        if "'" in body.replace("''", ""):
            raise ValueError(f"line {number}: unterminated single-quoted scalar")
        return body.replace("''", "'")
    if '"' in body.replace('\\"', ""):
        raise ValueError(f"line {number}: unterminated double-quoted scalar")
    try:
        return bytes(body, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError as exc:
        raise ValueError(f"line {number}: invalid escape in double-quoted scalar") from exc


def _parse_flow(text: str, number: int) -> tuple[Any, str]:
    opening = text[0]
    closing = "]" if opening == "[" else "}"
    index = 1
    items: list[Any] = []
    mapping: dict[Any, Any] = {}
    depth = 1
    while index < len(text):
        char = text[index]
        if char in {" ", "\t"}:
            index += 1
            continue
        if char == closing:
            depth -= 1
            if depth == 0:
                if opening == "[":
                    return items, text[index + 1 :]
                return mapping, text[index + 1 :]
            raise ValueError(f"line {number}: unbalanced flow collection")
        if char in {"}", "]"}:
            raise ValueError(f"line {number}: unbalanced flow collection")
        if char == ",":
            index += 1
            continue
        value, index = _parse_flow_item(text, index, number)
        if opening == "[":
            items.append(value)
            continue
        if not isinstance(value, tuple):
            raise ValueError(f"line {number}: flow mapping entries must be 'key: value'")
        key, item_value = value
        if key in mapping:
            raise ValueError(f"line {number}: duplicate flow mapping key {key!r}")
        mapping[key] = item_value
    raise ValueError(f"line {number}: unterminated flow collection")


def _parse_flow_item(text: str, index: int, number: int) -> tuple[Any, int]:
    start = index
    depth = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in {"[", "{"}:
            depth += 1
            index += 1
            continue
        if char in {"]", "}"}:
            if depth == 0:
                break
            depth -= 1
            index += 1
            continue
        if char == "," and depth == 0:
            break
        if char == ":" and depth == 0:
            after = text[index + 1 : index + 2]
            if after in {"", " ", ",", "]", "}"}:
                key_text = text[start:index].strip()
                key = _parse_scalar(key_text, number)
                if not isinstance(key, (str, int, float, bool)) and key is not None:
                    raise ValueError(f"line {number}: unsupported flow mapping key type")
                rest = text[index + 1 :].lstrip()
                if rest.startswith(",") or rest.startswith("}") or rest.startswith("]"):
                    return (key, None), index + 1
                value, next_index = _parse_flow_item(text, index + 1, number)
                return (key, value), next_index
        index += 1
    segment = text[start:index].strip()
    if not segment:
        raise ValueError(f"line {number}: empty flow collection entry")
    if segment[0] in {"{", "["}:
        value, rest = _parse_flow(segment, number)
        if rest.strip():
            raise ValueError(f"line {number}: trailing content after nested flow collection")
        return value, index
    return _parse_value(segment, number), index
