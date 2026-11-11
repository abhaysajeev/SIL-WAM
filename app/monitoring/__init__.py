"""Copying API traffic to the central log server (the log-ingestion project).

  middleware.py  captures /client-api, /agent-api and /webhook requests
  redact.py      masks secrets and trims what is kept
  shipper.py     sends batches from its own thread, never on a request

Off unless LOG_INGEST_URL and LOG_INGEST_KEY are both set.
"""
