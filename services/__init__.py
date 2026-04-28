"""
AARON Router donor-reward services.

Phase 2 (Helius webhook) + Phase 3a (instant 1 JTX SPL drop) live here.
Phase 1 (Xahau Hook trigger) lives in `services.xahau`.

All services are lazy-initialized so the module is importable even when
the JOE agent keypair file or external secrets are not yet provisioned —
required for unit tests and CI.
"""
