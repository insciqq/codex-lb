## 1. Implementation

- [x] 1.1 Measure body sizes one input item at a time for the transport decision, the websocket budget and slimming.
- [x] 1.2 Serialize an oversized body once, after slimming, on the bridge and websocket preparation paths.
- [x] 1.3 Hash input fingerprints one item at a time.
- [x] 1.4 Serialize the native egress body at dispatch and stream its base64 encoding into the command line.
- [x] 1.5 Close the helper's input under the write lock, with bounded waits.

## 2. Verification

- [x] 2.1 Peak-allocation regressions for direct HTTP dispatch, websocket preparation and the fingerprint.
- [x] 2.2 Exactness: sizes match the compact dump, digests match the whole-list dump, the command line decodes to the body.
- [x] 2.3 A sender cancelled under pipe backpressure still queues one complete command line.
- [x] 2.4 Closing the client finishes the command being written, drops queued ones and terminates a helper that stopped reading.
