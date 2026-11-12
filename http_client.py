"""Rate limited HTTP transport with an optional browser fallback."""

import logging
import time
from pathlib import Path
from urllib.parse import urlsplit
from dataclasses import dataclass

import requests
from events import emit, check_stop

log = logging.getLogger(__name__)


@dataclass
class Response:
    url: str
    content: bytes
    status_code: int = 200

    @property
    def text(self):
        return self.content.decode("utf-8-sig", errors="replace")


class HTTPError(RuntimeError):
    def __init__(self, url, status):
        self.status = status
        super().__init__(f"HTTP {status}: {url}")


def blocked(html):
    """Detect challenge pages even if the server returned HTTP 200."""
    lower = html[:15000].lower()
    return any(s in lower for s in ("<title>just a moment", "<title>access denied", "cf-chl-", "<title>attention required",
                                    "przeprowadzanie weryfikacji zabezpieczeń", "ta witryna jest chroniona przed złośliwymi botami"))


class Client:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        # Opcjonalny transport jest już używany przez stare scrapery.
        try:
            from curl_cffi import requests as curl_requests
            self.session = curl_requests.Session(impersonate="chrome")
        except ImportError:
            pass
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
        })
        self.next_request = 0.0
        self.playwright = self.browser = self.context = self.page = None
        self.cookies_checked = False
        self.cookie_status = "not_checked"
        self.access_denied_hosts = set()

    def throttle(self):
        check_stop()
        time.sleep(max(0, self.next_request - time.monotonic()))
        check_stop()
        self.next_request = time.monotonic() + 1 / self.settings["requests_per_second"]

    def get(self, url, browser_fallback=False, render=False, timeout=None, retries=None):
        """Retry transient errors; never interpret failures as an empty catalog."""
        host = (urlsplit(url).hostname or '').removeprefix('www.')
        if host in self.access_denied_hosts:
            raise HTTPError(url, 403)
        publio = host == 'publio.pl'
        attempts = retries if retries is not None else self.settings['http_retries']
        browser_fallback = browser_fallback or publio
        if render:
            for attempt in range(attempts):
                try:
                    return self.browser_get(url)
                except HTTPError as exc:
                    if exc.status not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                        raise
                    wait = min(60, 30 * (attempt + 1)) if exc.status == 429 else 2 ** (attempt + 1)
                    log.warning("%s: HTTP %s w przeglądarce, ponowienie za %ss", url, exc.status, wait)
                    for _ in range(wait):
                        check_stop()
                        time.sleep(1)
                except Exception as exc:
                    transient = any(word in str(exc) for word in ('Timeout', 'ERR_TIMED_OUT', 'ERR_CONNECTION_RESET'))
                    if not transient or attempt + 1 == attempts:
                        raise
                    log.warning('Przeglądarka: ponawiam nieudaną nawigację %s', url)
        for attempt in range(attempts):
            self.throttle()
            try:
                response = self.session.get(url, timeout=timeout if timeout is not None else self.settings["timeout"])
                status = response.status_code
                if status == 404 or status == 410:
                    raise HTTPError(url, status)
                if browser_fallback and (status == 403 or blocked(response.text)):
                    return self.browser_get(url)
                if status == 429 or status >= 500:
                    if status == 500 and host == 'woblink.com' and urlsplit(url).path.startswith('/audiobook/--'):
                        raise HTTPError(url, status)
                    retry_after = response.headers.get("Retry-After", "")
                    wait = min(60, float(retry_after)) if retry_after.isdigit() else min(30, 2 ** (attempt + 1))
                    if attempt + 1 == attempts:
                        raise HTTPError(url, status)
                    log.warning("%s: HTTP %s, ponowienie za %ss", url, status, wait)
                    time.sleep(wait)
                    continue
                if status != 200:
                    raise HTTPError(url, status)
                if blocked(response.text):
                    raise RuntimeError(f"Strona blokady zamiast danych: {url}")
                return Response(str(response.url), response.content, status)
            except HTTPError:
                raise
            except Exception:
                if attempt + 1 == attempts:
                    raise
                time.sleep(min(30, 2 ** (attempt + 1)))

    def get_optional(self, url):
        """Bound optional review requests so they cannot stall bibliographic metadata."""
        return self.get(url, timeout=self.settings.get('review_timeout', 8), retries=1)

    def browser_get(self, url):
        """Reuse a browser profile, dismiss consent and wait for dynamic details."""
        self.throttle()
        publio = (urlsplit(url).hostname or '').removeprefix('www.') == 'publio.pl'
        if self.playwright is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError("Empik wymaga Playwrighta: uruchom install.bat.") from exc
            self.playwright = sync_playwright().start()
            try:
                profile = self.settings.get("browser_profile")
                if publio and profile:
                    profile = Path(profile).with_name('browser_profile_publio')
                if profile:
                    self.context = self.playwright.chromium.launch_persistent_context(str(profile),
                        headless=self.settings["browser_headless"], channel="chromium", locale="pl-PL",
                        viewport={"width": 1440, "height": 1000})
                else:
                    self.browser = self.playwright.chromium.launch(headless=self.settings["browser_headless"], channel="chromium")
                    self.context = self.browser.new_context(locale="pl-PL", viewport={"width": 1440, "height": 1000})
                self.page = self.context.new_page()
            except Exception:
                self.close_browser()
                raise
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.settings["timeout"] * 1000)
        if response and response.status in (404, 410):
            raise HTTPError(url, response.status)
        try:
            self.page.wait_for_function("!document.title.toLowerCase().includes('just a moment')", timeout=10000)
        except Exception:
            pass
        html = self.page.content()
        if publio:
            try:
                self.page.locator('h1, .details-element, .product-tile').first.wait_for(state='attached', timeout=10000)
            except Exception:
                pass
            html = self.page.content()
            if blocked(html) or response and response.status >= 400:
                self.access_denied_hosts.add('publio.pl')
                raise HTTPError(url, response.status if response and response.status >= 400 else 403)
            self.session.cookies.update({cookie['name']: cookie['value'] for cookie in self.context.cookies()})
            return Response(self.page.url, html.encode('utf-8'))
        if blocked(html):
            raise RuntimeError(f"Empik zwrócił blokadę także w przeglądarce: {url}")
        if response and response.status >= 400 and not self.page.locator('h1[data-ta="title"], .search-content').count():
            raise HTTPError(url, response.status)
        self.dismiss_consent()
        emit("browser", url=url, cookies=self.cookie_status, visible=not self.settings["browser_headless"])
        if "ebooki-i-mp3-p" in url:
            self.page.locator('h1[data-ta="title"]').first.wait_for(state="visible", timeout=15000)
            for _ in range(5):
                self.page.evaluate("window.scrollBy(0, 900)")
                self.page.wait_for_timeout(400)
            self.dismiss_consent()
            for selector in ('button[data-ta="expand-details-btn"]', 'button[data-ta="expand-description-btn"]'):
                button = self.page.locator(selector)
                if button.count():
                    try:
                        button.first.click(timeout=2000)
                    except Exception:
                        # Okno zgody bywa doładowywane dopiero po przewinięciu.
                        self.dismiss_consent()
                        button.first.click(timeout=3000)
            try:
                self.page.locator('tr[data-ta="row"], [class*="labelWrapper"] [data-ta="attribute-value"]').first.wait_for(state="attached", timeout=15000)
            except Exception:
                log.warning("Empik: brak tabeli szczegółów; sprawdzam pozostałe dane: %s", url)
            self.page.wait_for_timeout(800)
        else:
            try:
                self.page.locator('a[href*="ebooki-i-mp3-p"]').first.wait_for(timeout=10000)
            except Exception:
                pass
            self.dismiss_consent()
        return Response(self.page.url, self.page.content().encode("utf-8"))

    def dismiss_consent(self):
        """Dismiss visible consent buttons, including delayed and repeated Empik dialogs."""
        accept = self.page.locator('button[data-ta="cookie-btn-accept-all"]:visible')
        if not self.cookies_checked:
            try:
                accept.first.wait_for(state="visible", timeout=10000)
            except Exception:
                pass
        accepted = False
        for _ in range(3):
            if not accept.count():
                break
            accept.first.click(timeout=5000)
            self.page.wait_for_timeout(500)
            accepted = True
        if accept.count():
            raise RuntimeError("Empik: nie udało się zamknąć okna zgody; nie uznaję strony za w pełni odczytaną")
        if accepted:
            self.cookie_status = "accepted"
            log.info("Empik: zaakceptowano cookies, okno zniknęło")
        elif not self.cookies_checked:
            self.cookie_status = "not_present"
            log.info("Empik: brak okna cookies (zgoda może być zapisana w profilu)")
        self.cookies_checked = True

    def post_catalog(self, url, data):
        """Read the public LC filtered catalog using its documented-in-page POST form."""
        for attempt in range(self.settings["http_retries"]):
            self.throttle()
            try:
                response = self.session.post(url, data=data, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=self.settings["timeout"])
            except Exception:
                if attempt + 1 == self.settings["http_retries"]:
                    raise
                time.sleep(min(30, 2 ** (attempt + 1)))
                continue
            if response.status_code == 200 and not blocked(response.text):
                return Response(str(response.url), response.content)
            if response.status_code not in (429, 500, 502, 503, 504) or attempt + 1 == self.settings["http_retries"]:
                raise HTTPError(url, response.status_code)
            time.sleep(2 ** (attempt + 1))

    def close_browser(self):
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
        finally:
            if self.playwright:
                self.playwright.stop()
        self.playwright = self.browser = self.context = self.page = None

    def close(self):
        self.close_browser()
        self.session.close()
