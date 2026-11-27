"""Real-Chrome browser session tuned to survive bot detection.

Why real Chrome and not bundled Chromium: the DOM probes showed Greenhouse
(reCAPTCHA v2 checkbox), Ashby (reCAPTCHA + live challenge) and Lever
(hCaptcha) all flag headless Chromium immediately. A persistent profile in
real Chrome, headed, with human-ish timing, passes far more often -- though
never 100%. Outcomes are always verified, never assumed.
"""

from __future__ import annotations

import os
import random
import time
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Page, sync_playwright

from .config import CHROME_PROFILE_DIR, SHOTS_DIR

VIEWPORT = {"width": 1440, "height": 900}

# Removes the obvious automation tells before any page script runs.
# NOTE: navigator.plugins is NOT overridden -- real Chrome already exposes a
# PluginArray (PDF viewer) and replacing it with a JS Array is itself a tell.
STEALTH_JS = """
(() => {
  const def = (o, k, v) => { try { Object.defineProperty(o, k, {get: () => v, configurable: true}); } catch (e) {} };
  def(Navigator.prototype, 'webdriver', false);
  def(navigator, 'languages', ['en-US', 'en']);
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  // Headless has no browser frame: outer == inner and avail == screen.
  // Give it a normal macOS window: menu bar + dock + toolbar.
  try {
    const sw = screen.width || 1440, sh = screen.height || 900;
    def(Screen.prototype, 'availHeight', sh - 25);
    def(Screen.prototype, 'availWidth', sw);
    def(Screen.prototype, 'availTop', 25);
    def(window, 'outerWidth', window.innerWidth || sw);
    def(window, 'outerHeight', (window.innerHeight || sh) + 87);
    def(window, 'screenX', 0);
    def(window, 'screenY', 25);
  } catch (e) {}
  // Report a mouse (fine pointer + hover), not a touch screen.
  try {
    const mm = window.matchMedia.bind(window);
    const fake = (q, v) => ({matches: v, media: q, onchange: null,
      addListener() {}, removeListener() {}, addEventListener() {},
      removeEventListener() {}, dispatchEvent() { return false; }});
    window.matchMedia = (q) => {
      const s = String(q).replace(/\\s+/g, '');
      if (/\\((any-)?pointer:fine\\)/.test(s)) return fake(q, true);
      if (/\\((any-)?pointer:(coarse|none)\\)/.test(s)) return fake(q, false);
      if (/\\((any-)?hover:hover\\)/.test(s)) return fake(q, true);
      if (/\\((any-)?hover:none\\)/.test(s)) return fake(q, false);
      return mm(q);
    };
  } catch (e) {}
  try {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) => (p && p.name === 'notifications')
      ? Promise.resolve({state: Notification.permission === 'default' ? 'prompt' : Notification.permission, onchange: null})
      : origQuery(p);
  } catch (e) {}
})();
"""


class BrowserSession:
    """Owns one persistent Chrome context for a whole apply run."""

    def __init__(self, headless: bool = True, slow_mo: int = 15,
                 offscreen: bool = True, profile_dir=None) -> None:
        # Default is HEADLESS real Chrome (new headless mode = the full Chrome
        # browser, no window). The user explicitly asked that nothing ever
        # appears on / moves their screen; macOS snaps off-screen windows back
        # into view and Chrome activation steals focus, so a window is never
        # opened unless --show is passed.
        self.headless = headless
        self.slow_mo = slow_mo
        self.offscreen = offscreen and not headless
        # Separate profile dirs let two sessions (one per candidate) run in
        # parallel; Chrome refuses to share one user-data-dir between processes.
        self.profile_dir = str(profile_dir or CHROME_PROFILE_DIR)
        self._pw = None
        self._ctx = None

    def start(self) -> "BrowserSession":
        os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs = {
            "user_data_dir": self.profile_dir,
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "viewport": VIEWPORT,
            "locale": "en-US",
            "timezone_id": "Asia/Kolkata",
            "accept_downloads": False,
            "args": [
                *( ["--headless=new"] if self.headless else [] ),
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-default-browser-check",
                "--no-first-run",
                "--password-store=basic",
                *( ["--window-position=-4000,-4000", "--window-size=1440,900"]
                   if self.offscreen else [] ),
            ],
            # Playwright's headless defaults advertise a touch-only pointer and
            # hidden scrollbars -- both are bot tells for hCaptcha/reCAPTCHA.
            "ignore_default_args": [
                "--enable-automation",
                "--hide-scrollbars",
                "--mute-audio",
                "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
                "primaryPointerType=4,availablePointerTypes=4",
            ],
        }
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
        except Exception:
            # No Google Chrome available -> fall back to bundled Chromium.
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self._ctx.set_default_timeout(30_000)
        if self.headless:
            # Headless UA advertises 'HeadlessChrome' -- an instant bot flag.
            try:
                probe = self._ctx.new_page()
                ua = probe.evaluate("navigator.userAgent")
                probe.close()
                ua = ua.replace("HeadlessChrome", "Chrome")
                self._ctx.set_extra_http_headers({"User-Agent": ua})
                self._ctx.add_init_script(
                    "Object.defineProperty(navigator,'userAgent',{get:()=>%r});" % ua
                )
            except Exception:
                pass
        self._ctx.add_init_script(STEALTH_JS)
        return self

    @property
    def context(self):
        if self._ctx is None:
            raise RuntimeError("BrowserSession.start() was never called")
        return self._ctx

    def new_page(self) -> Page:
        page = self.context.new_page()
        page.set_default_timeout(30_000)
        return page

    @contextmanager
    def page(self) -> Iterator[Page]:
        page = self.new_page()
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        for closer in (
            lambda: self._ctx.close() if self._ctx else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._ctx = None
        self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


# Every deliberate pause is scaled by this factor. 0.3 = fast (default),
# 1.0 = the old cautious pacing. Override with JOBBOT_PAUSE_SCALE.
PAUSE_SCALE = float(os.environ.get("JOBBOT_PAUSE_SCALE", "0.3"))


def human_pause(low: float = 0.25, high: float = 0.75) -> None:
    time.sleep(random.uniform(low, high) * PAUSE_SCALE)


def goto(page: Page, url: str, wait: str = "domcontentloaded") -> bool:
    """Navigate; returns False on hard navigation failure instead of raising."""
    try:
        page.goto(url, wait_until=wait, timeout=45_000)
    except Exception:
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=4_000)
    except Exception:
        pass
    human_pause(0.6, 1.2)
    dismiss_cookies(page)
    return True


_COOKIE_BUTTONS = (
    "[data-ui='cookie-consent-decline']",
    "#onetrust-reject-all-handler",
    "button:has-text('Decline all')",
    "button:has-text('Reject all')",
    "button:has-text('Reject All')",
    "[data-ui='cookie-consent-accept']",
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept cookies')",
)


def dismiss_cookies(page: Page) -> bool:
    """Cookie banners sit over the form and swallow every click; close them."""
    for sel in _COOKIE_BUTTONS:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


def shoot(page: Page, name: str) -> str:
    """Screenshot every outcome so each logged row has visual proof."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120]
    path = SHOTS_DIR / f"{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        try:
            page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return ""


def scroll_through(page: Page, steps: int = 4) -> None:
    """Lazy-loaded ATS forms only render fields once scrolled."""
    for _ in range(steps):
        try:
            page.mouse.wheel(0, 700)
        except Exception:
            return
        human_pause(0.15, 0.35)
    try:
        page.mouse.wheel(0, -2400)
    except Exception:
        pass
    human_pause(0.2, 0.4)


def human_mouse(page: Page, target=None) -> None:
    """Move the pointer along a few curved, uneven steps (and onto `target`).

    Invisible captchas (hCaptcha on Lever, reCAPTCHA v3 on Ashby) score pointer
    movement; a click with no prior mouse activity looks scripted.
    """
    try:
        vw, vh = VIEWPORT["width"], VIEWPORT["height"]
        x, y = random.uniform(vw * 0.2, vw * 0.8), random.uniform(vh * 0.2, vh * 0.7)
        page.mouse.move(x, y, steps=random.randint(8, 16))
        for _ in range(random.randint(2, 4)):
            x = min(max(x + random.uniform(-220, 220), 5), vw - 5)
            y = min(max(y + random.uniform(-160, 160), 5), vh - 5)
            page.mouse.move(x, y, steps=random.randint(6, 14))
            time.sleep(random.uniform(0.08, 0.3))
        if target is not None:
            box = target.bounding_box(timeout=3000)
            if box:
                tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
                page.mouse.move(tx, ty, steps=random.randint(12, 24))
                time.sleep(random.uniform(0.15, 0.45))
    except Exception:
        pass
