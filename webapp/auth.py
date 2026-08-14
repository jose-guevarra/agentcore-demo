"""Cognito authentication for the webapp.

Uses an unsigned (no-credentials) boto3 client, the same way a public app
client is meant to be used: end users authenticate with just their Cognito
username/password, never AWS credentials. Mirrors tools/user_login.sh's two
flows (USER_PASSWORD_AUTH, and the NEW_PASSWORD_REQUIRED challenge for
admin-created users).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import boto3
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from config import Config


FRIENDLY_ERRORS = {
    "NotAuthorizedException": "Incorrect username or password.",
    "UserNotFoundException": "Incorrect username or password.",
    "UserNotConfirmedException": "This account has not been confirmed yet.",
    "InvalidPasswordException": "That password doesn't meet the password policy.",
    "InvalidParameterException": "Invalid input.",
    "LimitExceededException": "Too many attempts. Please try again later.",
    "TooManyRequestsException": "Too many attempts. Please try again later.",
}


class AuthError(Exception):
    """Raised with a user-facing message for any Cognito auth failure."""


class NewPasswordRequired(Exception):
    """Raised when Cognito challenges the login with NEW_PASSWORD_REQUIRED."""

    def __init__(self, session: str):
        super().__init__("NEW_PASSWORD_REQUIRED")
        self.session = session


@dataclass(frozen=True)
class Tokens:
    access_token: str
    id_token: str
    refresh_token: str
    expires_at: float  # unix timestamp

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


def _client(config: Config):
    return boto3.client(
        "cognito-idp",
        region_name=config.region,
        config=BotoConfig(signature_version=UNSIGNED),
    )


def _friendly(err: ClientError) -> AuthError:
    code = err.response.get("Error", {}).get("Code", "")
    return AuthError(FRIENDLY_ERRORS.get(code, f"Login failed ({code or 'unknown error'})."))


def _tokens_from_result(result: dict) -> Tokens:
    return Tokens(
        access_token=result["AccessToken"],
        id_token=result["IdToken"],
        refresh_token=result.get("RefreshToken", ""),
        expires_at=time.time() + result.get("ExpiresIn", 3600),
    )


def login(config: Config, username: str, password: str) -> Tokens:
    """Authenticate with USER_PASSWORD_AUTH.

    Raises NewPasswordRequired if Cognito wants a password reset first,
    or AuthError on any other failure.
    """
    client = _client(config)
    try:
        response = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=config.cognito_client_id,
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except ClientError as err:
        raise _friendly(err) from err

    if response.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
        raise NewPasswordRequired(session=response["Session"])

    return _tokens_from_result(response["AuthenticationResult"])


def respond_new_password(config: Config, username: str, new_password: str, session: str) -> Tokens:
    """Complete the NEW_PASSWORD_REQUIRED challenge."""
    client = _client(config)
    try:
        response = client.respond_to_auth_challenge(
            ClientId=config.cognito_client_id,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=session,
            ChallengeResponses={"USERNAME": username, "NEW_PASSWORD": new_password},
        )
    except ClientError as err:
        raise _friendly(err) from err

    return _tokens_from_result(response["AuthenticationResult"])


def refresh(config: Config, refresh_token: str) -> Tokens:
    """Get a fresh access/id token using a refresh token."""
    client = _client(config)
    try:
        response = client.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            ClientId=config.cognito_client_id,
            AuthParameters={"REFRESH_TOKEN": refresh_token},
        )
    except ClientError as err:
        raise _friendly(err) from err

    result = response["AuthenticationResult"]
    return Tokens(
        access_token=result["AccessToken"],
        id_token=result["IdToken"],
        refresh_token=refresh_token,  # not reissued by REFRESH_TOKEN_AUTH
        expires_at=time.time() + result.get("ExpiresIn", 3600),
    )
