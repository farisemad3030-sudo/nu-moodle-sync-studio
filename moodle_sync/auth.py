"""
moodle_sync/auth.py
DNS resolution patching, Playwright Microsoft SSO login, session caching,
and requests.Session pre-seeding.
"""

import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from moodle_sync.config import (
    MOODLE_HOST,
    MOODLE_IP,
    MOODLE_STATIC_IP,
    DNS_MAPPINGS,
    CHROME_DNS_ARGS,
    LOGIN_URL,
    MY_URL,
    SESSION_CACHE_FILE,
    USER_AGENT,
    DEFAULT_HEADERS,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    BACKOFF_FACTOR,
    RETRY_STATUS_CODES,
    get_credentials,
)

logger = logging.getLogger("moodle_sync.auth")

# --- DNS Monkeypatching ---
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_IS_DNS_PATCHED = False

def patch_dns() -> None:
    """
    Monkeypatches socket.getaddrinfo to resolve Moodle to its static IP (68.183.242.170),
    bypassing failing local adapter DNS. Idempotent.
    """
    global _IS_DNS_PATCHED
    if _IS_DNS_PATCHED or getattr(socket.getaddrinfo, "_is_moodle_patched", False):
        return

    orig = _ORIGINAL_GETADDRINFO

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host and isinstance(host, str) and host.lower() == MOODLE_HOST:
            if family == socket.AF_INET6:
                raise socket.gaierror(socket.EAI_NONAME, "No IPv6 address for host")
            target_ip = DNS_MAPPINGS.get(host.lower(), MOODLE_STATIC_IP)
            return orig(target_ip, port, family, type, proto, flags)
        return orig(host, port, family, type, proto, flags)

    patched_getaddrinfo._is_moodle_patched = True
    socket.getaddrinfo = patched_getaddrinfo
    _IS_DNS_PATCHED = True
    logger.debug(f"DNS monkeypatch applied: {DNS_MAPPINGS}")

def unpatch_dns() -> None:
    """Restores original socket.getaddrinfo."""
    global _IS_DNS_PATCHED
    if not _IS_DNS_PATCHED and not getattr(socket.getaddrinfo, "_is_moodle_patched", False):
        return
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    _IS_DNS_PATCHED = False
    logger.debug("DNS monkeypatch removed.")

# --- Session Caching & Validation ---

def save_session_cache(cookies: Dict[str, Any], cache_path: Optional[Path] = None) -> None:
    """Saves session cookies to JSON cache atomically with restricted owner-only file permissions."""
    path = Path(cache_path) if cache_path else SESSION_CACHE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        data = {
            "timestamp": time.time(),
            "cookies": cookies,
        }
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(temp_path, 0o600)
        except Exception:
            pass
        temp_path.replace(path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        logger.info(f"Cached session cookies to {path}")
    except (OSError, PermissionError) as e:
        logger.warning(f"Failed to write session cache to {path}: {e}")

def clear_session_cache(cache_path: Optional[Path] = None) -> bool:
    """Deletes the cached session cookies file from disk."""
    path = Path(cache_path) if cache_path else SESSION_CACHE_FILE
    try:
        if path.exists():
            path.unlink()
            logger.info(f"Deleted cached session file at {path}")
            return True
    except Exception as e:
        logger.warning(f"Failed to delete session cache at {path}: {e}")
    return False

def load_session_cache(cache_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Loads session cookies from JSON cache if present and valid."""
    path = Path(cache_path) if cache_path else SESSION_CACHE_FILE
    if not path.exists():
        return None
    try:
        if path.stat().st_size == 0:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(f"Session cache at {path} is not a valid JSON dictionary.")
            return None
        cookies = data.get("cookies")
        if not isinstance(cookies, dict):
            logger.warning(f"Session cache 'cookies' field at {path} is not a dictionary.")
            return None
        return cookies
    except Exception as e:
        logger.warning(f"Failed to read session cache from {path}: {e}")
        return None

def validate_session(session: requests.Session) -> bool:
    """
    Validates if an existing requests.Session has active authenticated access to Moodle.
    Returns True if valid, False if expired or redirected to login.
    """
    patch_dns()
    try:
        r = session.get(MY_URL, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        if r.status_code == 200:
            return True
        if r.status_code in (301, 302, 303):
            loc = r.headers.get("Location", "").lower()
            if "login" in loc or "oauth" in loc or "microsoft" in loc:
                return False
            # Check follow redirect
            r_follow = session.get(MY_URL, allow_redirects=True, timeout=DEFAULT_TIMEOUT)
            return "login" not in r_follow.url.lower() and r_follow.status_code == 200
    except Exception as e:
        logger.debug(f"Session validation probe failed: {e}")
        return False
    return False

# --- Playwright Headless SSO Authentication ---

def _launch_playwright_browser(p: Any, headless: bool) -> Any:
    """Launch Chrome, falling back to Edge or bundled Chromium across Windows/macOS/Linux."""
    for ch in ("chrome", "msedge", None):
        try:
            if ch:
                return p.chromium.launch(channel=ch, headless=headless, args=CHROME_DNS_ARGS)
            return p.chromium.launch(headless=headless, args=CHROME_DNS_ARGS)
        except Exception:
            if ch is None:
                if not getattr(sys, "frozen", False):
                    try:
                        import subprocess
                        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
                        return p.chromium.launch(headless=headless, args=CHROME_DNS_ARGS)
                    except Exception:
                        pass
                raise
    return p.chromium.launch(headless=headless, args=CHROME_DNS_ARGS)


ACTIVE_EMAIL_SEL = (
    'input[name="loginfmt"]:not(.moveOffScreen):not([aria-hidden="true"]), '
    'input[type="email"]:not(.moveOffScreen):not([aria-hidden="true"])'
)
ACTIVE_PWD_SEL = (
    'input[name="passwd"]:not(.moveOffScreen):not([aria-hidden="true"]), '
    'input[type="password"]:not(.moveOffScreen):not([aria-hidden="true"])'
)


def _fill_active_microsoft_input(page: Any, selector: str, value: str, timeout_ms: int = 20000) -> None:
    """
    Waits for the real, non-offscreen Microsoft Entra ID input (ignoring .moveOffScreen / aria-hidden="true"
    pre-transition clones), fills `value`, verifies Knockout.js did not clear it during animation,
    and clicks the enabled submit button.
    """
    el = page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    time.sleep(0.25)
    el.click()
    el.fill(value)
    # Verify value stuck (Microsoft Knockout transition occasionally clears initial input)
    try:
        if el.input_value() != value:
            time.sleep(0.25)
            el.fill(value)
    except Exception:
        pass
    submit_btn = page.wait_for_selector(
        'input[type="submit"]:not([disabled]), #idSIButton9:not([disabled]), button[type="submit"]:not([disabled])',
        state="visible",
        timeout=10000,
    )
    submit_btn.click()


class AuthenticationError(ValueError):
    """Structured authentication exception carrying error_code and error_field."""

    def __init__(self, message: str, code: str = "AUTH_ERROR", field: str = "general") -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def classify_auth_error(message: str) -> Tuple[str, str]:
    """Classifies an authentication failure string into (error_code, error_field)."""
    low = (message or "").lower()
    if any(k in low for k in (
        "password is incorrect",
        "account or password is incorrect",
        "wrong password",
        "incorrect password",
        "your password is wrong",
        "sign-in was incorrect",
        "enter your password",
    )):
        return "WRONG_PASSWORD", "password"
    if any(k in low for k in (
        "username may be incorrect",
        "couldn't find an account",
        "could not find an account",
        "wrong email",
        "wrong nile university email",
        "does not exist",
        "valid email address",
    )):
        return "WRONG_EMAIL", "email"
    if any(k in low for k in ("mfa", "2fa", "two-factor", "authenticator", "approve sign in")):
        return "MFA_REQUIRED", "general"
    if any(k in low for k in ("email and password are required", "please enter your")):
        if "email" in low and "password" not in low:
            return "MISSING_CREDENTIALS", "email"
        return "MISSING_CREDENTIALS", "password"
    if "timed out" in low or "timeout" in low:
        return "AUTH_TIMEOUT", "general"
    if "password" in low:
        return "WRONG_PASSWORD", "password"
    return "AUTH_ERROR", "general"


def _detect_microsoft_auth_state(page: Any) -> Tuple[str, str]:
    """
    Inspects the current Microsoft / Moodle login page state.
    Returns (state, detail) where state in
    {"LANDED_MOODLE", "KMSI", "PWD_EMPTY_ERR", "PASSWORD_ERROR", "USERNAME_ERROR", "ERROR", "MFA", "PENDING"}.
    """
    url = (page.url or "").lower()
    if "moodle.nu.edu.eg" in url and "login" not in url and "oidc" not in url and "/auth/" not in url:
        return "LANDED_MOODLE", url

    try:
        # Check Moodle's own login error box if bounced back to Moodle /login/
        if "moodle.nu.edu.eg" in url and "login" in url:
            for m_sel in ("#loginerrormessage", ".loginerrors .alert", ".loginerrors"):
                node = page.query_selector(m_sel)
                if node and node.is_visible():
                    txt = (node.inner_text() or "").strip()
                    if txt:
                        return "ERROR", txt

        # Specific Microsoft Entra ID username / password error alerts
        uerr = page.query_selector("#usernameError")
        if uerr and uerr.is_visible():
            txt = (uerr.inner_text() or "").strip()
            if txt:
                return "USERNAME_ERROR", txt

        perr = page.query_selector("#passwordError")
        if perr and perr.is_visible():
            txt = (perr.inner_text() or "").strip()
            if txt:
                if "enter your password" in txt.lower():
                    return "PWD_EMPTY_ERR", txt
                return "PASSWORD_ERROR", txt

        for gen_sel in ("#errorText", "#aadTileError", ".alert-error"):
            node = page.query_selector(gen_sel)
            if node and node.is_visible():
                txt = (node.inner_text() or "").strip()
                if txt:
                    code, _ = classify_auth_error(txt)
                    if code == "WRONG_PASSWORD":
                        return "PASSWORD_ERROR", txt
                    if code == "WRONG_EMAIL":
                        return "USERNAME_ERROR", txt
                    return "ERROR", txt

        # Check MFA / 2FA / Additional verification screens
        for mfa_sel in (
            "#idDiv_SAOTCS_Title",
            "#idDiv_SAOTCC_Title",
            "#idRichContext_DisplaySign",
            "#idTxtBx_OTC_OTCTitle",
            "#idDiv_SAOTCAS_Title",
        ):
            node = page.query_selector(mfa_sel)
            if node and node.is_visible():
                txt = (node.inner_text() or "").strip()
                return "MFA", txt or "Two-factor / Authenticator approval required"

        # Check KMSI ("Stay signed in?") - ensure password input is no longer active
        pwd_active = page.query_selector(ACTIVE_PWD_SEL)
        pwd_visible = bool(pwd_active and pwd_active.is_visible())
        if not pwd_visible:
            kmsi_desc = page.query_selector("#KmsiDescription, #kmsiTitle, input[name='DontShowAgain']")
            no_btn = page.query_selector("#idBtn_Back")
            yes_btn = page.query_selector('input[id="idSIButton9"], button[id="idSIButton9"], #acceptButton')
            if kmsi_desc or (no_btn and no_btn.is_visible() and yes_btn and yes_btn.is_visible()):
                return "KMSI", "Stay signed in?"
            if yes_btn and yes_btn.is_visible():
                btn_val = (yes_btn.get_attribute("value") or yes_btn.inner_text() or "").strip().lower()
                if btn_val in ("yes", "continue", "accept"):
                    return "KMSI", btn_val
    except Exception:
        pass
    return "PENDING", ""


def playwright_login(
    username: Optional[str] = None,
    password: Optional[str] = None,
    headless: bool = True,
    timeout_sec: int = 35,
) -> Dict[str, Any]:
    """
    Performs automated Microsoft Entra ID OIDC SSO login via headless Playwright Chrome.
    Extracts all Moodle and Microsoft tenant cookies.
    """
    from playwright.sync_api import sync_playwright

    user, pwd = get_credentials()
    username = username or user
    password = password or pwd
    if not username or not password:
        raise AuthenticationError(
            "Nile University email and password are required to sign in.",
            code="MISSING_CREDENTIALS",
            field="email" if not username else "password",
        )

    patch_dns()
    logger.info(f"Starting Playwright SSO login for {username} (headless={headless})...")

    cookies_bundle: Dict[str, Any] = {
        "moodle": {},
        "microsoft": {},
        "all": [],
        "account_email": username,
    }

    with sync_playwright() as p:
        browser = _launch_playwright_browser(p, headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        try:
            # Step 1: Navigate to Moodle Login
            logger.debug(f"Navigating to {LOGIN_URL}...")
            page.goto(LOGIN_URL, wait_until="commit", timeout=25000)

            # Fallback: if Moodle displays a local login form with an OpenID Connect button, click it
            start_nav = time.time()
            while time.time() - start_nav < 8:
                if "login.microsoftonline.com" in (page.url or "").lower():
                    break
                if page.query_selector(ACTIVE_EMAIL_SEL):
                    break
                if "moodle.nu.edu.eg/login" in (page.url or "").lower():
                    oidc_btn = page.query_selector('a[href*="auth/oidc"], a[title*="Microsoft"], .login-identityprovider-btn a')
                    if oidc_btn and oidc_btn.is_visible():
                        oidc_btn.click()
                        break
                time.sleep(0.3)

            # Step 2: Email input (ignore pre-rendered .moveOffScreen clones)
            logger.debug("Waiting for active email input field...")
            _fill_active_microsoft_input(page, ACTIVE_EMAIL_SEL, username, timeout_ms=20000)

            # Wait for either active password screen OR immediate username error (avoid 20s wait on wrong email)
            step_wait_start = time.time()
            pwd_field_ready = False
            while time.time() - step_wait_start < 18.0:
                state, detail = _detect_microsoft_auth_state(page)
                if state == "USERNAME_ERROR":
                    raise AuthenticationError(
                        f"Wrong Nile University email address: {detail}",
                        code="WRONG_EMAIL",
                        field="email",
                    )
                if state == "ERROR":
                    code, field = classify_auth_error(detail)
                    raise AuthenticationError(f"Microsoft sign-in failed: {detail}", code=code, field=field)
                if state == "MFA" and headless:
                    raise AuthenticationError(
                        f"Microsoft requested MFA/2FA verification: {detail}",
                        code="MFA_REQUIRED",
                        field="general",
                    )
                if state == "LANDED_MOODLE":
                    pwd_field_ready = True
                    break
                pwd_node = page.query_selector(ACTIVE_PWD_SEL)
                if pwd_node and pwd_node.is_visible():
                    pwd_field_ready = True
                    break
                time.sleep(0.15)

            if not pwd_field_ready:
                raise AuthenticationError(
                    "Timed out waiting for Microsoft password prompt. Please verify your email and connection.",
                    code="AUTH_TIMEOUT",
                    field="email",
                )

            # Step 3: Password input (wait until active password field slides in, NOT .moveOffScreen)
            state_now, _ = _detect_microsoft_auth_state(page)
            if state_now != "LANDED_MOODLE":
                logger.debug("Waiting for active password input field...")
                _fill_active_microsoft_input(page, ACTIVE_PWD_SEL, password, timeout_ms=10000)

            # Step 4 & 5: Monitor KMSI, empty-password animation race retry, auth errors, MFA, and Moodle landing
            logger.debug("Waiting for landing on Moodle domain...")
            start_time = time.time()
            landed = False
            pwd_retried = False
            kmsi_clicked = False

            while time.time() - start_time < timeout_sec:
                state, detail = _detect_microsoft_auth_state(page)
                if state == "LANDED_MOODLE":
                    landed = True
                    break
                if state == "PWD_EMPTY_ERR" and not pwd_retried:
                    pwd_retried = True
                    logger.warning("Microsoft animation cleared password input; retrying password entry once...")
                    _fill_active_microsoft_input(page, ACTIVE_PWD_SEL, password, timeout_ms=10000)
                    time.sleep(0.5)
                    continue
                if state == "PASSWORD_ERROR":
                    raise AuthenticationError(
                        f"Incorrect password or account: {detail}",
                        code="WRONG_PASSWORD",
                        field="password",
                    )
                if state == "USERNAME_ERROR":
                    raise AuthenticationError(
                        f"Wrong Nile University email address: {detail}",
                        code="WRONG_EMAIL",
                        field="email",
                    )
                if state == "ERROR":
                    code, field = classify_auth_error(detail)
                    raise AuthenticationError(f"Microsoft auth failed: {detail}", code=code, field=field)
                if state == "MFA":
                    if headless:
                        raise AuthenticationError(
                            f"Microsoft requested MFA/2FA verification: {detail}",
                            code="MFA_REQUIRED",
                            field="general",
                        )
                if state == "KMSI" and not kmsi_clicked:
                    try:
                        yes_btn = page.query_selector('input[id="idSIButton9"], button[id="idSIButton9"], #acceptButton')
                        if yes_btn and yes_btn.is_visible():
                            yes_btn.click()
                            kmsi_clicked = True
                            logger.debug("Clicked KMSI confirmation button (#idSIButton9).")
                    except Exception:
                        pass
                time.sleep(0.3)

            if not landed:
                state, detail = _detect_microsoft_auth_state(page)
                if state == "PASSWORD_ERROR":
                    raise AuthenticationError(f"Incorrect password or account: {detail}", code="WRONG_PASSWORD", field="password")
                if state == "USERNAME_ERROR":
                    raise AuthenticationError(f"Wrong Nile University email address: {detail}", code="WRONG_EMAIL", field="email")
                if state in ("ERROR", "PWD_EMPTY_ERR", "MFA"):
                    code, field = classify_auth_error(detail)
                    raise AuthenticationError(f"Microsoft auth failed ({state}): {detail}", code=code, field=field)
                raise TimeoutError(f"Login timed out after {timeout_sec}s; final URL: {page.url}")

            logger.info(f"Successfully landed on Moodle! Landing URL: {page.url}")

            # Extract all cookies
            raw_cookies = context.cookies()
        finally:
            browser.close()

    cookies_bundle["all"] = raw_cookies
    for c in raw_cookies:
        name = c["name"]
        val = c["value"]
        dom = c.get("domain", "")
        if "moodle" in dom:
            cookies_bundle["moodle"][name] = val
        if any(k in dom for k in ["microsoft", "live", "sharepoint"]):
            cookies_bundle["microsoft"][name] = val

    if "MoodleSession" not in cookies_bundle["moodle"]:
        raise ValueError("Login completed but MoodleSession cookie was not found in browser context.")

    save_session_cache(cookies_bundle)
    return cookies_bundle

# --- requests.Session Pre-seeding ---

def create_requests_session(cookies_bundle: Dict[str, Any]) -> requests.Session:
    """
    Initializes a high-performance requests.Session pre-seeded with retry adapters,
    headers, Moodle auth cookies, and attaches Microsoft tenant cookies.
    """
    patch_dns()
    session = requests.Session()

    # Mount retry adapter
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=list(RETRY_STATUS_CODES),
        allowed_methods=["HEAD", "GET", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Set headers
    session.headers.update(DEFAULT_HEADERS)

    # Pre-seed all domain cookies first (preserving exact domain/path attributes for Moodle & SharePoint)
    seeded_names = set()
    raw_cookies = cookies_bundle.get("all", [])
    if isinstance(raw_cookies, list):
        for c in raw_cookies:
            if isinstance(c, dict) and "name" in c and "value" in c:
                session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", ""),
                    path=c.get("path", "/"),
                )
                seeded_names.add(c["name"])

    # Fallback: seed Moodle dict cookies only if not already seeded from full cookie list
    moodle_cookies = cookies_bundle.get("moodle", {})
    if isinstance(moodle_cookies, dict):
        for k, v in moodle_cookies.items():
            if k not in seeded_names:
                session.cookies.set(k, v)

    # Attach complete cookie bundle to session attribute for media resolvers (SharePoint)
    session.auth_cookies = cookies_bundle  # type: ignore

    return session

def login(
    username: Optional[str] = None,
    password: Optional[str] = None,
    headless: bool = True,
    force_fresh: bool = False,
) -> requests.Session:
    """
    Top-level authentication factory. Checks session cache first.
    If valid, returns authenticated requests.Session immediately.
    If invalid or force_fresh, executes Playwright login, refreshes cache, and returns session.
    """
    patch_dns()

    if not force_fresh:
        cached = load_session_cache()
        cached_email = (cached.get("account_email") or "").strip().lower() if isinstance(cached, dict) else ""
        same_user = (not username) or (not cached_email) or (username.strip().lower() == cached_email)
        if same_user and isinstance(cached, dict) and "moodle" in cached and isinstance(cached.get("moodle"), dict):
            s = create_requests_session(cached)
            if validate_session(s):
                logger.info("Reusing existing cached Moodle session (instant login).")
                if username:
                    s._runtime_username = username  # type: ignore
                if password:
                    s._runtime_password = password  # type: ignore
                return s
            else:
                logger.info("Cached session expired. Initiating fresh login...")
        elif not same_user:
            logger.info(f"Switching account from {cached_email} to {username}; performing fresh login...")

    bundle = playwright_login(username=username, password=password, headless=headless)
    s = create_requests_session(bundle)
    if username:
        s._runtime_username = username  # type: ignore
    if password:
        s._runtime_password = password  # type: ignore
    return s

def get_session(
    username: Optional[str] = None,
    password: Optional[str] = None,
    headless: bool = True,
) -> requests.Session:
    """Alias for login()."""
    return login(username=username, password=password, headless=headless)

def get_auth_cookies() -> Dict[str, Any]:
    """Retrieve current authentication cookies bundle."""
    cached = load_session_cache()
    if isinstance(cached, dict) and "moodle" in cached and isinstance(cached.get("moodle"), dict):
        return cached
    return playwright_login()


def ensure_sharepoint_session(
    session: requests.Session,
    target_url: str,
    headless: bool = True,
) -> Optional[str]:
    """
    Uses Playwright headless Chrome with the user's cached Microsoft SSO cookies to open
    a SharePoint/OneDrive URL, automatically minting FedAuth / rtFa cookies for that
    SharePoint host (e.g. nileuniversity-my.sharepoint.com or nileuniversity.sharepoint.com),
    attaching them to `session`, saving them to `.moodle_session.json`, and returning
    the final resolved SharePoint URL (which for /:f:/ folders contains listurl & id).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    env_user, env_pwd = get_credentials()
    user = getattr(session, "_runtime_username", None) or env_user
    pwd = getattr(session, "_runtime_password", None) or env_pwd
    auth_bundle = getattr(session, "auth_cookies", None) or load_session_cache() or {
        "moodle": {},
        "microsoft": {},
        "all": [],
    }

    final_url: Optional[str] = None
    new_cookies: List[Dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            browser = _launch_playwright_browser(p, headless=headless)
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )
            existing_all = auth_bundle.get("all", [])
            if isinstance(existing_all, list) and existing_all:
                try:
                    context.add_cookies(existing_all)
                except Exception:
                    pass

            page = context.new_page()
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
                time.sleep(2.5)

                if "login.microsoftonline.com" in page.url:
                    try:
                        email_el = page.query_selector(ACTIVE_EMAIL_SEL)
                        if email_el and email_el.is_visible() and user:
                            _fill_active_microsoft_input(page, ACTIVE_EMAIL_SEL, user, timeout_ms=6000)
                            time.sleep(1.0)
                    except Exception:
                        pass

                    try:
                        pwd_el = page.query_selector(ACTIVE_PWD_SEL) or page.wait_for_selector(
                            ACTIVE_PWD_SEL, state="visible", timeout=6000
                        )
                        if pwd_el and pwd_el.is_visible() and pwd:
                            _fill_active_microsoft_input(page, ACTIVE_PWD_SEL, pwd, timeout_ms=6000)
                            time.sleep(1.0)
                    except Exception:
                        pass

                    try:
                        kmsi_el = page.wait_for_selector(
                            'input[id="idSIButton9"]:not([disabled]), button[id="idSIButton9"]:not([disabled])',
                            state="visible",
                            timeout=4000,
                        )
                        state, _ = _detect_microsoft_auth_state(page)
                        if kmsi_el and kmsi_el.is_visible() and state == "KMSI":
                            kmsi_el.click()
                    except Exception:
                        pass

                    start_t = time.time()
                    while time.time() - start_t < 15:
                        if "sharepoint.com" in page.url and "login.microsoftonline.com" not in page.url:
                            break
                        time.sleep(0.5)
                    time.sleep(2.0)

                final_url = page.url
                new_cookies = context.cookies()
            finally:
                browser.close()
    except Exception as e:
        logger.warning(f"Playwright SharePoint session acquisition failed for {target_url}: {e}")
        return None

    if new_cookies:
        # Merge cookies into session and auth_bundle
        by_key = {}
        for c in (auth_bundle.get("all") or []):
            if isinstance(c, dict) and "name" in c:
                by_key[(c.get("domain", ""), c["name"])] = c
        for c in new_cookies:
            if isinstance(c, dict) and "name" in c and "value" in c:
                by_key[(c.get("domain", ""), c["name"])] = c
                session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", ""),
                    path=c.get("path", "/"),
                )
                dom = c.get("domain", "").lower()
                if any(k in dom for k in ["microsoft", "live", "sharepoint"]):
                    auth_bundle.setdefault("microsoft", {})[c["name"]] = c["value"]

        auth_bundle["all"] = list(by_key.values())
        session.auth_cookies = auth_bundle  # type: ignore
        save_session_cache(auth_bundle)

    return final_url

