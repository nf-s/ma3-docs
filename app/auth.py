"""Environment-configured HTTP Basic authentication for private HTTPS hosting."""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field


CHALLENGE = 'Basic realm="MA3 Reference", charset="UTF-8"'
MIN_PASSWORD_LENGTH = 16
MAX_CREDENTIAL_BYTES = 4096
MAX_HEADER_LENGTH = 8192


class AuthConfigurationError(ValueError):
    """Authentication cannot safely start with the supplied configuration."""


def is_loopback_host(host: str) -> bool:
    """Recognize loopback literals without trusting DNS resolution."""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


@dataclass(frozen=True)
class AuthConfig:
    """Keep only credential digests; construct through from_environment()."""

    enabled: bool
    _username_digest: bytes = field(repr=False)
    _password_digest: bytes = field(repr=False)

    @classmethod
    def from_environment(
        cls, host: str, environment: Mapping[str, str] | None = None
    ) -> "AuthConfig":
        values = os.environ if environment is None else environment
        username = values.get("MA3_DOCS_USERNAME", "ma3")
        password = values.get("MA3_DOCS_PASSWORD", "")
        if not isinstance(host, str):
            raise AuthConfigurationError("The bind host must be a string.")
        if not isinstance(username, str) or not isinstance(password, str):
            raise AuthConfigurationError("Authentication environment values must be strings.")
        if not username or ":" in username or has_control_characters(username):
            raise AuthConfigurationError(
                "MA3_DOCS_USERNAME must be nonempty and contain no colon or control characters."
            )
        if not password:
            if not is_loopback_host(host):
                raise AuthConfigurationError(
                    "MA3_DOCS_PASSWORD is required when binding beyond loopback."
                )
            return cls(False, b"", b"")
        if len(password) < MIN_PASSWORD_LENGTH or not password.strip():
            raise AuthConfigurationError(
                "MA3_DOCS_PASSWORD must contain at least 16 characters and not be only whitespace."
            )
        if has_control_characters(password):
            raise AuthConfigurationError("MA3_DOCS_PASSWORD must not contain control characters.")
        try:
            username_bytes = username.encode("utf-8")
            password_bytes = password.encode("utf-8")
        except UnicodeEncodeError:
            raise AuthConfigurationError("Authentication credentials must be valid UTF-8.") from None
        if len(username_bytes) + 1 + len(password_bytes) > MAX_CREDENTIAL_BYTES:
            raise AuthConfigurationError("Authentication credentials exceed the supported length.")
        return cls(
            True,
            hashlib.sha256(username_bytes).digest(),
            hashlib.sha256(password_bytes).digest(),
        )

    def is_authorized(self, header: str | None) -> bool:
        """Check one Authorization value; reject duplicate headers in the handler.

        The handler must enforce this check before all routes, return 401 with
        WWW-Authenticate: CHALLENGE on failure, and serve hosted traffic over TLS.
        """
        if not self.enabled:
            return True
        if not isinstance(header, str) or len(header) > MAX_HEADER_LENGTH:
            return False
        if has_control_characters(header) or not header.isascii():
            return False
        scheme, separator, token = header.partition(" ")
        if scheme.casefold() != "basic" or not separator:
            return False
        try:
            credential_bytes = base64.b64decode(token.lstrip(" "), validate=True)
            if len(credential_bytes) > MAX_CREDENTIAL_BYTES:
                return False
            credentials = credential_bytes.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        username, separator, password = credentials.partition(":")
        if not separator or has_control_characters(credentials):
            return False
        username_matches = hmac.compare_digest(
            hashlib.sha256(username.encode("utf-8")).digest(), self._username_digest
        )
        password_matches = hmac.compare_digest(
            hashlib.sha256(password.encode("utf-8")).digest(), self._password_digest
        )
        return username_matches and password_matches
