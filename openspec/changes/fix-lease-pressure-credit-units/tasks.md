# Tasks

## 0. Decide

- [x] 0.1 Settle the lease-pressure unit: percentage points per full-size lease (see `context.md`, "Decision").

## 1. Pressure units

- [x] 1.1 `_state_from_account` (`app/modules/proxy/load_balancer.py`): replace the leased-token term with `lease_token_weight x leased_tokens / max_lease_estimate` percentage points, where the maximum estimate is the existing 8,192 + 2,048 token budget; stop dividing by `capacity_credits`.
- [x] 1.2 Define the maximum estimate in the balancer tunables and pin it to the API-key reservation constants in a unit test, so the balancer does not import the API-key service.
- [x] 1.3 Check the other readers of the pressure-adjusted fields (sticky budget gates, `_state_above_budget_threshold`, health tiers, quota planner state builds) and note the effect in `context.md`.

## 2. Verification

- [x] 2.1 Integration test at the routing path: two Plus accounts with persisted weekly usage of 95 and 38 percent and one stream lease each; route an unbound request through the load balancer and assert the 38 percent account is selected.
- [x] 2.2 Integration test: a `codex_session` sticky thread on the 95 percent account, with the secondary budget threshold at 95, is reallocated to the 38 percent account while both hold a stream lease.
- [x] 2.3 Unit test: the pressure a stream lease adds is the same number of points on Plus, Pro Lite and Pro.
- [x] 2.4 `uv run ruff check`, `uv run ruff format --check`, `openspec validate fix-lease-pressure-credit-units --strict`.
