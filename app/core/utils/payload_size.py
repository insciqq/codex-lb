"""Size a request body without holding a second full copy of it.

A ``response.create`` can be tens of megabytes. Measuring it by dumping or
encoding the whole body allocates another copy of that size, which on the
request path multiplies with every concurrent request and retry.
"""

from __future__ import annotations

import json
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
    element (one input item) instead of the whole body. Anything else is
    measured with a single dump, which yields the same length and errors.
    """
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return len(_COMPACT_ENCODER.encode(value))
    # ``{`` and ``}``, a ``:`` per member and a ``,`` between members.
    length = 2 + 2 * len(value) - 1 if value else 2
    for key, member in value.items():
        length += len(_COMPACT_ENCODER.encode(key))
        if isinstance(member, list):
            # ``[`` and ``]`` and a ``,`` between elements.
            length += 2 + max(len(member) - 1, 0)
            length += sum(len(_COMPACT_ENCODER.encode(element)) for element in member)
        else:
            length += len(_COMPACT_ENCODER.encode(member))
    return length
