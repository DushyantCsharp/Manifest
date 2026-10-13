"""What the API's adapters connect to. ``MANIFEST_*`` variables, or ``.env``.

The broker, Redis and S3 settings use the same names as the worker's, so one ``.env`` serves
both; the database URL is ``MANIFEST_DB_URL`` (``manifest_db.settings``).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MANIFEST_", env_file=".env", extra="ignore")

    broker_url: str = "amqp://manifest:manifest@localhost:5672//"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint_url: str = "http://localhost:9000"
    # Where clients reach storage, for the presigned upload URLs (default: s3_endpoint_url).
    s3_public_endpoint_url: str | None = None
    s3_bucket: str = "manifest-documents"
    s3_access_key: str = "manifest"
    s3_secret_key: str = "manifest-secret"  # noqa: S105 - local MinIO default (.env.example)
    s3_region: str = "us-east-1"
    upload_part_size: int = 8 * 1024 * 1024  # S3 parts are at least 5 MiB, bar the last
    upload_url_ttl_seconds: int = 3600
    idempotency_ttl_seconds: int = 24 * 3600
    # Backpressure: refuse finalizes (429) while more pages than this wait or run.
    pending_pages_high_water: int = 2000
    admission_retry_after_seconds: int = 30
