import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from app.services.credential_crypto import encrypt_password

# Mock database session
mock_db = AsyncMock()


@pytest.fixture
def client():
    # Override get_db dependency
    app.dependency_overrides[get_db] = lambda: mock_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@patch("app.infrastructure.web.routes.decode_access_token")
@patch("app.infrastructure.web.routes.SqlAlchemyRepo")
def test_get_user_credentials_decrypted(mock_repo_class, mock_decode, client):
    # Setup mocks
    mock_decode.return_value = {"sub": "testuser", "is_admin": False}
    mock_repo = mock_repo_class.return_value

    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_repo.get_user_by_username = AsyncMock(return_value=mock_user)

    # Encrypt password to mock DB state
    fiorilli_pw_enc = encrypt_password("fiorilli123")
    ahgora_pw_enc = encrypt_password("ahgora456")

    mock_repo.get_user_credentials = AsyncMock(
        return_value={
            "fiorilli_url": "http://fiorilli",
            "fiorilli_user": "fuser",
            "fiorilli_password_encrypted": fiorilli_pw_enc,
            "ahgora_url": "http://ahgora",
            "ahgora_user": "auser",
            "ahgora_password_encrypted": ahgora_pw_enc,
            "ahgora_company": "acompany",
        }
    )

    # Make request with cookie
    client.cookies.set("access_token", "dummy_token")
    response = client.get("/api/user/credentials")

    assert response.status_code == 200
    data = response.json()
    assert data["fiorilli_url"] == "http://fiorilli"
    assert data["fiorilli_user"] == "fuser"
    assert data["fiorilli_password"] == "fiorilli123"  # Decrypted!
    assert data["ahgora_url"] == "http://ahgora"
    assert data["ahgora_user"] == "auser"
    assert data["ahgora_password"] == "ahgora456"  # Decrypted!
    assert data["ahgora_company"] == "acompany"
    assert "fiorilli_password_encrypted" not in data
    assert "ahgora_password_encrypted" not in data


@patch("app.infrastructure.web.routes.decode_access_token")
@patch("app.infrastructure.web.routes.SqlAlchemyRepo")
def test_save_user_credentials_encrypts(mock_repo_class, mock_decode, client):
    mock_decode.return_value = {"sub": "testuser", "is_admin": False}
    mock_repo = mock_repo_class.return_value

    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_repo.get_user_by_username = AsyncMock(return_value=mock_user)
    mock_repo.get_user_credentials = AsyncMock(return_value=None)
    mock_repo.save_user_credentials = AsyncMock()

    client.cookies.set("access_token", "dummy_token")
    response = client.put(
        "/api/user/credentials",
        data={
            "fiorilli_url": "http://fiorilli",
            "fiorilli_user": "fuser",
            "fiorilli_password": "fiorilli_new",
            "ahgora_url": "http://ahgora",
            "ahgora_user": "auser",
            "ahgora_password": "ahgora_new",
            "ahgora_company": "acompany",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # Verify save was called and arguments were encrypted
    mock_repo.save_user_credentials.assert_called_once()
    args = mock_repo.save_user_credentials.call_args[0]
    assert args[0] == mock_user.id
    saved_dict = args[1]
    assert saved_dict["fiorilli_url"] == "http://fiorilli"
    assert saved_dict["fiorilli_user"] == "fuser"
    assert saved_dict["ahgora_url"] == "http://ahgora"
    assert saved_dict["ahgora_user"] == "auser"
    assert saved_dict["ahgora_company"] == "acompany"

    # Decrypt and check that passwords were saved encrypted
    from app.services.credential_crypto import decrypt_password

    assert decrypt_password(saved_dict["fiorilli_password_encrypted"]) == "fiorilli_new"
    assert decrypt_password(saved_dict["ahgora_password_encrypted"]) == "ahgora_new"


@patch("app.infrastructure.web.routes.decode_access_token")
@patch("app.infrastructure.web.routes.SqlAlchemyRepo")
def test_save_user_credentials_preserves_existing_password(
    mock_repo_class, mock_decode, client
):
    mock_decode.return_value = {"sub": "testuser", "is_admin": False}
    mock_repo = mock_repo_class.return_value

    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_repo.get_user_by_username = AsyncMock(return_value=mock_user)

    fiorilli_pw_enc = encrypt_password("fiorilli_old")
    ahgora_pw_enc = encrypt_password("ahgora_old")

    mock_repo.get_user_credentials = AsyncMock(
        return_value={
            "fiorilli_url": "http://fiorilli",
            "fiorilli_user": "fuser",
            "fiorilli_password_encrypted": fiorilli_pw_enc,
            "ahgora_url": "http://ahgora",
            "ahgora_user": "auser",
            "ahgora_password_encrypted": ahgora_pw_enc,
            "ahgora_company": "acompany",
        }
    )
    mock_repo.save_user_credentials = AsyncMock()

    client.cookies.set("access_token", "dummy_token")
    # Submit PUT with empty passwords (which means preserve the old ones)
    response = client.put(
        "/api/user/credentials",
        data={
            "fiorilli_url": "http://fiorilli_updated",
            "fiorilli_user": "fuser_updated",
            "fiorilli_password": "",
            "ahgora_url": "http://ahgora_updated",
            "ahgora_user": "auser_updated",
            "ahgora_password": "",
            "ahgora_company": "acompany_updated",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    # Verify save was called and original encrypted passwords were kept
    mock_repo.save_user_credentials.assert_called_once()
    args = mock_repo.save_user_credentials.call_args[0]
    saved_dict = args[1]
    assert saved_dict["fiorilli_url"] == "http://fiorilli_updated"
    assert saved_dict["fiorilli_user"] == "fuser_updated"
    assert saved_dict["fiorilli_password_encrypted"] == fiorilli_pw_enc
    assert saved_dict["ahgora_password_encrypted"] == ahgora_pw_enc
