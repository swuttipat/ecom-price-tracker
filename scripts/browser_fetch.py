#!/usr/bin/env python3
"""Fetching through a real browser, for when the plain HTTP path is walled.

Lazada raised Alibaba's x5sec wall against plain HTTP clients on 2026-08-11. Cookie
priming, the exact parameter set the site's own JavaScript uses, and curl_cffi's Chrome
TLS impersonation were all blocked, while a real browser on the same IP kept working. So
the fallback is a real browser.

The trick is not to screen-scrape the DOM. The page fetches its own data from
`?ajax=true`, so this loads the ordinary category page once to establish a session, then
issues the same ajax calls from *inside* that page with `fetch()`. Those requests carry
the browser's real fingerprint and its JavaScript-set cookies, and they return exactly
the JSON the HTTP collector used to get, so nothing downstream changes.

`AutoFetcher` is the thing collectors use: it tries cheap HTTP first and only starts a
browser once it sees the wall. If Lazada relaxes, runs quietly go back to being fast.

Setup (once):
    py -m pip install playwright
    py -m playwright install chromium
"""
from __future__ import annotations

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common as C  # noqa: E402

# A browser profile is tens of thousands of small files. It must never live in OneDrive.
PROFILE_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ecom_price_tracking")
PROFILE_DIR = os.path.join(PROFILE_ROOT, "browser_profile")

INSTALL_HINT = (
    "Playwright is not set up. Run these two commands, then try again:\n"
    "    py -m pip install playwright\n"
    "    py -m playwright install chromium"
)

# Runs inside the page, so the request carries the browser's own cookies and fingerprint.
# The AbortController is not optional: page.evaluate has no timeout of its own, so a
# request the server never answers would hang the whole run indefinitely.
FETCH_JS = """
async ({url, timeoutMs}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      credentials: 'include',
      signal: ctrl.signal,
      headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/plain, */*'},
    });
    return {status: r.status, body: await r.text()};
  } catch (e) {
    return {status: 0, body: '', error: String(e && e.name === 'AbortError' ? 'timeout' : e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


class BrowserFetcher:
    """Same get_json contract as common.Fetcher, backed by a real browser."""

    def __init__(self, settings: dict, verbose: bool = True, headless: bool = True,
                 profile_dir: str | None = None):
        self.settings = settings
        self.verbose = verbose
        self.headless = headless
        self.profile_dir = profile_dir or PROFILE_DIR
        # Always True now that this class only ever launches its own browser. Kept because
        # close() must not tear down a browser it did not start.
        self._owns_browser = True
        self._created_page = True
        self._browser = None
        self.count = 0
        self.blocked = False
        self.cap = int(settings.get("max_requests_per_run", 90))
        self.timeout_ms = int(settings.get("request_timeout_seconds", 30)) * 1000
        self._min = float(settings.get("delay_seconds_min", 2.0))
        self._max = float(settings.get("delay_seconds_max", 4.0))
        self._last_at = 0.0
        self._warmed = set()
        self._pw = self._ctx = self._page = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(INSTALL_HINT) from None

        os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            # A persistent profile lets cookies and the site's trust score carry over
            # between runs, which is most of what keeps the wall down.
            self._ctx = self._pw.chromium.launch_persistent_context(
                self.profile_dir,
                headless=self.headless,
                locale="th-TH",
                timezone_id="Asia/Bangkok",
                viewport={"width": 1440, "height": 900},
                user_agent=self.settings.get("user_agent") or None,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                raise RuntimeError(INSTALL_HINT) from None
            raise
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # Images and fonts are pure weight when we only want the JSON, so they are dropped
        # in headless runs. Never in headed runs: a verification challenge IS images, and
        # blocking them renders it as broken icons that nobody can solve.
        if self.headless:
            self._page.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in ("image", "media", "font")
                else route.continue_()
            ))
        if self.verbose:
            print(f"    browser started ({'headless' if self.headless else 'visible'}), "
                  f"profile at {self.profile_dir}")

    def close(self):
        if not self._owns_browser:
            # Attached to the user's Chrome: close only a tab we opened ourselves, never
            # one they were already using, and never their browser.
            page_to_close = self._page if self._created_page else None
            for obj, meth in ((page_to_close, "close"), (self._browser, "close"), (self._pw, "stop")):
                try:
                    if obj:
                        getattr(obj, meth)()
                except Exception:                   # noqa: BLE001
                    pass
        else:
            for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
                try:
                    if obj:
                        getattr(obj, meth)()
                except Exception:                   # noqa: BLE001 - teardown must not mask a real error
                    pass
        self._pw = self._ctx = self._page = self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- fetching ----------------------------------------------------------
    def _wait(self):
        gap = random.uniform(self._min, self._max)
        elapsed = time.time() - self._last_at
        if self._last_at and elapsed < gap:
            time.sleep(gap - elapsed)

    # Alibaba's verification widgets, and the product grid that should be there instead.
    # Lazada serves either its own slide-to-verify or a Google reCAPTCHA image challenge,
    # so both shapes have to be recognised.
    _SLIDER_SEL = (
        '.nc_wrapper, #nc_1_wrapper, .baxia-dialog, iframe[src*="punish"],'
        ' iframe[src*="recaptcha"], iframe[title*="recaptcha" i], .geetest_panel'
    )
    _CARD_SEL = '[data-qa-locator="product-item"]'

    def _page_state(self) -> str:
        """'ok' | 'slider' | 'punish' | 'empty'"""
        try:
            if "_____tmd_____/punish" in self._page.url:
                return "punish"
        except Exception:                           # noqa: BLE001 - mid-navigation
            return "empty"
        try:
            found = self._page.evaluate(
                "(sel) => ({slider: !!document.querySelector(sel.s),"
                " cards: document.querySelectorAll(sel.c).length})",
                {"s": self._SLIDER_SEL, "c": self._CARD_SEL},
            )
        except Exception:                           # noqa: BLE001
            return "empty"
        if found.get("slider"):
            return "slider"
        return "ok" if found.get("cards") else "empty"

    def _await_human(self) -> bool:
        """Headed mode only: hold while the person at the keyboard clears the challenge.

        Solving it is deliberately left to Max. Once solved, the persistent profile keeps
        the verified session, so this should be a one-off.
        """
        print("\n" + "=" * 62)
        print("  LAZADA IS ASKING FOR VERIFICATION")
        print("=" * 62)
        print("  A browser window is open. Please solve the challenge in it:")
        print("  either a slider, or a 'select all images with...' grid.")
        print("  Collection continues by itself the moment the products appear.")
        print("  Waiting up to 5 minutes...")
        print("=" * 62 + "\n")
        deadline = time.time() + 300
        announced = set()
        while time.time() < deadline:
            self._page.wait_for_timeout(2000)
            if self._page_state() == "ok":
                print("  Verified, thank you. Carrying on.\n")
                return True
            left = int(deadline - time.time())
            for mark in (240, 120, 60, 30):
                if mark not in announced and left <= mark:
                    announced.add(mark)
                    print(f"    still waiting, {mark}s left")
        print("  Not solved in time. Nothing was written; just run it again when ready.\n")
        return False

    def warm(self, page_url: str) -> bool:
        """Load a normal page so the session looks like somebody browsing."""
        key = page_url.split("?")[0]
        if key in self._warmed:
            return True
        self.start()
        self._wait()
        self.count += 1
        self._last_at = time.time()
        try:
            self._page.goto(page_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:                    # noqa: BLE001
            if self.verbose:
                print(f"    ! could not open {key}: {type(exc).__name__}")
            return False

        try:
            self._page.wait_for_timeout(2500)       # let the grid render
        except Exception:                           # noqa: BLE001 - page may still be navigating
            time.sleep(2.5)
        state = self._page_state()
        if state in ("slider", "punish"):
            if self.headless or not self._await_human():
                self.blocked = True
                return False
        elif state == "empty" and self.verbose:
            print(f"    ! {key} rendered no products; continuing, the ajax call will confirm")

        self._warmed.add(key)
        return True

    def get_json(self, url: str, referer: str | None = None):
        """(payload, note), matching common.Fetcher.get_json."""
        if self.count >= self.cap:
            return None, f"request cap reached ({self.cap})"
        self.start()
        if referer and not self.warm(referer):
            return None, C.BLOCKED_SENTINEL if self.blocked else "could not load the page"

        self._wait()
        self.count += 1
        self._last_at = time.time()

        result = None
        for attempt in (1, 2):
            try:
                result = self._page.evaluate(FETCH_JS, {"url": url, "timeoutMs": self.timeout_ms})
                break
            except Exception as exc:                # noqa: BLE001
                message = str(exc)
                # The site redirected the page out from under the call. That is usually the
                # site rejecting us rather than a transport fault, so settle and try once
                # more before deciding.
                if attempt == 1 and "Execution context was destroyed" in message:
                    try:
                        self._page.wait_for_timeout(2500)
                    except Exception:               # noqa: BLE001
                        time.sleep(2.5)
                    continue
                # The renderer died. That is a browser fault, not the site refusing us, so
                # replace the dead tab and give it one more go before calling the run lost.
                if attempt == 1 and ("Target crashed" in message or "Target closed" in message):
                    if self.verbose:
                        print("    ! the browser tab crashed - reopening it and retrying once")
                    try:
                        self._page = self._ctx.new_page()
                        self._created_page = True
                        self._warmed.clear()
                        if referer:
                            self.warm(referer)
                    except Exception:               # noqa: BLE001
                        return None, "browser tab crashed and could not be reopened"
                    continue
                return None, f"{type(exc).__name__}: {message.splitlines()[0]}"

        body = result.get("body") or ""
        if C.is_antibot_wall(body):
            self.blocked = True
            return None, C.BLOCKED_SENTINEL
        if result.get("error"):
            return None, f"in-page fetch {result['error']}"
        if result.get("status") != 200:
            return None, f"HTTP {result.get('status')}"
        try:
            return json.loads(body), "ok"
        except ValueError:
            return None, f"HTTP 200 but body was not JSON ({len(body)} bytes)"


class AutoFetcher:
    """Cheap HTTP first, real browser once the wall appears.

    Collectors hold one of these and never care which path is live. The switch happens at
    most once per run and is announced, so a slow run is never a mystery.
    """

    def __init__(self, settings: dict, verbose: bool = True,
                 force_browser: bool = False, headless: bool = True,
                 profile_dir: str | None = None):
        self.settings = settings
        self.verbose = verbose
        self.headless = headless
        self.profile_dir = profile_dir
        self._http = None if force_browser else C.Fetcher(settings, verbose)
        self._browser = None
        self.mode = "browser" if force_browser else "http"
        if force_browser:
            self._browser = BrowserFetcher(settings, verbose, headless, profile_dir)

    @property
    def count(self) -> int:
        return (self._http.count if self._http else 0) + (self._browser.count if self._browser else 0)

    @property
    def cap(self) -> int:
        return int(self.settings.get("max_requests_per_run", 90))

    @property
    def blocked(self) -> bool:
        """Only meaningful once every available path has been walled."""
        return bool(self._browser and self._browser.blocked)

    def _switch(self):
        if self.verbose:
            print("\n  ! Lazada's anti-bot wall is up for plain HTTP requests.")
            print("    Switching to a real browser for the rest of this run. It is slower.\n")
        self._browser = BrowserFetcher(self.settings, self.verbose, self.headless, self.profile_dir)
        self._http = None
        self.mode = "browser"

    def get_json(self, url: str, referer: str | None = None):
        if self._http is not None:
            payload, note = self._http.get_json(url, referer)
            if note != C.BLOCKED_SENTINEL:
                return payload, note
            self._switch()
        return self._browser.get_json(url, referer)

    def close(self):
        if self._browser:
            self._browser.close()
