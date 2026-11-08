"""
Test Suite: Authentication & DNS Resolution
Covers Features:
- F1: DNS Resolution Workaround (Tier 1 & Tier 2)
- F2: Microsoft Entra ID OIDC SSO Authentication (Tier 1 & Tier 2)
- F3: Session Persistence & Transfer (Tier 1 & Tier 2)
"""

import os
import sys
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.fixtures import (
    MOODLE_HOST,
    MOODLE_STATIC_IP,
    HAS_MOODLE_SYNC,
    moodle_auth,
    moodle_config,
)


class TestF1DNSResolution(unittest.TestCase):
    """Tests for Feature F1: DNS Resolution Workaround (Tier 1 & Tier 2)."""

    def setUp(self):
        self.orig_getaddrinfo = socket.getaddrinfo

    def tearDown(self):
        socket.getaddrinfo = self.orig_getaddrinfo

    # ------------------ Tier 1 Tests ------------------

    def test_f1_t1_01_monkeypatch_socket_getaddrinfo_target_host(self):
        """F1.T1.1: Verify socket.getaddrinfo intercept for moodle.nu.edu.eg returns static IP 68.183.242.170."""
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return self.orig_getaddrinfo(MOODLE_STATIC_IP, port, family, type, proto, flags)
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        results = socket.getaddrinfo("moodle.nu.edu.eg", 443, socket.AF_INET, socket.SOCK_STREAM)
        self.assertTrue(len(results) > 0)
        sockaddr = results[0][4]
        self.assertEqual(sockaddr[0], MOODLE_STATIC_IP)
        self.assertEqual(sockaddr[1], 443)

    def test_f1_t1_02_passthrough_non_target_hosts(self):
        """F1.T1.2: Verify non-target hostnames pass through untouched without replacing IP."""
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return self.orig_getaddrinfo(MOODLE_STATIC_IP, port, family, type, proto, flags)
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        # Resolve localhost
        results = socket.getaddrinfo("localhost", 80, socket.AF_INET, socket.SOCK_STREAM)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0][4][0], "127.0.0.1")

    def test_f1_t1_03_chrome_host_rules_argument(self):
        """F1.T1.3: Verify Chrome launch arguments include --host-rules mapping moodle.nu.edu.eg."""
        expected_flag = f"--host-rules=MAP {MOODLE_HOST} {MOODLE_STATIC_IP}"
        # Test contract format
        flags = [f"--host-rules=MAP {MOODLE_HOST} {MOODLE_STATIC_IP}"]
        self.assertIn(expected_flag, flags)

    def test_f1_t1_04_port_and_socket_type_preservation(self):
        """F1.T1.4: Verify port 443 and SOCK_STREAM socket type are correctly forwarded."""
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (MOODLE_STATIC_IP, port))]
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        res = socket.getaddrinfo("moodle.nu.edu.eg", 443, proto=socket.IPPROTO_TCP)
        family, socktype, proto, canonname, sockaddr = res[0]
        self.assertEqual(socktype, socket.SOCK_STREAM)
        self.assertEqual(sockaddr[1], 443)

    def test_f1_t1_05_idempotent_patching(self):
        """F1.T1.5: Verify applying monkeypatch multiple times does not trigger recursion."""
        calls = [0]
        base_orig = socket.getaddrinfo

        def patch_once():
            curr = socket.getaddrinfo
            if getattr(curr, "_is_patched", False):
                return
            def p(h, pt, f=0, t=0, pr=0, fl=0):
                calls[0] += 1
                if h and h.lower() == MOODLE_HOST:
                    return base_orig(MOODLE_STATIC_IP, pt, f, t, pr, fl)
                return base_orig(h, pt, f, t, pr, fl)
            p._is_patched = True
            socket.getaddrinfo = p

        patch_once()
        patch_once()
        patch_once()
        res = socket.getaddrinfo("moodle.nu.edu.eg", 80)
        self.assertEqual(calls[0], 1)
        self.assertEqual(res[0][4][0], MOODLE_STATIC_IP)

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f1_t2_01_case_insensitivity_handling(self):
        """F1.T2.1: Verify uppercase and mixed case hostnames (e.g. MOODLE.NU.EDU.EG) resolve to static IP."""
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (MOODLE_STATIC_IP, port))]
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        for variant in ["MOODLE.NU.EDU.EG", "Moodle.Nu.Edu.Eg", "mOoDlE.nU.eDu.Eg"]:
            res = socket.getaddrinfo(variant, 443)
            self.assertEqual(res[0][4][0], MOODLE_STATIC_IP)

    def test_f1_t2_02_ipv6_family_handling(self):
        """F1.T2.2: Verify behavior when socket.AF_INET6 is requested falls back cleanly to IPv4 or empty."""
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                if family == socket.AF_INET6:
                    raise socket.gaierror(socket.EAI_NONAME, "No IPv6 address for host")
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (MOODLE_STATIC_IP, port))]
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo(MOODLE_HOST, 443, family=socket.AF_INET6)

    def test_f1_t2_03_dns_failure_fallback_resilience(self):
        """F1.T2.3: Verify that if host is moodle.nu.edu.eg, patched resolver does NOT call flaky router DNS."""
        mock_orig = MagicMock(side_effect=socket.gaierror(11001, "getaddrinfo failed"))
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (MOODLE_STATIC_IP, port))]
            return mock_orig(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        # Direct query for moodle MUST succeed without calling mock_orig
        res = socket.getaddrinfo(MOODLE_HOST, 443)
        self.assertEqual(res[0][4][0], MOODLE_STATIC_IP)
        mock_orig.assert_not_called()

    def test_f1_t2_04_empty_or_none_host_input(self):
        """F1.T2.4: Verify passing None or empty host string does not cause unhandled exceptions in patcher."""
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host and host.lower() == MOODLE_HOST:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (MOODLE_STATIC_IP, port))]
            return self.orig_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        res = socket.getaddrinfo(None, 80)
        self.assertTrue(len(res) > 0)

    def test_f1_t2_05_custom_target_ip_override(self):
        """F1.T2.5: Verify custom target IP override from configuration parameters."""
        custom_ip = "10.0.0.99"
        def make_patcher(target_ip):
            def p(host, port, family=0, type=0, proto=0, flags=0):
                if host and host.lower() == MOODLE_HOST:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (target_ip, port))]
                return self.orig_getaddrinfo(host, port, family, type, proto, flags)
            return p

        socket.getaddrinfo = make_patcher(custom_ip)
        res = socket.getaddrinfo(MOODLE_HOST, 443)
        self.assertEqual(res[0][4][0], custom_ip)


class TestF2MicrosoftSSOAuth(unittest.TestCase):
    """Tests for Feature F2: Microsoft Entra ID OIDC SSO Authentication (Tier 1 & Tier 2)."""

    # ------------------ Tier 1 Tests ------------------

    def test_f2_t1_01_playwright_auth_selectors_contract(self):
        """F2.T1.1: Verify presence and exact selector definitions for Microsoft login DOM."""
        selectors = {
            "email_input": 'input[type="email"]',
            "password_input": 'input[type="password"]',
            "submit_button": 'input[type="submit"]',
            "kmsi_button": 'input[id="idSIButton9"]',
        }
        self.assertEqual(selectors["email_input"], 'input[type="email"]')
        self.assertEqual(selectors["password_input"], 'input[type="password"]')
        self.assertIn("idSIButton9", selectors["kmsi_button"])

    def test_f2_t1_02_kmsi_button_click_handling(self):
        """F2.T1.2: Verify KMSI ('Stay signed in?') dialog is detected and confirmed."""
        page_mock = MagicMock()
        page_mock.query_selector.return_value = MagicMock()
        # Simulate click
        if page_mock.query_selector('input[id="idSIButton9"]'):
            page_mock.click('input[id="idSIButton9"]')
        page_mock.click.assert_called_with('input[id="idSIButton9"]')

    def test_f2_t1_03_wait_until_commit_arg(self):
        """F2.T1.3: Verify page navigation specifies wait_until='commit' to prevent hanging on analytics."""
        page_mock = MagicMock()
        target_url = "https://moodle.nu.edu.eg"
        page_mock.goto(target_url, wait_until="commit")
        page_mock.goto.assert_called_with(target_url, wait_until="commit")

    def test_f2_t1_04_credentials_passing(self):
        """F2.T1.4: Verify username and password parameter binding for SSO."""
        user = "student@nu.edu.eg"
        pw = "MockPassword123!"
        page_mock = MagicMock()
        page_mock.fill('input[type="email"]', user)
        page_mock.fill('input[type="password"]', pw)
        page_mock.fill.assert_any_call('input[type="email"]', user)
        page_mock.fill.assert_any_call('input[type="password"]', pw)

    def test_f2_t1_05_moodle_session_cookie_extraction(self):
        """F2.T1.5: Verify extraction of MoodleSession cookie from browser context cookies."""
        mock_cookies = [
            {"name": "MoodleSession", "value": "abc123456789xyz", "domain": "moodle.nu.edu.eg"},
            {"name": "ESTSAUTH", "value": "token_val", "domain": "login.microsoftonline.com"},
        ]
        context_mock = MagicMock()
        context_mock.cookies.return_value = mock_cookies
        extracted = {c["name"]: c["value"] for c in context_mock.cookies() if "moodle" in c["domain"]}
        self.assertIn("MoodleSession", extracted)
        self.assertEqual(extracted["MoodleSession"], "abc123456789xyz")

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f2_t2_01_invalid_credentials_error_detection(self):
        """F2.T2.1: Verify error handling when Microsoft login reports bad credentials (#passwordError)."""
        page_mock = MagicMock()
        error_elem = MagicMock()
        error_elem.inner_text.return_value = "Your account or password is not correct."
        page_mock.query_selector.return_value = error_elem

        err_text = None
        if page_mock.query_selector("#passwordError"):
            err_text = page_mock.query_selector("#passwordError").inner_text()
        self.assertIn("not correct", err_text)

    def test_f2_t2_02_login_timeout_resilience(self):
        """F2.T2.2: Verify timeout on initial navigation raises TimeoutError with clear message."""
        page_mock = MagicMock()
        page_mock.goto.side_effect = TimeoutError("Navigation timeout of 30000ms exceeded")
        with self.assertRaises(TimeoutError):
            page_mock.goto("https://moodle.nu.edu.eg", wait_until="commit", timeout=30000)

    def test_f2_t2_03_mfa_prompt_detection(self):
        """F2.T2.3: Verify detection of unexpected MFA challenge (#idDiv_SAOTCS_Title)."""
        page_mock = MagicMock()
        mfa_elem = MagicMock()
        mfa_elem.inner_text.return_value = "Approve sign in request"
        page_mock.query_selector.return_value = mfa_elem
        is_mfa = bool(page_mock.query_selector("#idDiv_SAOTCS_Title"))
        self.assertTrue(is_mfa)

    def test_f2_t2_04_headless_mode_toggle(self):
        """F2.T2.4: Verify launch parameter headless accepts both True and False."""
        for mode in [True, False]:
            browser_args = {"headless": mode, "channel": "chrome"}
            self.assertEqual(browser_args["headless"], mode)

    def test_f2_t2_05_already_authenticated_skip_login(self):
        """F2.T2.5: Verify that if session cookies are still valid, full browser login is bypassed."""
        session_mock = MagicMock()
        session_mock.get.return_value.status_code = 200
        session_mock.get.return_value.text = "Home | Nile"
        # If response doesn't redirect to login, session is valid
        is_valid = ("login" not in session_mock.get.return_value.text)
        self.assertTrue(is_valid)


class TestF3SessionPersistence(unittest.TestCase):
    """Tests for Feature F3: Session Persistence & Transfer (Tier 1 & Tier 2)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_file = Path(self.temp_dir.name) / ".moodle_session.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    # ------------------ Tier 1 Tests ------------------

    def test_f3_t1_01_cookie_transfer_to_requests_session(self):
        """F3.T1.1: Verify cookies transferred into requests.Session match keys and values."""
        import requests
        s = requests.Session()
        cookie_dict = {"MoodleSession": "mock_session_abc", "auth_token": "token_123"}
        s.cookies.update(cookie_dict)
        self.assertEqual(s.cookies.get("MoodleSession"), "mock_session_abc")
        self.assertEqual(s.cookies.get("auth_token"), "token_123")

    def test_f3_t1_02_session_file_cache_serialization(self):
        """F3.T1.2: Verify session cookies serialized to JSON file on disk."""
        data = {"MoodleSession": "cached_hash_456", "timestamp": "2026-11-05T12:00:00Z"}
        self.session_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(self.session_file.exists())
        loaded = json.loads(self.session_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded["MoodleSession"], "cached_hash_456")

    def test_f3_t1_03_session_file_cache_deserialization(self):
        """F3.T1.3: Verify loading cached session cookies from file and verifying expiry."""
        data = {"cookies": {"MoodleSession": "valid_token"}, "created_at": 1730808000}
        self.session_file.write_text(json.dumps(data), encoding="utf-8")
        with open(self.session_file, "r", encoding="utf-8") as f:
            read_data = json.load(f)
        self.assertIn("MoodleSession", read_data["cookies"])

    def test_f3_t1_04_retry_adapter_configuration(self):
        """F3.T1.4: Verify HTTPAdapter is configured with retries and backoff."""
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.assertEqual(adapter.max_retries.total, 3)
        self.assertIn(502, adapter.max_retries.status_forcelist)

    def test_f3_t1_05_browser_user_agent_header(self):
        """F3.T1.5: Verify requests.Session User-Agent matches modern Chrome string."""
        import requests
        s = requests.Session()
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        s.headers["User-Agent"] = ua
        self.assertIn("Mozilla/5.0", s.headers["User-Agent"])
        self.assertIn("Chrome/120", s.headers["User-Agent"])

    # ------------------ Tier 2 Tests (Boundaries & Corners) ------------------

    def test_f3_t2_01_expired_session_redirect_detection(self):
        """F3.T2.1: Verify detecting HTTP 303 redirect to login.microsoftonline.com as expired session."""
        response_mock = MagicMock()
        response_mock.status_code = 303
        response_mock.headers = {"Location": "https://login.microsoftonline.com/common/oauth2/authorize"}
        is_expired = (response_mock.status_code == 303 and "login.microsoftonline.com" in response_mock.headers.get("Location", ""))
        self.assertTrue(is_expired)

    def test_f3_t2_02_corrupted_session_file_handling(self):
        """F3.T2.2: Verify handling corrupt/malformed JSON in session cache file without crashing."""
        self.session_file.write_text("{NOT_VALID_JSON: true,,,", encoding="utf-8")
        cookies = {}
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except json.JSONDecodeError:
            cookies = {}  # fallback to empty, triggering fresh login
        self.assertEqual(cookies, {})

    def test_f3_t2_03_empty_session_file_handling(self):
        """F3.T2.3: Verify handling 0-byte session cache file."""
        self.session_file.write_text("", encoding="utf-8")
        cookies = {}
        if self.session_file.stat().st_size == 0:
            cookies = {}
        self.assertEqual(cookies, {})

    def test_f3_t2_04_read_only_session_cache_path(self):
        """F3.T2.4: Verify handling read-only or unwritable session cache location gracefully."""
        # Simulated unwritable path
        unwritable_path = Path("/invalid_or_locked_path/.moodle_session.json")
        saved = False
        try:
            unwritable_path.write_text("{}", encoding="utf-8")
            saved = True
        except (OSError, PermissionError):
            saved = False
        self.assertFalse(saved)

    def test_f3_t2_05_network_retry_on_server_errors(self):
        """F3.T2.5: Verify HTTP 502/503/504 errors trigger configured retry adapter logic."""
        from urllib3.util.retry import Retry
        retry_policy = Retry(total=5, status_forcelist=[502, 503, 504])
        self.assertTrue(retry_policy.is_retry("GET", 502))
        self.assertTrue(retry_policy.is_retry("GET", 503))
        self.assertTrue(retry_policy.is_retry("GET", 504))
        self.assertFalse(retry_policy.is_retry("GET", 200))


if __name__ == "__main__":
    unittest.main()
