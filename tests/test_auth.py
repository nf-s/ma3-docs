"""Authentication configuration and credential boundary checks without a server."""

import base64
import hmac
import unittest
from unittest.mock import patch

from app.auth import AuthConfig, AuthConfigurationError, CHALLENGE


def basic_header(username, password):
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


class AuthenticationTests(unittest.TestCase):
    password = "correct horse battery staple"

    def config(self, host="0.0.0.0", **environment):
        values = {"MA3_DOCS_PASSWORD": self.password, **environment}
        return AuthConfig.from_environment(host, values)

    def test_loopback_without_password_is_optional(self):
        for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "LOCALHOST"):
            for environment in ({}, {"MA3_DOCS_PASSWORD": ""}):
                with self.subTest(host=host, environment=environment):
                    config = AuthConfig.from_environment(host, environment)
                    self.assertFalse(config.enabled)
                    self.assertTrue(config.is_authorized(None))
                    self.assertTrue(config.is_authorized("malformed"))

    def test_missing_password_fails_closed_for_external_hosts(self):
        for host in ("0.0.0.0", "::", "", "192.168.1.2", "example.com", "localhost.example.com", "localhost."):
            for environment in ({}, {"MA3_DOCS_PASSWORD": ""}):
                with self.subTest(host=host, environment=environment):
                    with self.assertRaises(AuthConfigurationError):
                        AuthConfig.from_environment(host, environment)

    def test_weak_explicit_password_rejected_even_on_loopback(self):
        for host in ("127.0.0.1", "0.0.0.0"):
            for password in ("short", "x" * 15, " " * 20, "valid length\nsecret"):
                with self.subTest(host=host, password=password):
                    with self.assertRaises(AuthConfigurationError):
                        self.config(host, MA3_DOCS_PASSWORD=password)

    def test_default_username_and_exact_password(self):
        config = self.config()
        self.assertTrue(config.enabled)
        self.assertTrue(config.is_authorized(basic_header("ma3", self.password)))
        self.assertFalse(config.is_authorized(basic_header("MA3", self.password)))
        self.assertFalse(config.is_authorized(basic_header("ma3", self.password.upper())))
        self.assertFalse(config.is_authorized(basic_header("wrong", self.password)))
        self.assertFalse(config.is_authorized(basic_header("ma3", "wrong")))

    def test_explicit_password_enables_auth_on_loopback(self):
        config = self.config("127.0.0.1")
        self.assertTrue(config.enabled)
        self.assertFalse(config.is_authorized(None))
        self.assertTrue(config.is_authorized(basic_header("ma3", self.password)))

    def test_unicode_credentials_and_colons_in_password(self):
        username = "éclairage 🎛"
        password = "  lumière:色:🔐 busking  "
        config = self.config(MA3_DOCS_USERNAME=username, MA3_DOCS_PASSWORD=password)
        self.assertTrue(config.is_authorized(basic_header(username, password)))
        self.assertFalse(config.is_authorized(basic_header(username, password.strip())))
        self.assertFalse(config.is_authorized(basic_header("e\u0301clairage 🎛", password)))
        self.assertIn('charset="UTF-8"', CHALLENGE)

    def test_minimum_counts_characters_not_utf8_bytes(self):
        with self.assertRaises(AuthConfigurationError):
            self.config(MA3_DOCS_PASSWORD="🔐" * 8)
        config = self.config(MA3_DOCS_PASSWORD="🔐" * 16)
        self.assertTrue(config.is_authorized(basic_header("ma3", "🔐" * 16)))

    def test_invalid_username_and_unencodable_credentials_are_rejected(self):
        for username in ("", "name:colon", "name\nnewline", "name\x00null", "name\x7f", "name\ud800"):
            with self.subTest(username=username):
                with self.assertRaises(AuthConfigurationError):
                    self.config(MA3_DOCS_USERNAME=username)
        with self.assertRaises(AuthConfigurationError):
            self.config(MA3_DOCS_PASSWORD=self.password + "\ud800")

    def test_malformed_headers_safely_reject(self):
        valid = basic_header("ma3", self.password)
        cases = [
            None, "", "Basic", "Basic ", "Bearer token", "Basic !!!", "Basic YQ",
            valid + " garbage", valid + ", " + valid, valid + "\n", valid + "\r\n",
            "Basic\t" + valid[6:], "Basic \u00e9", "Basic " + "A" * 9000,
            "Basic " + base64.b64encode(b"ma3:\xff").decode("ascii"),
            "Basic " + base64.b64encode(b"no delimiter").decode("ascii"),
            basic_header("ma3", self.password + "\x00"),
            basic_header("", self.password), basic_header("ma3", ""),
        ]
        config = self.config()
        for header in cases:
            with self.subTest(header=header):
                self.assertFalse(config.is_authorized(header))

    def test_scheme_is_case_insensitive_and_multiple_spaces_are_valid(self):
        token = basic_header("ma3", self.password)[6:]
        self.assertTrue(self.config().is_authorized("bAsIc   " + token))

    def test_both_fixed_size_digests_compared_even_when_username_fails(self):
        config = self.config()
        with patch("app.auth.hmac.compare_digest", wraps=hmac.compare_digest) as comparison:
            self.assertFalse(config.is_authorized(basic_header("wrong", "also wrong")))
        self.assertEqual(comparison.call_count, 2)
        for call in comparison.call_args_list:
            self.assertEqual([len(argument) for argument in call.args], [32, 32])

    def test_credentials_not_retained_or_in_error_messages(self):
        username = "private account"
        config = self.config(MA3_DOCS_USERNAME=username)
        self.assertNotIn(username, repr(config))
        self.assertNotIn(self.password, repr(config))
        self.assertTrue(all(not isinstance(value, str) for value in vars(config).values()))
        for password in ("private weak", "private\ud800" + self.password, self.password * 500):
            with self.assertRaises(AuthConfigurationError) as error:
                self.config(MA3_DOCS_PASSWORD=password)
            self.assertNotIn(password, str(error.exception))
            self.assertNotIn(username, str(error.exception))

    def test_oversized_config_and_request_rejected(self):
        with self.assertRaises(AuthConfigurationError):
            self.config(MA3_DOCS_PASSWORD="p" * 4096)
        self.assertFalse(self.config().is_authorized(basic_header("ma3", "p" * 4096)))

    def test_environment_is_read_without_mutation_and_can_default_to_process(self):
        environment = {"MA3_DOCS_USERNAME": "operator", "MA3_DOCS_PASSWORD": self.password}
        before = dict(environment)
        config = AuthConfig.from_environment("0.0.0.0", environment)
        self.assertEqual(environment, before)
        with patch.dict("os.environ", environment, clear=True):
            from_process = AuthConfig.from_environment("0.0.0.0")
        header = basic_header("operator", self.password)
        self.assertTrue(config.is_authorized(header))
        self.assertTrue(from_process.is_authorized(header))

    def test_non_string_environment_values_fail_with_safe_error(self):
        for key in ("MA3_DOCS_USERNAME", "MA3_DOCS_PASSWORD"):
            with self.subTest(key=key):
                with self.assertRaises(AuthConfigurationError):
                    AuthConfig.from_environment("0.0.0.0", {key: None})


if __name__ == "__main__":
    unittest.main()
