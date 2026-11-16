"""Envelope encryption: each practice has its own data key, wrapped by a master key.

Production: set PREVISIT_MASTER_KEY from the host's secret store / KMS. Without it a dev key is
generated in instance/master.key (fine for fake-data demos only).
"""
import hashlib
import hmac
import json
import os
from functools import lru_cache

from cryptography.fernet import Fernet

from . import config


def _master() -> Fernet:
    key = os.getenv("PREVISIT_MASTER_KEY")
    if not key:
        path = config.INSTANCE_DIR / "master.key"
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
        key = path.read_bytes()
    return Fernet(key)


@lru_cache(maxsize=64)
def _data_key(wrapped: bytes) -> bytes:
    return _master().decrypt(wrapped)


def new_wrapped_data_key() -> bytes:
    return _master().encrypt(Fernet.generate_key())


def _cipher(practice) -> Fernet:
    return Fernet(_data_key(practice.wrapped_key))


def encrypt_text(practice, text: str) -> bytes:
    return _cipher(practice).encrypt(text.encode())


def decrypt_text(practice, blob: bytes) -> str:
    return _cipher(practice).decrypt(blob).decode()


def encrypt_json(practice, obj) -> bytes:
    return encrypt_text(practice, json.dumps(obj))


def decrypt_json(practice, blob: bytes):
    return json.loads(decrypt_text(practice, blob))


def keyed_hash(practice, text: str) -> str:
    """Stable lookup key that can't be reversed or guessed without the practice's data key."""
    return hmac.new(_data_key(practice.wrapped_key), text.encode(), hashlib.sha256).hexdigest()
