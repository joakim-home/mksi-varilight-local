# Contributing

Contributions are welcome, especially captures/tests for additional VARILIGHT firmware revisions.

## Principles

- Keep the implementation narrow: implement observed/verified device behavior, not the full SUPLA ecosystem.
- Preserve stock dimmer firmware compatibility.
- Do not weaken GUID allowlisting or availability semantics for convenience.
- Keep MQTT callbacks non-blocking with respect to device session/keepalive handling.
- Add tests for protocol/lifecycle changes.
- Do not commit live credentials, private keys, personal LAN configuration or raw registration AuthKeys.

## Before opening a pull request

Run:

```bash
python3 -m unittest -v test_protocol.py test_controller.py
python3 -m py_compile controller.py protocol.py ui.py wait-for-bind.py
```

For protocol changes, document the evidence: firmware version, transport negotiation, call ID/payload shape and relevant capture observations.

## New firmware support

Do not generalize based on a single assumption. Capture registration, timeout negotiation, ping, one physical state change and one server-driven brightness command/acknowledgement. Add firmware-specific differences to `docs/PROTOCOL.md` and tests.
