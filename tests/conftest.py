"""Top-level pytest config for the Kiosk Satellite Manager suite.

phacc is registered per-layer (integration/conftest.py only, once that layer
exists) so its autouse async fixtures don't inject into sync unit tests.
Mirrors ham-notify's pattern.
"""
