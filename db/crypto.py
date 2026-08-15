"""
db/crypto.py — Symmetric encryption for CloudTenant.client_secret at rest.

Client Secret must be recoverable (the app needs the real value to
authenticate against Azure), unlike AppUser passwords (db/users.py), which
are one-way bcrypt hashes that never need to be un-hashed - hashing wouldn't
work here, so this uses Fernet (AES-128-CBC + HMAC) from the `cryptography`
package instead. `cryptography` is already a direct dependency (pulled in by
azure-identity, and pinned explicitly in requirements.txt), so this adds no
new package.

Key comes from the TENANT_SECRET_KEY env var - production must set this
(an Azure App Service application setting, same as DATABASE_URL). Local dev
without it falls back to a key auto-generated and cached in .tenant_secret_key
(gitignored) so local development isn't blocked - that fallback is per-machine
and NOT suitable for production.
"""

import os
from cryptography.fernet import Fernet, InvalidToken

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL_KEY_FILE = os.path.join(_PROJECT_ROOT, ".tenant_secret_key")

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.getenv("TENANT_SECRET_KEY")
    if not key:
        if os.path.exists(_LOCAL_KEY_FILE):
            with open(_LOCAL_KEY_FILE, "r") as f:
                key = f.read().strip()
        else:
            key = Fernet.generate_key().decode("utf-8")
            with open(_LOCAL_KEY_FILE, "w") as f:
                f.write(key)
            print(
                "[Warning] TENANT_SECRET_KEY not set - generated a local-only "
                "encryption key at .tenant_secret_key for development. "
                "Set TENANT_SECRET_KEY as a real app setting in production."
            )
    _fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    """Returns the Fernet-encrypted ciphertext (a string, safe to store in a
    String column). Empty/None input passes through unchanged - nothing to
    encrypt."""
    if not plaintext:
        return plaintext
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Returns the real plaintext secret. Falls back to returning the input
    as-is if it doesn't decrypt as a valid Fernet token - covers tenant rows
    saved before this encryption was added (still plaintext in the DB), so
    existing connected tenants keep working instead of breaking outright."""
    if not ciphertext:
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ciphertext
