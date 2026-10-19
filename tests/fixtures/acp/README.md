# ACP spec fixtures

`schema.agentic_checkout.json` and `examples.agentic_checkout.json` are copied
unchanged from the Agentic Commerce Protocol repository
(github.com/agentic-commerce-protocol/agentic-commerce-protocol), spec version
**2026-04-17** (`spec/2026-04-17/json-schema/`, `examples/2026-04-17/`), commit
7fdd78df677a94dce04c770644b0fbbb1401272b (2026-07-17).

They are licensed under the Apache License 2.0 (copy in `LICENSE`).
`tests/test_flight_deck/test_payments_acp.py` validates every request
Captain sends against the schema, and the stub merchant answers with the
examples.
