# Tasks: release-stream-lease-on-early-close

## 1. Implementation

- [x] 1.1 `app/modules/proxy/api.py` `_normalize_public_responses_stream`: close
  the inner stream before the native transport-failure `stream_incomplete` raise.
- [x] 1.2 `app/modules/proxy/api.py` `_stream_response_error_events`: close the
  inner stream in a `finally`.
- [x] 1.3 `app/modules/proxy/api.py` `_normalize_reasoning_summary_stream`: close
  the inner stream in a `finally`.
- [x] 1.4 `app/modules/proxy/api.py` `_prepend_first`, `_prepend_items` and
  `_prepend_first_task` (after the probe task settles), and the bridge-bypass
  relays in `app/modules/proxy/_service/http_bridge/streaming.py`: close the
  inner stream in a `finally`.
- [x] 1.5 One `_aclose_stream` helper skips a generator that is still running
  in another task and logs that at debug level.
- [x] 1.6 `_stream_responses`: the startup guard also closes the original service stream.
- [x] 1.7 `_stream_http_bridge_or_retry` closes its active `_stream_via_http_bridge()` iterator; `_stream_proxy_errors_as_response_failed`, `stream_chat_chunks`, `_stream_with_cursor_usage_fallback` and the images replay close their inner stream.
- [x] 1.8 `ClosingStreamingResponse` (base of `DeliveryTracedStreamingResponse`) closes its body and runs an
  `on_close` hook in a shielded `finally`; the Responses route passes the startup teardown, which closes the
  outer keepalive wrapper first; the Images streaming routes use it and close the translator.
- [x] 1.9 The Images streaming translator and non-streaming collector close their upstream on every exit.
- [x] 1.10 The Images streaming routes settle through one owned cleanup, run from the body's `finally` and from
  the response's `on_close`, that closes the translator, the primed replay and the service stream and settles
  the image reservation; the Responses startup teardown runs as one owned task as well.

## 2. Regression coverage

- [x] 2.1 `tests/unit/test_proxy_utils.py::test_native_transport_failure_after_event_closes_the_upstream_stream`:
  drives the real `_normalize_public_responses_stream` / `_stream_response_error_events`
  / `_stream_with_retry` chain, with the upstream suspended at a `yield` when the
  native give-up raises. Asserts the account lease is released. Parametrized
  over the direct chain, both startup-probe wrappers and the bridge bypass;
  the release is checked when the error reaches the consumer.
- [x] 2.2 `tests/unit/test_proxy_api_responses_contract.py::test_aclose_stream_skips_a_generator_another_task_is_running`.
- [x] 2.3 `tests/integration/test_proxy_api_extended.py::test_codex_route_closing_at_the_initial_heartbeat_closes_the_probed_service_stream`.
- [x] 2.4 `tests/unit/test_proxy_utils.py::test_stream_http_bridge_or_retry_closes_the_bridge_stream_on_early_close`.
- [x] 2.5 `tests/unit/test_proxy_api_responses_contract.py::test_lease_holding_relays_close_their_inner_stream_on_early_close`.
- [x] 2.6 `tests/integration/test_proxy_api_extended.py::test_codex_route_disconnect_during_a_send_closes_the_probed_service_stream`
  drives the real ASGI call with a blocked send and an `http.disconnect`.
- [x] 2.7 `tests/unit/test_proxy_api_responses_contract.py::test_startup_guard_closes_its_keepalive_wrapper_first`.
- [x] 2.8 `tests/unit/test_images_translation.py` close tests and
  `tests/integration/test_proxy_images.py::test_images_streaming_closes_the_upstream_when_the_client_leaves_after_a_partial_image`.
- [x] 2.9 `tests/integration/test_proxy_images.py::test_images_streaming_settles_when_the_client_leaves`
  (partial image, and a blocked header send on generations and edits) and
  `tests/unit/test_proxy_utils.py::test_startup_teardown_finishes_the_release_when_its_caller_is_cancelled`.

## 3. Verification

- [x] 3.1 `openspec validate release-stream-lease-on-early-close --strict`
