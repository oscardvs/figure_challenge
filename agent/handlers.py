"""Specialized handlers for known challenge patterns."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browser import BrowserController


async def handle_cookie_consent(browser: "BrowserController") -> bool:
    """Handle cookie consent popups."""
    selectors = [
        "text=Accept",
        "text=Accept All",
        "text=I Accept",
        "button:has-text('Accept')",
        "[data-testid*='accept']",
    ]
    for sel in selectors:
        try:
            await browser.page.click(sel, timeout=1000)
            return True
        except Exception:
            continue
    return False


async def handle_fake_popup(browser: "BrowserController") -> bool:
    """Close popups with fake dismiss buttons using X."""
    # The red X is the real close, green Dismiss is fake
    selectors = [
        "button:has(img[src*='x'])",
        "button:has(img[alt*='close'])",
        "[class*='close']:not(:has-text('Dismiss'))",
        "[aria-label*='close']",
        "[aria-label*='Close']",
        "button:has-text('×')",
    ]
    for sel in selectors:
        try:
            await browser.page.click(sel, timeout=500)
            return True
        except Exception:
            continue
    return False


async def handle_scroll_challenge(browser: "BrowserController") -> bool:
    """Scroll to bottom to find navigation."""
    await browser.scroll_to_bottom()
    return True


async def handle_moving_element(browser: "BrowserController", selector: str) -> bool:
    """Click moving element using JS (bypasses movement)."""
    try:
        await browser.execute_js(f"""
            const el = document.querySelector('{selector}');
            if (el) el.click();
        """)
        return True
    except Exception:
        return False


async def handle_delayed_content(browser: "BrowserController", timeout: int = 3000) -> bool:
    """Wait for delayed content to appear."""
    try:
        # Wait for common navigation elements
        selectors = ["text=Next", "text=Continue", "text=Proceed"]
        for sel in selectors:
            try:
                await browser.page.wait_for_selector(sel, timeout=timeout)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def handle_multiple_popups(browser: "BrowserController") -> int:
    """Close multiple popups in z-index order. Returns count closed."""
    closed = 0
    for _ in range(5):  # Max 5 popups
        if await handle_fake_popup(browser):
            closed += 1
            await browser.page.wait_for_timeout(200)
        else:
            break
    return closed


def detect_challenge_type(html: str) -> str:
    """Detect challenge type from HTML patterns."""
    html_lower = html.lower()

    if "cookie" in html_lower and "consent" in html_lower:
        return "cookie"

    if "dismiss" in html_lower and "close" in html_lower:
        return "fake_popup"

    if "scroll down" in html_lower or "scroll to find" in html_lower:
        return "scroll"

    if "hidden" in html_lower and ("code" in html_lower or "data-" in html):
        return "hidden_code"

    if "moving" in html_lower or "catch" in html_lower:
        return "moving"

    if "wait" in html_lower or "loading" in html_lower:
        return "delayed"

    # Count number of similar buttons (decoy detection)
    next_count = len(re.findall(r'>next<', html_lower))
    if next_count > 3:
        return "decoy"

    return "unknown"


def get_handler_for_type(challenge_type: str):
    """Get the appropriate handler function for a challenge type."""
    handlers = {
        "cookie": handle_cookie_consent,
        "fake_popup": handle_fake_popup,
        "scroll": handle_scroll_challenge,
        "delayed": handle_delayed_content,
    }
    return handlers.get(challenge_type)
