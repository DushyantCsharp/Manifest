"""The production adapters behind ``manifest_api.ports``.

    shipments    Postgres (asyncpg) for shipments and documents, S3 for presigned multipart
                 uploads, the broker for the pipeline (``manifest.classify`` on ``classify``)
    admission    refuses finalizes while too many pages are pending (429 + Retry-After)
    idempotency  Redis: claimed keys, stored responses

``wiring.build`` assembles them from ``ApiSettings``. The review port has no adapter yet and
still answers 501.
"""
