"""The only gateway to LLM providers.

Provider client, the Redis token bucket shared across workers (RPM/TPM), structured output
into Pydantic schemas, and per-call telemetry. No other package may import a provider SDK.
"""
