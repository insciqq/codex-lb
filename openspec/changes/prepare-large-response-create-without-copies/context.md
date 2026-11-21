# Context

Peak traced allocations for a synthetic ~50 MB image-heavy body, relative to
the body size (`tests/unit/test_response_create_memory.py`):

| Path | Before | After |
| --- | --- | --- |
| Direct HTTP dispatch through the native egress helper | 6.0x | 2.0x |
| Websocket `response.create` preparation, oversized frame (after parsing) | 2.0x | 0.41x-0.54x |
| Input fingerprint | 2.0x | 0.25x (largest item) |

Event-loop time of preparing a routine ~15 MB body (below the websocket
budget) on Python 3.14, the version the container image runs: minimum of 30
runs in a fresh process, before the body copies were reduced, with a separate
measuring pass, and with the measuring pass producing the frame.

| Body | Bridge preparation | Retry-body size guard |
| --- | --- | --- |
| Image-heavy (13.8 MB) | 65.7 / 57.7 / 54.5 ms | 30.5 / 34.9 / 31.0 ms |
| Tool-output-heavy (15.1 MB) | 92.5 / 105.9 / 85.5 ms | 45.8 / 62.4 / 43.4 ms |
| Non-ASCII text (15.0 MB) | 54.0 / 55.4 / 59.9 ms | 32.3 / 32.5 / 38.2 ms |

A routine body is encoded once, member by member, while it is measured, and
the pieces are joined into the frame; past the budget the pieces are dropped
and the body is only counted. An oversized frame therefore peaks at the
websocket budget plus the input item that crosses it (built twice by the
Python 3.13 and 3.14 encoders), 0.54x of a 50 MB body there and 0.41x on 3.15,
instead of a multiple of the body. Text with many short non-ASCII items stays a
few milliseconds above the original single dump: holding the pieces and then
joining them writes fresh memory twice, as the original dump and its encoding
did, while the separate counting pass reused one block.

The remaining direct HTTP peak is the moment the body is serialized: the
JSON text and its encoding. The helper protocol carries the body inside one
JSON line; the line is written one base64 slice at a time and drained between
slices, so the pipe buffer holds one slice instead of the whole line. A sender
cancelled part way queues the rest of the line at once before the
cancellation propagates, so the helper never reads a partial command and the
cancellation is not held up by a slow helper. Before this change the whole
line sat in the pipe buffer, which on Python 3.13 alone added about 2x the
body. The parsed payload and the ingress body itself are outside these
numbers.
