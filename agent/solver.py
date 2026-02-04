import asyncio
import re
from browser import BrowserController
from vision import VisionAnalyzer, ActionType
from dom_parser import extract_hidden_codes
from metrics import MetricsTracker
from handlers import (
    detect_challenge_type,
    handle_cookie_consent,
    handle_fake_popup,
    handle_scroll_challenge,
    handle_moving_element,
    handle_delayed_content,
)


class ChallengeSolver:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.browser = BrowserController()
        self.vision = VisionAnalyzer(api_key)
        self.metrics = MetricsTracker()
        self.max_attempts_per_challenge = 10
        self.current_challenge = 0

    async def run(self, start_url: str, headless: bool = False) -> dict:
        """Run through all 30 challenges."""
        await self.browser.start(start_url, headless=headless)

        try:
            # Click START button
            await asyncio.sleep(1)
            await self.browser.click_by_text("START")
            await asyncio.sleep(0.5)

            for challenge_num in range(1, 31):
                self.current_challenge = challenge_num
                self.metrics.start_challenge(challenge_num)
                print(f"\n--- Challenge {challenge_num}/30 ---")

                success = await self._solve_challenge(challenge_num)

                if not success:
                    self.metrics.end_challenge(
                        challenge_num,
                        success=False,
                        error="Failed to solve within max attempts"
                    )
                    print(f"Challenge {challenge_num} FAILED")

        finally:
            await self.browser.stop()
            self.metrics.print_summary()

        return self.metrics.get_summary()

    async def _solve_challenge(self, challenge_num: int) -> bool:
        """Solve a single challenge with optimizations."""
        total_tokens_in = 0
        total_tokens_out = 0

        for attempt in range(self.max_attempts_per_challenge):
            # Parallel: get screenshot and HTML simultaneously
            screenshot_task = asyncio.create_task(self.browser.screenshot())
            html_task = asyncio.create_task(self.browser.get_html())
            url_task = asyncio.create_task(self.browser.get_url())

            screenshot, html, url = await asyncio.gather(
                screenshot_task, html_task, url_task
            )

            # Check if moved to next challenge
            if self._check_progress(url, challenge_num):
                self.metrics.end_challenge(
                    challenge_num,
                    success=True,
                    tokens_in=total_tokens_in,
                    tokens_out=total_tokens_out
                )
                print(f"Challenge {challenge_num} PASSED")
                return True

            # Fast path: DOM code extraction (no API call)
            dom_codes = extract_hidden_codes(html)

            if dom_codes:
                print(f"  Found codes in DOM: {dom_codes}")
                filled = await self._try_fill_code(dom_codes)
                if filled:
                    await asyncio.sleep(0.2)
                    continue

            # Challenge type detection using handlers (no API call)
            challenge_type = detect_challenge_type(html)
            if challenge_type != "unknown":
                print(f"  Detected challenge type: {challenge_type}")
                handled = await self._handle_challenge_type(challenge_type)
                if handled:
                    await asyncio.sleep(0.2)
                    continue

            # Quick pattern detection (no API call)
            quick_action = self._detect_quick_pattern(html)
            if quick_action:
                print(f"  Quick pattern: {quick_action}")
                await self._execute_quick_action(quick_action)
                await asyncio.sleep(0.2)
                continue

            # Vision analysis only if needed
            print(f"  Attempt {attempt + 1}: Analyzing with vision...")
            action, tokens_in, tokens_out = self.vision.analyze_page(
                screenshot, html, challenge_num, dom_codes
            )
            total_tokens_in += tokens_in
            total_tokens_out += tokens_out

            print(f"  Action: {action.action_type} -> {action.target_selector}")
            print(f"  Reasoning: {action.reasoning}")

            await self._execute_action(action)
            await asyncio.sleep(0.2)

        return False

    def _check_progress(self, url: str, challenge_num: int) -> bool:
        """Check if we've progressed past current challenge."""
        url_lower = url.lower()

        # Check for next step
        if f"step{challenge_num + 1}" in url_lower:
            return True
        if f"step-{challenge_num + 1}" in url_lower:
            return True
        if f"step/{challenge_num + 1}" in url_lower:
            return True

        # Check for completion
        if challenge_num == 30 and ("complete" in url_lower or "finish" in url_lower or "done" in url_lower):
            return True

        # Check for step number in URL
        match = re.search(r'step[/-]?(\d+)', url_lower)
        if match:
            step_num = int(match.group(1))
            if step_num > challenge_num:
                return True

        return False

    async def _try_fill_code(self, codes: list[str]) -> bool:
        """Try to fill code into input field."""
        for code in codes:
            try:
                # Try common input selectors
                selectors = [
                    "input[type='text']",
                    "input[placeholder*='code' i]",
                    "input[placeholder*='Code']",
                    "input[name*='code' i]",
                    "input:not([type='hidden'])",
                ]
                for sel in selectors:
                    if await self.browser.type_text(sel, code):
                        print(f"  Filled code: {code}")
                        # Try to submit
                        submit_clicked = await self.browser.click_by_text("Submit")
                        if not submit_clicked:
                            await self.browser.click_by_text("Next")
                        return True
            except Exception:
                continue
        return False

    async def _execute_action(self, action) -> None:
        """Execute the determined action."""
        if action.action_type == ActionType.CLICK:
            if action.target_selector:
                success = await self.browser.click(action.target_selector)
                if not success:
                    # Try by text content
                    await self.browser.click_by_text(action.target_selector)

        elif action.action_type == ActionType.TYPE:
            if action.target_selector and action.value:
                await self.browser.type_text(action.target_selector, action.value)
                # Try to submit after typing
                await self.browser.click_by_text("Submit")

        elif action.action_type == ActionType.SCROLL:
            await self.browser.scroll_to_bottom()

        elif action.action_type == ActionType.CLOSE_POPUP:
            await self.browser.close_popup_by_x()

        elif action.action_type == ActionType.WAIT:
            await asyncio.sleep(0.5)

        elif action.action_type == ActionType.NAVIGATE:
            if action.target_selector:
                await self.browser.click(action.target_selector)

        elif action.action_type == ActionType.EXTRACT_CODE:
            # Code extraction handled in main loop
            pass

    def _detect_quick_pattern(self, html: str) -> str | None:
        """Detect common challenge patterns without API call."""
        html_lower = html.lower()

        # Cookie consent
        if "cookie" in html_lower and "consent" in html_lower:
            return "cookie_consent"

        # Scroll challenge
        if "scroll down" in html_lower or "scroll to find" in html_lower:
            return "scroll"

        # Accept button visible
        if ">accept<" in html_lower or ">accept all<" in html_lower:
            return "accept"

        # Continue/Next button (but not if there are many decoys)
        if html_lower.count(">next<") == 1 or html_lower.count(">continue<") == 1:
            return "next"

        return None

    async def _execute_quick_action(self, action: str) -> None:
        """Execute quick pattern action."""
        if action == "cookie_consent":
            await self.browser.click_by_text("Accept")

        elif action == "scroll":
            await self.browser.scroll_to_bottom()

        elif action == "accept":
            await self.browser.click_by_text("Accept")

        elif action == "next":
            success = await self.browser.click_by_text("Next")
            if not success:
                await self.browser.click_by_text("Continue")

    async def _handle_challenge_type(self, challenge_type: str) -> bool:
        """Handle detected challenge type using specialized handlers."""
        if challenge_type == "cookie":
            return await handle_cookie_consent(self.browser)

        elif challenge_type == "fake_popup":
            return await handle_fake_popup(self.browser)

        elif challenge_type == "scroll":
            return await handle_scroll_challenge(self.browser)

        elif challenge_type == "delayed":
            return await handle_delayed_content(self.browser)

        elif challenge_type == "moving":
            # Try to find and click moving element via JS
            return await handle_moving_element(self.browser, "button, a")

        return False
