# Controlled interruption presentation

Native Codex can interrupt an active generation to incorporate an incoming subagent message. The upstream terminal is `response.incomplete` with reason `interrupted`. Shared WebSocket settlement records that terminal as cancelled, preserving partial usage and forwarding the original event. The existing blue Cancelled badge is reused.

Rows recorded earlier as `error/interrupted` keep their Error status; they are not remapped when read. Report rollups have no error-code dimension and cannot split them out of their error counts, so a read-side remap would make the request list disagree with the aggregates.

Historical aggregate buckets retain their prior error counts under the existing historical-bucket policy. Newly stored cancellations enter the existing cancellation measures. For example, a subagent result arriving during generation can produce a Cancelled row with 241 output tokens; those tokens remain accounted for, and the row remains inspectable with reason `interrupted`.
