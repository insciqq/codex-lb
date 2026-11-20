## Why

A `response.create` body of tens of megabytes (an image-heavy thread resent
in full) was held as several full copies at once on the request path. Sizing
it for the transport decision and the websocket budget dumped and encoded the
whole body; the bridge and websocket preparation dumped it again before
slimming, fingerprinted the input with another full dump, and the direct HTTP
path kept the JSON text for the whole attempt while the native egress command
built base64 bytes, a decoded string, a JSON dump, its encoding and a
newline-joined copy. A single 50 MB request peaked at about six times its
size in Python on the direct HTTP path, and every retry repeated the work.

## What Changes

- Body sizes are measured one input item at a time; the measured byte counts
  are unchanged.
- An oversized body is serialized once, after historical slimming.
- Input fingerprints hash the canonical dump one item at a time; the digest
  is unchanged.
- The direct HTTP path serializes the body at dispatch instead of holding the
  JSON text for the attempt.
- The native egress request command writes the body base64-encoded in bounded
  slices with pipe backpressure between them; a cancelled sender still queues
  the whole line.

## Impact

Request preparation only. Wire bodies, size decisions, slimming, digests and
the native helper protocol are unchanged. No setting, schema or API change.
