from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


ENCRYPTED_PREFIX = "enc:"


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_text(plain_text: str, password: str) -> str:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(iv, plain_text.encode("utf-8"), None)
    payload = salt + iv + ciphertext
    return ENCRYPTED_PREFIX + base64.b64encode(payload).decode("utf-8")


def decrypt_text(encrypted_text: str, password: str) -> str:
    payload_text = encrypted_text.strip()
    if payload_text.startswith(ENCRYPTED_PREFIX):
        payload_text = payload_text[len(ENCRYPTED_PREFIX) :]

    try:
        payload = base64.b64decode(payload_text.encode("utf-8"))
        salt = payload[:16]
        iv = payload[16:28]
        ciphertext = payload[28:]
        key = _derive_key(password, salt)
        decrypted = AESGCM(key).decrypt(iv, ciphertext, None)
        return decrypted.decode("utf-8")
    except Exception as exc:  # pragma: no cover - intentionally collapsed for secret handling
        raise ValueError("解密失败：口令错误或密文已被篡改。") from exc


def is_encrypted_value(value: str) -> bool:
    return value.strip().startswith(ENCRYPTED_PREFIX)