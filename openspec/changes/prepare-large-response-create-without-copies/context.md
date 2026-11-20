# Context

Peak traced allocations for a synthetic ~50 MB image-heavy body, relative to
the body size (`tests/unit/test_response_create_memory.py`):

| Path | Before | After |
| --- | --- | --- |
| Direct HTTP dispatch through the native egress helper | 6.0x | 2.0x |
| Websocket `response.create` preparation, oversized frame (after parsing) | 2.0x | 0.25x |
| Input fingerprint | 2.0x | 0.25x (largest item) |

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
