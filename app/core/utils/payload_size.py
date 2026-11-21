"""Size a request body without holding a second full copy of it.

A ``response.create`` can be tens of megabytes. Measuring it by dumping or
encoding the whole body allocates another copy of that size, which on the
request path multiplies with every concurrent request and retry.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Final

# Large enough that the per-slice overhead is negligible, small enough that a
# slice of a multi-megabyte request costs little temporary memory.
_UTF8_LENGTH_SLICE_CHARS: Final[int] = 1024 * 1024
_COMPACT_ENCODER: Final[json.JSONEncoder] = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))


def utf8_length(text: str) -> int:
    """Return ``len(text.encode("utf-8"))`` without materializing the encoding.

    ``json.dumps(..., ensure_ascii=True)`` output and most request frames are
    pure ASCII, where the UTF-8 length is the character count and
    ``str.isascii`` is a constant-time flag check. Other text is encoded in
    bounded slices, so a large request frame costs at most one slice of
    temporary memory instead of a second full copy. Lone surrogates count as
    their three-byte ``surrogatepass`` encoding instead of raising.
    """
    if text.isascii():
        return len(text)
    return sum(
        len(text[start : start + _UTF8_LENGTH_SLICE_CHARS].encode("utf-8", "surrogatepass"))
        for start in range(0, len(text), _UTF8_LENGTH_SLICE_CHARS)
    )


def compact_json_length(value: object) -> int:
    """Return ``len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))``.

    The output is ASCII, so this is also its UTF-8 byte count. A top-level
    object is measured one member at a time, and a member that is an array one
    element at a time, so the temporary memory is bounded by the largest
    element (one input item) instead of the whole body.
    """
    return compact_json_dumps_within(value, -1)[0]


def compact_json_dumps_within(value: object, max_length: int) -> tuple[int, str | None]:
    """Measure the compact dump of ``value`` and return it if it fits ``max_length``.

    Returns ``(length, text)`` where ``text`` equals
    ``json.dumps(value, ensure_ascii=True, separators=(",", ":"))`` when its
    length is at most ``max_length`` and is ``None`` otherwise. A top-level
    object is encoded one member, and one array element, at a time in a single
    pass: the pieces are joined when the total fits, and dropped as soon as
    it passes ``max_length``, so an oversized body is only counted and never
    held whole. Anything else is dumped in one call, with the same length and
    errors.
    """
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        text = _COMPACT_ENCODER.encode(value)
        return len(text), text if len(text) <= max_length else None
    pieces: list[str] | None = []
    length = 0
    for piece in _compact_object_pieces(value):
        length += len(piece)
        if pieces is None:
            continue
        if length <= max_length:
            pieces.append(piece)
        else:
            pieces = None
    return length, "".join(pieces) if pieces is not None else None


def _compact_object_pieces(value: dict[str, object]) -> Iterator[str]:
    yield "{"
    for index, (key, member) in enumerate(value.items()):
        if index:
            yield ","
        yield _COMPACT_ENCODER.encode(key)
        yield ":"
        if isinstance(member, list):
            yield "["
            for position, element in enumerate(member):
                if position:
                    yield ","
                yield _COMPACT_ENCODER.encode(element)
            yield "]"
        else:
            yield _COMPACT_ENCODER.encode(member)
    yield "}"
