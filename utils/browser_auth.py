import json
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from utils.console import console

HDU_BASE_URL = "https://hdu.huitu.zhishulib.com"
MY_APPOINT_PATH = "/User/Center/myAppoint"
MY_APPOINT_JSON_URL = f"{HDU_BASE_URL}{MY_APPOINT_PATH}?LAB_JSON=1"
HDU_CAS_LOGIN_URL = (
    f"{HDU_BASE_URL}/User/Index/hduCASLogin?forward=%2FUser%2FCenter%2FmyAppoint"
)


@dataclass
class SessionCookieResult:
    cookies: dict[str, str]
    source: str

    @property
    def cookie_header(self) -> str:
        return build_cookie_header(self.cookies)


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    """Parse a browser Cookie header into an httpx-friendly cookie mapping."""
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cookies[name] = value
    return cookies


def build_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def load_cookie_file(path: Path) -> dict[str, str]:
    """Load cookies from either JSON mapping/list or raw Cookie header text."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return parse_cookie_header(text)

    if isinstance(payload, dict):
        return {str(key): str(value) for key, value in payload.items()}

    if isinstance(payload, list):
        cookies: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            domain = str(item.get("domain", ""))
            if name and value is not None and "hdu.huitu.zhishulib.com" in domain:
                cookies[str(name)] = str(value)
        return cookies

    return {}


def prompt_for_cookie_header(open_browser: bool = True) -> SessionCookieResult:
    """Open SSO login page and ask the user to paste the resulting Cookie header."""
    if open_browser:
        webbrowser.open(HDU_CAS_LOGIN_URL)

    console.info("A browser window was opened for HDU unified authentication.")
    console.info("After login, open DevTools on the library page and copy Request Headers > Cookie.")
    cookie_header = input("Paste Cookie header for hdu.huitu.zhishulib.com: ").strip()
    cookies = parse_cookie_header(cookie_header)
    if not cookies:
        raise RuntimeError("No valid cookies were pasted")
    return SessionCookieResult(cookies=cookies, source="manual-cookie")


def browser_login_with_playwright(
    timeout_seconds: int = 300,
    browser_channel: str | None = None,
) -> SessionCookieResult:
    """Let the user log in through a visible browser and capture HDU session cookies."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Browser login requires Playwright. Install it with "
            "`pip install playwright` and use an installed Chrome/Edge browser, "
            "or use `--auth cookie` to paste cookies manually."
        ) from exc

    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None

    with sync_playwright() as playwright:
        browser = None
        channels = [browser_channel] if browser_channel else []
        channels.extend(["msedge", "chrome", None])

        for channel in channels:
            try:
                kwargs = {"headless": False}
                if channel:
                    kwargs["channel"] = channel
                browser = playwright.chromium.launch(**kwargs)
                break
            except Exception as exc:
                last_error = exc

        if browser is None:
            raise RuntimeError(f"Unable to launch a browser: {last_error}")

        context = browser.new_context()
        page = context.new_page()
        page.goto(HDU_CAS_LOGIN_URL, wait_until="domcontentloaded")
        console.info("Please complete HDU login in the opened browser window.")

        try:
            while time.time() < deadline:
                if _context_is_authenticated(context):
                    cookies = _extract_hdu_cookies(context)
                    if cookies:
                        console.success("HDU browser session captured")
                        return SessionCookieResult(cookies=cookies, source="browser")
                page.wait_for_timeout(1500)
        except PlaywrightTimeoutError:
            pass
        finally:
            browser.close()

    raise RuntimeError("Timed out waiting for HDU browser login")


def _context_is_authenticated(context) -> bool:
    response = context.request.get(MY_APPOINT_JSON_URL, timeout=10_000)
    if not response.ok:
        return False

    try:
        payload = response.json()
    except Exception:
        return False

    if not isinstance(payload, dict):
        return False

    href = str(payload.get("href", ""))
    if payload.get("ui_type") == "com.Redirect" and "hduCASLogin" in href:
        return False
    return True


def _extract_hdu_cookies(context) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in context.cookies(HDU_BASE_URL):
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            cookies[str(name)] = str(value)
    return cookies
