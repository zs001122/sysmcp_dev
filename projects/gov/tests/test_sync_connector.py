"""Offline publication checks with synthetic claims and a captured management client."""

import base64
import copy
import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from ops import sync_connector as sync

NOW = 1_800_000_000


def jwt(claims, header=None):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return ".".join(
        (
            part(header or {"alg": "RS256", "typ": "JWT"}),
            part(claims),
            "synthetic-signature",
        )
    )


class FakeClient:
    def __init__(self, server):
        self.server = copy.deepcopy(server)
        self.updates = []
        self.gets = 0
        self.logins = 0
        self.login_error = False
        self.fail_get_at = None
        self.update_error = False
        self.apply_update = True
        self.drift = False

    def login(self, _):
        self.logins += 1
        if self.login_error:
            sync.fail("login_failed", "login")

    def get(self):
        self.gets += 1
        if self.gets == self.fail_get_at:
            sync.fail("get_failed", "get")
        return copy.deepcopy(self.server)

    def update(self, payload):
        self.updates.append(copy.deepcopy(payload))
        if self.apply_update:
            self.server.update(payload)
        if self.drift:
            self.server["env"] = {"UNEXPECTED": "synthetic"}
        if self.update_error:
            sync.fail("update_failed", "update")


class ConnectorTest(unittest.TestCase):
    def setUp(self):
        # /tmp is writable by other users and deliberately rejected by the tool.
        self.directory = tempfile.TemporaryDirectory(
            prefix="gov-sync-test-", dir=Path.home()
        )
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.config_path = self.base / "config.json"
        self.config = {
            "schema_version": 1,
            "base_url": "http://127.0.0.1:12008",
            "origin": "http://127.0.0.1:12008",
            "server_uuid": "00000000-0000-4000-8000-000000000001",
            "expected_name": "sysmcp-t100",
            "expected_type": "STREAMABLE_HTTP",
            "expected_url": "http://127.0.0.1:8781/mcp",
            "token_file": str(self.base / "token.jwt"),
            "admin_login_file": str(self.base / "login.json"),
            "state_file": str(self.base / "state.json"),
            "expected_issuer": "https://synthetic-issuer.invalid",
            "expected_audience": "synthetic-t100",
            "expected_subject": "synthetic-connector",
            "expected_client_id": "synthetic-connector",
            "expected_scopes": ["t100:employee:query", "t100:account:maintain"],
        }
        self.claims = {
            "iss": self.config["expected_issuer"],
            "aud": self.config["expected_audience"],
            "sub": self.config["expected_subject"],
            "azp": self.config["expected_client_id"],
            "scope": " ".join(self.config["expected_scopes"]),
            "iat": NOW,
            "exp": NOW + 3600,
        }
        self.token = jwt(self.claims)
        self.put(self.config_path, self.config)
        self.put(Path(self.config["token_file"]), self.token)
        self.put(
            Path(self.config["admin_login_file"]),
            {"email": "operator@example.invalid", "password": "synthetic-password"},
        )
        self.server = {
            "uuid": self.config["server_uuid"],
            "name": "sysmcp-t100",
            "type": "STREAMABLE_HTTP",
            "url": self.config["expected_url"],
            "headers": {"X-Synthetic": "unchanged"},
            "args": ["unchanged"],
            "env": {"SYNTHETIC": "unchanged"},
            "description": None,
            "command": None,
            "user_id": "synthetic-user",
            "bearerToken": "old-synthetic-token",
            "created_at": "synthetic-time",
            "error_status": "NONE",
        }
        self.client = FakeClient(self.server)

    def put(self, path, value):
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(0o600)

    def execute(self, client=None):
        return sync.execute(
            sync.load_config(self.config_path),
            client_factory=lambda _: client or self.client,
            clock=lambda: NOW,
        )

    def error(self, code, action=None):
        with self.assertRaises(sync.SyncError) as caught:
            (action or self.execute)()
        self.assertEqual(caught.exception.code, code)

    def state(self):
        return json.loads(Path(self.config["state_file"]).read_text())

    def test_updates_only_token_and_verifies(self):
        result = self.execute()
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(result["business_requests"], 0)
        self.assertEqual(self.client.gets, 2)
        self.assertEqual(len(self.client.updates), 1)
        expected = {
            k: v
            for k, v in self.server.items()
            if k in sync.SERVER_FIELDS and v is not None
        }
        expected["bearerToken"] = self.token
        self.assertEqual(self.client.updates[0], expected)
        state = self.state()
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["last_success"], sync.timestamp(NOW))
        raw = Path(self.config["state_file"]).read_text()
        for secret in (
            self.token,
            "synthetic-password",
            "old-synthetic-token",
            "operator@example.invalid",
        ):
            self.assertNotIn(secret, raw)
        self.assertEqual(Path(self.config["state_file"]).stat().st_mode & 0o777, 0o600)

    def test_optional_fields_and_null_owner_preserved(self):
        self.client.server.update(
            description="retained", command="retained", user_id=None
        )
        self.execute()
        for key in ("description", "command", "user_id"):
            self.assertEqual(self.client.updates[0][key], self.client.server[key])

    def test_same_token_does_not_update(self):
        self.client.server["bearerToken"] = self.token
        self.assertEqual(self.execute()["outcome"], "unchanged")
        self.assertEqual(self.client.updates, [])
        self.assertEqual(self.client.gets, 1)

    def test_wrong_server_identity_never_updates(self):
        for key in ("uuid", "name", "type", "url"):
            with self.subTest(key=key):
                client = FakeClient(self.server)
                client.server[key] = "unexpected"
                self.error(
                    "server_identity_mismatch",
                    lambda client=client: self.execute(client),
                )
                self.assertEqual(client.updates, [])

    def test_bad_server_fields_never_update(self):
        for key, value in (
            ("headers", []),
            ("env", {"bad": 1}),
            ("args", [1]),
            ("user_id", 1),
        ):
            with self.subTest(key=key):
                client = FakeClient(self.server)
                client.server[key] = value
                self.error(
                    "server_contract_failed", lambda client=client: self.execute(client)
                )
                self.assertEqual(client.updates, [])

    def test_bad_token_is_rejected_before_login(self):
        cases = [
            {"exp": NOW - 1},
            {"exp": NOW + 599},
            {"exp": NOW + 3601},
            {"iat": NOW + 31},
            {"iat": True},
            {"exp": float(NOW + 3600)},
            {"nbf": NOW + 31},
            {"iss": "wrong"},
            {"aud": [self.claims["aud"]]},
            {"sub": "wrong"},
            {"azp": "wrong"},
            {"scope": self.claims["scope"] + " admin"},
            {"scope": self.claims["scope"] + " "},
            {"scope": "t100:employee:query"},
            {"scp": ["admin"]},
            {"employee_no": "E001"},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.put(Path(self.config["token_file"]), jwt(self.claims | change))
                self.error("token_constraints_failed")
                self.assertEqual(self.client.logins, 0)
                self.assertEqual(self.client.updates, [])

    def test_wrong_algorithm_or_malformed_token(self):
        for token in (
            jwt(self.claims, {"alg": "none"}),
            "not-a-jwt",
            "a.b.c",
            self.token + "\nextra",
        ):
            with self.subTest(token=token[:10]):
                self.put(Path(self.config["token_file"]), token)
                self.error("token_constraints_failed")

    def test_login_failure_preserves_last_success_and_exposes_no_secret(self):
        self.execute()
        previous = self.state()
        self.client.login_error = True
        self.error("login_failed")
        current = self.state()
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["last_success"], previous["last_success"])
        self.assertEqual(current["token_sha256"], previous["token_sha256"])
        self.assertEqual(current["error"], "login_failed")

    def test_first_get_failure_has_zero_updates(self):
        self.client.fail_get_at = 1
        self.error("get_failed")
        self.assertEqual(self.client.updates, [])

    def test_lost_update_response_reconciles_without_second_post(self):
        self.client.update_error = True
        self.assertEqual(self.execute()["outcome"], "reconciled")
        self.assertEqual(len(self.client.updates), 1)
        self.assertEqual(self.client.gets, 2)

    def test_rejected_update_is_not_retried(self):
        self.client.update_error = True
        self.client.apply_update = False
        self.error("update_not_verified")
        self.assertEqual(len(self.client.updates), 1)
        self.assertEqual(self.client.gets, 2)
        self.assertEqual(self.client.server["bearerToken"], self.server["bearerToken"])

    def test_readback_failure_is_not_success_or_rollback(self):
        self.client.fail_get_at = 2
        self.error("readback_failed")
        self.assertEqual(self.state()["status"], "failed")
        self.assertEqual(len(self.client.updates), 1)
        self.assertEqual(self.client.server["bearerToken"], self.token)
        self.client.fail_get_at = None
        self.assertEqual(self.execute()["outcome"], "unchanged")
        self.assertEqual(len(self.client.updates), 1)

    def test_preserved_field_drift_is_failure(self):
        self.client.drift = True
        self.error("server_fields_changed")
        self.assertEqual(len(self.client.updates), 1)

    def test_lock_excludes_concurrent_attempt_without_state_override(self):
        state = Path(self.config["state_file"])
        with sync.state_lock(state):
            self.error("sync_already_running")
        self.assertFalse(state.exists())
        self.assertEqual(self.client.logins, 0)

    def test_state_failure_before_login_does_not_dispatch(self):
        with patch.object(
            sync,
            "write_state",
            side_effect=sync.SyncError("state_write_failed", "state"),
        ):
            self.error("state_write_failed")
        self.assertEqual(self.client.logins, 0)

    def test_final_state_failure_does_not_report_success(self):
        original = sync.write_state
        calls = []

        def write(path, state):
            calls.append(state["status"])
            if state["status"] == "succeeded":
                sync.fail("state_write_failed", "state")
            original(path, state)

        with patch.object(sync, "write_state", side_effect=write):
            self.error("state_write_failed")
        self.assertEqual(calls, ["running", "succeeded", "failed"])
        self.assertEqual(self.state()["status"], "failed")
        self.assertIsNone(self.state()["last_success"])

    def test_expiry_is_checked_again_after_network(self):
        with self.assertRaises(sync.SyncError) as caught:
            sync.execute(
                self.config,
                client_factory=lambda _: self.client,
                clock=iter([NOW, NOW + 3100]).__next__,
            )
        self.assertEqual(caught.exception.code, "token_constraints_failed")
        self.assertEqual(self.state()["status"], "failed")

    def test_public_secret_file_symlink_or_hardlink_rejected(self):
        path = Path(self.config["token_file"])
        path.chmod(0o644)
        self.error("private_file_invalid")
        path.chmod(0o600)
        linked = self.base / "linked.jwt"
        linked.symlink_to(path)
        self.error("private_file_invalid", lambda: sync.private_path(linked))
        linked.unlink()
        os.link(path, linked)
        self.error("private_file_invalid", lambda: sync.private_path(path))

    def test_shared_parent_or_ancestor_rejected(self):
        for mode in (0o750, 0o770, 0o777):
            with self.subTest(mode=mode):
                self.base.chmod(mode)
                self.error("private_file_invalid")
                self.base.chmod(0o700)
        nested = self.base / "nested"
        nested.mkdir(mode=0o700)
        self.put(nested / "file", "synthetic")
        self.base.chmod(0o720)
        self.error("private_file_invalid", lambda: sync.private_path(nested / "file"))
        self.base.chmod(0o700)

    def test_config_unknown_fields_types_and_remote_http_rejected(self):
        for patch_value in (
            {"extra": "value"},
            {"schema_version": True},
            {"expected_scopes": "admin"},
            {"expected_name": "other-service"},
            {"base_url": "http://example.invalid:80"},
            {"base_url": "http://user:password@127.0.0.1:12008"},
            {"base_url": "http://127.0.0.1:12008/path"},
            {"base_url": "http://127.0.0.1:12008?query=secret"},
            {"state_file": self.config["token_file"]},
        ):
            with self.subTest(patch_value=patch_value):
                self.put(self.config_path, self.config | patch_value)
                self.error(
                    "configuration_invalid", lambda: sync.load_config(self.config_path)
                )

    def test_duplicate_json_keys_rejected(self):
        raw = json.dumps(self.config)
        self.put(self.config_path, raw[:-1] + ', "schema_version": 1}')
        self.error("configuration_invalid", lambda: sync.load_config(self.config_path))

    def test_admin_login_rejects_extra_fields_without_contact(self):
        self.put(
            Path(self.config["admin_login_file"]),
            {
                "email": "operator@example.invalid",
                "password": "synthetic",
                "apiKey": "extra",
            },
        )
        self.error("admin_login_invalid")
        self.assertEqual(self.client.logins, 0)

    def test_main_fixed_failure_output_no_traceback_or_credentials(self):
        stream = io.StringIO()
        with (
            patch.object(
                sync,
                "execute",
                side_effect=RuntimeError("synthetic-secret-DO-NOT-PRINT"),
            ),
            patch("sys.stdout", stream),
        ):
            result = sync.main(["--config", str(self.config_path)])
        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stream.getvalue()),
            {"status": "failed", "error": "sync_failed", "stage": "internal"},
        )


class HTTPClientTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "base_url": "http://127.0.0.1:12008",
            "origin": "http://192.0.2.1:12008",
            "server_uuid": "00000000-0000-4000-8000-000000000001",
        }
        self.client = sync.MetaMCPClient(self.config)

    def test_cookie_domain_is_not_an_auth_redirect_or_printed(self):
        headers = Message()
        headers.add_header(
            "Set-Cookie", "session=synthetic-cookie; Domain=192.0.2.1; HttpOnly; Path=/"
        )
        headers.add_header("Set-Cookie", "session_data=synthetic-data; Path=/")
        with patch.object(self.client, "_request", return_value=({}, headers)):
            self.client.login(
                {"email": "synthetic@example.invalid", "password": "synthetic"}
            )
        self.assertEqual(
            self.client.cookie, "session=synthetic-cookie; session_data=synthetic-data"
        )

    def test_no_cookie_login_is_failure(self):
        with (
            patch.object(self.client, "_request", return_value=({}, Message())),
            self.assertRaises(sync.SyncError) as caught,
        ):
            self.client.login({})
        self.assertEqual(caught.exception.code, "login_failed")

    def test_plain_trpc_payload_and_get_input(self):
        result = {
            "result": {
                "data": {"success": True, "data": {"uuid": self.config["server_uuid"]}}
            }
        }
        with patch.object(
            self.client, "_request", return_value=(result, Message())
        ) as request:
            self.client.get()
            args = request.call_args.args
            self.assertEqual(args[0], "GET")
            parsed = sync.urllib.parse.parse_qs(
                sync.urllib.parse.urlsplit(args[1]).query
            )
            self.assertEqual(
                json.loads(parsed["input"][0]), {"uuid": self.config["server_uuid"]}
            )
            self.client.update(
                {"uuid": self.config["server_uuid"], "bearerToken": "synthetic"}
            )
            self.assertEqual(
                request.call_args.args[2],
                {"uuid": self.config["server_uuid"], "bearerToken": "synthetic"},
            )

    def test_api_error_body_is_not_exposed(self):
        with (
            patch.object(
                self.client,
                "_request",
                return_value=(
                    {
                        "result": {
                            "data": {"success": False, "message": "secret-DO-NOT-PRINT"}
                        }
                    },
                    Message(),
                ),
            ),
            self.assertRaises(sync.SyncError) as caught,
        ):
            self.client.get()
        self.assertEqual(str(caught.exception), "get_failed")

    def test_transport_errors_are_fixed_and_do_not_retry(self):
        with (
            patch.object(
                self.client.opener, "open", side_effect=RuntimeError("secret")
            ) as opening,
            self.assertRaises(sync.SyncError) as caught,
        ):
            self.client.get()
        self.assertEqual(str(caught.exception), "get_failed")
        self.assertEqual(opening.call_count, 1)

    def test_proxy_environment_ignored_and_redirects_refused(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://example.invalid:8888"}):
            client = sync.MetaMCPClient(self.config)
        self.assertFalse(
            any(
                isinstance(h, sync.urllib.request.ProxyHandler) and h.proxies
                for h in client.opener.handlers
            )
        )
        self.assertTrue(
            any(isinstance(h, sync.NoRedirect) for h in client.opener.handlers)
        )
        self.assertIsNone(
            sync.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://example.invalid"
            )
        )


if __name__ == "__main__":
    unittest.main()
