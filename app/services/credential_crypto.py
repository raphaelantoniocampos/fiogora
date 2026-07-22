import base64
import hashlib
import logging
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.settings import settings

logger = logging.getLogger(__name__)

_METADATA_KEY = "_encrypted_credentials"
_NULL_SEP = b"\x00"


def _get_fernet() -> Fernet:
    """Return a Fernet instance backed by a SHA-256-derived key from SECRET_KEY."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_credentials(
    fiorilli_password: Optional[str], ahgora_password: Optional[str]
) -> str:
    """Encrypt two passwords into a single Fernet token string."""
    fp = fiorilli_password or ""
    ap = ahgora_password or ""

    # Safely stringify any mock/MagicMock objects passed in tests
    if not isinstance(fp, str):
        fp = str(fp)
    if not isinstance(ap, str):
        ap = str(ap)

    plaintext = fp.encode() + _NULL_SEP + ap.encode()
    return _get_fernet().encrypt(plaintext).decode()


def decrypt_credentials(token: str) -> tuple[str, str]:
    """Decrypt a Fernet token back to (fiorilli_password, ahgora_password)."""
    try:
        plaintext = _get_fernet().decrypt(token.encode())
    except InvalidToken:
        raise ValueError("Fernet decryption failed — invalid or corrupted token")
    parts = plaintext.split(_NULL_SEP, 1)
    if len(parts) != 2:
        raise ValueError("Decrypted credential payload has unexpected format")
    return parts[0].decode(), parts[1].decode()


def encrypt_password(password: str) -> str:
    """Encrypt a single password using Fernet."""
    return _get_fernet().encrypt(password.encode()).decode()


def decrypt_password(token: str) -> str:
    """Decrypt a single password from a Fernet token."""
    try:
        plaintext = _get_fernet().decrypt(token.encode())
    except InvalidToken:
        raise ValueError("Fernet decryption failed — invalid or corrupted token")
    return plaintext.decode()


def store_credentials_in_metadata(
    metadata: dict[str, Any],
    fiorilli_password: str,
    ahgora_password: str,
) -> None:
    """Store Fernet-encrypted credentials in a job metadata dict (mutates in-place)."""
    metadata[_METADATA_KEY] = encrypt_credentials(fiorilli_password, ahgora_password)


def extract_credentials_from_metadata(
    metadata: dict[str, Any],
) -> Optional[tuple[str, str]]:
    """
    Extract and decrypt credentials from job metadata.

    Returns None if no credentials are stored (legacy job).
    Raises ValueError if credentials exist but decryption fails.
    """
    token = metadata.get(_METADATA_KEY)
    if token is None:
        return None
    return decrypt_credentials(token)


def decrypt_credentials_dict(credentials_dict, user_id):
    # Decrypt passwords
    if credentials_dict.get("fiorilli_password_encrypted"):
        try:
            credentials_dict["fiorilli_password"] = decrypt_password(
                credentials_dict["fiorilli_password_encrypted"]
            )
        except Exception as e:
            logger.error(f"Failed to decrypt Fiorilli password for user {user_id}: {e}")
    if credentials_dict.get("ahgora_password_encrypted"):
        try:
            credentials_dict["ahgora_password"] = decrypt_password(
                credentials_dict["ahgora_password_encrypted"]
            )
        except Exception as e:
            logger.error(f"Failed to decrypt Ahgora password for user {user_id}: {e}")
    return credentials_dict
