import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.settings import settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    """Return a Fernet instance backed by a SHA-256-derived key from SECRET_KEY."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


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
