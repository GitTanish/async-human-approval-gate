# Async Human Approval Gate

A small Python component that pauses an agent workflow until a human approves or rejects a proposed action. It handles pending requests, timeouts, and duplicate responses, with explicit state transitions and deterministic tests.

The gate guarantees a single durable decision and idempotent retries of that decision. It does not make external side effects exactly-once; use the request ID as the downstream idempotency key.

Design: [docs/design.md](docs/design.md)
