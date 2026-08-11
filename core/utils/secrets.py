"""
Local secret protection via Windows DPAPI
=========================================

Encrypts API keys at rest in config/hotwords.json using the Windows Data
Protection API (CryptProtectData / CryptUnprotectData, current-user scope)
through ctypes — no external dependencies.

Stored format: "dpapi:v1:<base64>". Plaintext values pass through readers
unchanged, so old configs keep working; writers encrypt when the platform
supports it and silently keep plaintext otherwise (non-Windows dev boxes).

Note: DPAPI ciphertext is bound to the Windows user account. A config file
copied to another machine/user cannot be decrypted there — readers get ""
and the user re-enters the key in Settings.
"""

import base64
import sys

SECRET_PREFIX = "dpapi:v1:"


class SecretsError(Exception):
    """Base error for secret protection failures (platform / crypto)."""


class SecretsUnavailableError(SecretsError):
    """DPAPI is not available on this platform."""


class SecretDecryptError(SecretsError):
    """Ciphertext is corrupt or was encrypted by a different user/machine."""


def is_encrypted(value: object) -> bool:
    """True if value is a string in the dpapi:v1: envelope format."""
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def _dpapi_crypt(data: bytes, *, protect: bool) -> bytes:
    """Raw CryptProtectData / CryptUnprotectData round through ctypes."""
    if sys.platform != "win32":
        raise SecretsUnavailableError("DPAPI requires Windows")

    import ctypes
    import ctypes.wintypes as wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    blob_in = DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = func(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        if protect:
            raise SecretsError("CryptProtectData failed")
        raise SecretDecryptError("CryptUnprotectData failed (corrupt or foreign key)")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def encrypt_str(plaintext: str) -> str:
    """Encrypt plaintext into the dpapi:v1:<base64> envelope.

    Already-encrypted input is returned unchanged (idempotent). Raises
    SecretsUnavailableError on non-Windows, SecretsError on DPAPI failure.
    """
    if is_encrypted(plaintext):
        return plaintext
    blob = _dpapi_crypt(plaintext.encode("utf-8"), protect=True)
    return SECRET_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_str(token: str) -> str:
    """Decrypt a dpapi:v1:<base64> envelope back to plaintext.

    Raises SecretDecryptError for malformed/corrupt/foreign ciphertext and
    SecretsUnavailableError on non-Windows.
    """
    if not is_encrypted(token):
        raise SecretDecryptError("Not a dpapi:v1: token")
    try:
        blob = base64.b64decode(token[len(SECRET_PREFIX) :], validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretDecryptError(f"Invalid base64 payload: {exc}") from exc
    return _dpapi_crypt(blob, protect=False).decode("utf-8")


def reveal_secret(value: str) -> str:
    """Read-side helper: decrypt if encrypted, pass plaintext through.

    Never raises: on decryption failure the key is unusable anyway, so
    return "" (callers already treat an empty key as "not configured").
    """
    if not value or not is_encrypted(value):
        return value
    try:
        return decrypt_str(value)
    except SecretsError:
        return ""


def protect_secret(value: str) -> str:
    """Write-side helper: encrypt plaintext when possible.

    Empty and already-encrypted values are returned unchanged. If DPAPI is
    unavailable (non-Windows) or fails, the plaintext is returned so config
    saving never breaks.
    """
    if not value or is_encrypted(value):
        return value
    try:
        return encrypt_str(value)
    except SecretsError:
        return value
