"""
Where media files live: our own copy of every broadcast image and chat attachment.

Two backends behind one interface, chosen by settings.MEDIA_STORE:

  * "s3"    — any S3-compatible store. Production runs MinIO inside the Docker network
              with no public port: files are only ever served through the app's own
              authenticated endpoint (GET /agent-api/v1/media/{id}), never by URL.
  * "local" — a directory on disk, for development and tests.

Keys are opaque strings chosen by the caller (`company/<id>/<yyyy>/<mm>/<uuid>.<ext>`).
Nothing here knows about companies, agents or retention — that is media_assets.py.
"""
import os
from pathlib import Path
from typing import Iterator

from app.core.config import settings

_CHUNK = 64 * 1024


class MediaStoreError(RuntimeError):
    pass


class _LocalStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        # A key can never climb out of the root, whatever it contains.
        if self.root not in p.parents:
            raise MediaStoreError("invalid media key")
        return p

    def put(self, key: str, data: bytes, mime: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def open(self, key: str) -> Iterator[bytes]:
        p = self._path(key)
        if not p.exists():
            raise FileNotFoundError(key)

        def gen():
            with p.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    yield chunk
        return gen()

    def delete(self, key: str) -> None:
        p = self._path(key)
        try:
            p.unlink()
        except FileNotFoundError:
            pass


class _S3Store:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.bucket = settings.MEDIA_S3_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.MEDIA_S3_ENDPOINT or None,
            aws_access_key_id=settings.MEDIA_S3_ACCESS_KEY or None,
            aws_secret_access_key=settings.MEDIA_S3_SECRET_KEY or None,
            region_name=settings.MEDIA_S3_REGION,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 3}),
        )

    def put(self, key: str, data: bytes, mime: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)

    def open(self, key: str) -> Iterator[bytes]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.NoSuchKey:
            raise FileNotFoundError(key)
        return obj["Body"].iter_chunks(_CHUNK)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


_store = None


def get_store():
    """The configured backend. Built once; tests may replace it via set_store()."""
    global _store
    if _store is None:
        _store = _S3Store() if settings.MEDIA_STORE == "s3" else _LocalStore(settings.MEDIA_LOCAL_DIR)
    return _store


def set_store(store) -> None:
    global _store
    _store = store


def local_store(root: str) -> _LocalStore:
    return _LocalStore(root)
