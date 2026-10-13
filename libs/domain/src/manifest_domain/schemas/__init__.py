"""Versioned extraction schemas.

Each ``vN`` package is frozen once released: changing what the LLM extracts means adding a
new version, never editing an old one. Every persisted extraction records the
``schema_version`` it was produced under.
"""
