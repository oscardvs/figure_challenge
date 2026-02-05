import asyncio
import re
import time
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
        self.failed_codes_this_step: set[str] = set()  # Track codes that failed on current step

    async def run(self, start_url: str, headless: bool = False) -> dict:
        """Run through all 30 challenges."""
        await self.browser.start(start_url, headless=headless)

        try:
            # Wait for page to load and click START button
            await asyncio.sleep(2)
            print("Clicking START button...", flush=True)
            start_clicked = await self.browser.click_by_text("START")
            if not start_clicked:
                # Try alternative selectors
                start_clicked = await self.browser.click("button:has-text('Start')")
            if not start_clicked:
                start_clicked = await self.browser.click("a:has-text('Start')")
            print(f"START clicked: {start_clicked}")
            await asyncio.sleep(1)

            run_start = time.time()
            for challenge_num in range(1, 31):
                self.current_challenge = challenge_num
                self.metrics.start_challenge(challenge_num)
                challenge_start = time.time()
                elapsed_total = challenge_start - run_start
                print(f"\n--- Challenge {challenge_num}/30 (elapsed: {elapsed_total:.1f}s) ---")

                success = await self._solve_challenge(challenge_num)

                challenge_time = time.time() - challenge_start
                if success:
                    print(f"  [{challenge_time:.1f}s] Challenge {challenge_num} PASSED", flush=True)
                else:
                    self.metrics.end_challenge(
                        challenge_num,
                        success=False,
                        error="Failed to solve within max attempts"
                    )
                    print(f"  [{challenge_time:.1f}s] Challenge {challenge_num} FAILED", flush=True)

        finally:
            await self.browser.stop()
            self.metrics.print_summary()

        return self.metrics.get_summary()

    async def _wait_for_content(self) -> bool:
        """Wait for React SPA to render content. Returns True if content loaded."""
        for wait_attempt in range(10):
            html = await self.browser.get_html()
            # Check if page has meaningful content (not just the React shell)
            has_buttons = 'button' in html.lower()
            has_input = 'input' in html.lower()
            has_step = f'step' in html.lower()
            if len(html) > 1000 and (has_buttons or has_input or has_step):
                return True
            print(f"  (waiting for content... {len(html)} chars, attempt {wait_attempt+1})", flush=True)
            await asyncio.sleep(0.5)
        return False

    async def _solve_challenge(self, challenge_num: int) -> bool:
        """Solve challenge using brute-force + DOM parsing (fast, no vision)."""
        total_tokens_in = 0
        total_tokens_out = 0

        # Reset per-step tracking
        self.failed_codes_this_step = set()

        # Wait for React to render the page content
        content_loaded = await self._wait_for_content()
        if not content_loaded:
            print(f"  WARNING: page content didn't load, continuing anyway", flush=True)

        stale_recovery_done = False  # Only try React state reset once per step

        for attempt in range(20):  # More attempts, faster
            url = await self.browser.get_url()
            print(f"  [{attempt+1}] url={url[-35:]}", flush=True)

            # Check if moved to next challenge
            if self._check_progress(url, challenge_num):
                self.metrics.end_challenge(
                    challenge_num, success=True,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                )
                print(f"  >>> PASSED <<<", flush=True)
                return True

            # Handle special challenges (modals, click-to-reveal elements)
            special = await self._handle_special_challenges()
            if special.get('handled'):
                print(f"  special: {special}", flush=True)

            # Use Playwright native clicks for radio/option selection (JS clicks don't trigger React)
            if special.get('modal_scrolled') or special.get('modal_closed'):
                radio_result = await self._try_radio_selection()
                if radio_result:
                    print(f"  radio_selected: True", flush=True)
                    await asyncio.sleep(0.5)
                    url = await self.browser.get_url()
                    if self._check_progress(url, challenge_num):
                        self.metrics.end_challenge(
                            challenge_num, success=True,
                            tokens_in=total_tokens_in, tokens_out=total_tokens_out
                        )
                        print(f"  >>> PASSED <<<", flush=True)
                        return True

            # If a countdown timer was detected, wait for it to finish (only first 3 attempts)
            if special.get('has_timer') and special.get('timer_seconds', 0) > 0 and attempt < 3:
                wait_secs = special['timer_seconds'] + 1  # +1 for safety margin
                print(f"  timer detected: {special['timer_seconds']}s remaining, waiting {wait_secs}s...", flush=True)
                await asyncio.sleep(wait_secs)
                # Re-extract codes after timer completes
                html = await self.browser.get_html()
                dom_codes = extract_hidden_codes(html)
                if dom_codes:
                    print(f"  post-timer codes: {dom_codes}", flush=True)
                    filled = await self._try_fill_code(dom_codes)
                    if filled:
                        url = await self.browser.get_url()
                        if self._check_progress(url, challenge_num):
                            self.metrics.end_challenge(
                                challenge_num, success=True,
                                tokens_in=total_tokens_in, tokens_out=total_tokens_out
                            )
                            print(f"  >>> PASSED <<<", flush=True)
                            return True
                continue  # Skip brute force on timer attempts

            # Handle Keyboard Sequence Challenge (press key combos to reveal code)
            html_check = await self.browser.get_html()
            if 'keyboard sequence' in html_check.lower() or ('press' in html_check.lower() and 'keys in sequence' in html_check.lower()):
                kbd_result = await self._try_keyboard_sequence(html_check)
                if kbd_result:
                    print(f"  keyboard_sequence: completed", flush=True)
                    await asyncio.sleep(0.5)
                    # Re-extract codes after sequence completes
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-keyboard codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Drag-and-Drop Challenge (fill slots with pieces to reveal code)
            html_lower = html_check.lower()
            if 'drag' in html_lower and 'drop' in html_lower and 'slot' in html_lower:
                dnd_result = await self._try_drag_and_drop()
                if dnd_result:
                    print(f"  drag_and_drop: completed", flush=True)
                    await asyncio.sleep(0.5)
                    # Re-extract codes after filling slots
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-dnd codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Hover Challenge (hover over element to reveal code)
            if 'hover' in html_lower and ('reveal' in html_lower or 'code' in html_lower):
                hover_result = await self._try_hover_challenge()
                if hover_result:
                    print(f"  hover_challenge: completed", flush=True)
                    await asyncio.sleep(0.3)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-hover codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Canvas Challenge (draw 3+ strokes on canvas to reveal code)
            if 'canvas' in html_lower and ('stroke' in html_lower or 'draw' in html_lower):
                canvas_result = await self._try_canvas_challenge()
                if canvas_result:
                    print(f"  canvas_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-canvas codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Timing Challenge (click Capture while window is active)
            if 'timing' in html_lower and 'capture' in html_lower and 'active' in html_lower:
                timing_result = await self._try_timing_challenge()
                if timing_result:
                    print(f"  timing_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-timing codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Audio Challenge (play audio, click complete to reveal code)
            if 'audio' in html_lower and ('play' in html_lower or 'listen' in html_lower):
                audio_result = await self._try_audio_challenge()
                if audio_result:
                    print(f"  audio_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-audio codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Split Parts Challenge (click scattered parts to assemble code)
            if 'split' in html_lower and 'part' in html_lower and ('found' in html_lower or 'click' in html_lower):
                split_result = await self._try_split_parts_challenge()
                if split_result:
                    print(f"  split_parts: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-split codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Rotating Code Challenge (click Capture N times to reveal real code)
            if 'rotating' in html_lower and 'capture' in html_lower:
                rotate_result = await self._try_rotating_code_challenge()
                if rotate_result:
                    print(f"  rotating_code: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-rotate codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Multi-Tab Challenge (click through tabs to collect code parts)
            if 'tab' in html_lower and ('puzzle' in html_lower or 'multi' in html_lower or 'visit' in html_lower):
                tab_result = await self._try_multi_tab_challenge()
                if tab_result:
                    print(f"  multi_tab: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-tab codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Sequence Challenge (click button N times to progress and reveal code)
            if 'sequence' in html_lower or ('progress' in html_lower and 'click' in html_lower):
                seq_result = await self._try_sequence_challenge()
                if seq_result:
                    print(f"  sequence_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-sequence codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Math/Puzzle Challenge (solve expression, type answer, click Solve)
            if 'puzzle' in html_lower and ('solve' in html_lower or '= ?' in html_lower or '=?' in html_lower):
                puzzle_result = await self._try_math_puzzle_challenge()
                if puzzle_result:
                    print(f"  math_puzzle: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-puzzle codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Handle Video Challenge (seek through frames to find code)
            if 'video' in html_lower and 'frame' in html_lower and 'seek' in html_lower:
                video_result = await self._try_video_challenge()
                if video_result:
                    print(f"  video_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-video codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Scroll page: alternate between bottom and top for scroll challenges
            if attempt % 2 == 0:
                await self.browser.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            else:
                await self.browser.page.evaluate("() => window.scrollTo(0, 0)")

            # BRUTE FORCE: Click all buttons rapidly via JS
            clicks = await self._brute_force_click()
            print(f"  brute_force: {clicks}", flush=True)

            # Small delay for DOM to update after reveal buttons
            if clicks.get('reveal', 0) > 0 or special.get('reveal_clicked'):
                await asyncio.sleep(0.3)

            # Check progress again after clicking
            url = await self.browser.get_url()
            if self._check_progress(url, challenge_num):
                self.metrics.end_challenge(
                    challenge_num, success=True,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                )
                print(f"  >>> PASSED <<<", flush=True)
                return True

            # DOM: Extract codes and try to fill
            html = await self.browser.get_html()
            dom_codes = extract_hidden_codes(html)
            if dom_codes:
                print(f"  dom_codes: {dom_codes}", flush=True)

                # Stale code recovery: if ALL codes were tried and failed on this step,
                # the React SPA may have retained old component state. Reset via back/forward.
                all_failed = all(c in self.failed_codes_this_step for c in dom_codes)
                if all_failed and not stale_recovery_done and len(self.failed_codes_this_step) > 0:
                    print(f"  -> all {len(dom_codes)} codes failed this step, resetting React state...", flush=True)
                    stale_recovery_done = True
                    try:
                        await self.browser.page.go_back(wait_until='domcontentloaded', timeout=2000)
                        await asyncio.sleep(0.1)
                        await self.browser.page.go_forward(wait_until='domcontentloaded', timeout=2000)
                        await asyncio.sleep(0.3)
                    except Exception:
                        pass
                    continue  # Retry with fresh React state

                filled = await self._try_fill_code(dom_codes)
                print(f"  filled: {filled}", flush=True)
            else:
                print(f"  dom_codes: none found", flush=True)
                # If page is still blank, wait a bit more
                if len(html) < 1000:
                    print(f"  (page still blank, waiting...)", flush=True)
                    await asyncio.sleep(1)
                    continue

            # Use AI vision agent earlier and more aggressively when stuck
            # Trigger at attempt 3, then every 3 attempts (3, 6, 9, 12...)
            if attempt >= 3 and attempt % 3 == 0:
                vision_call_num = (attempt - 3) // 3
                print(f"  vision (call #{vision_call_num})...", flush=True)
                screenshot = await self.browser.screenshot()
                action, tin, tout = self.vision.analyze_page(
                    screenshot, html[:5000], challenge_num, dom_codes,
                    attempt=vision_call_num
                )
                total_tokens_in += tin
                total_tokens_out += tout
                print(f"  vision: {action.action_type} -> {action.target_selector}", flush=True)
                if action.code_found:
                    print(f"  vision_code: {action.code_found}", flush=True)
                    await self._try_fill_code([action.code_found])
                await self._execute_action(action)

            await asyncio.sleep(0.1)

        return False

    async def _handle_special_challenges(self) -> dict:
        """Handle special challenge patterns: modals, click-to-reveal, overlays, fake popups."""
        return await self.browser.page.evaluate("""
            () => {
                const result = {handled: false, modal_closed: false, reveal_clicked: 0, popups_removed: 0, modal_scrolled: false, has_timer: false, timer_seconds: 0};

                // 0. Handle popups with fake/real close buttons
                // React needs DOM nodes intact for reconciliation during route transitions.
                const hideElement = (el) => {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                    el.style.visibility = 'hidden';
                    el.style.zIndex = '-1';
                };

                // 0a. FIRST: Handle popups where one button is real and another is fake
                // e.g. "Newsletter Signup" with "Close (Fake)" and "Dismiss" buttons
                // The text says "close button below is fake! Look for the real one."
                document.querySelectorAll('.fixed, [class*="absolute"], [class*="z-"]').forEach(el => {
                    const text = el.textContent || '';
                    // Check for "close button" + "fake" + "real one" pattern (has a real dismiss button)
                    if (text.includes('fake') && text.includes('real one')) {
                        const buttons = el.querySelectorAll('button');
                        buttons.forEach(btn => {
                            const btnText = (btn.textContent || '').trim();
                            // Click the button that is NOT labeled as fake
                            if (!btnText.toLowerCase().includes('fake') &&
                                btnText.length > 0 && btnText.length < 30) {
                                btn.click();
                                result.handled = true;
                                result.modal_closed = true;
                            }
                        });
                    }
                });

                // 0b. Hide popups where ALL close buttons are fake
                // These say "Look for another way to close" (no real button exists)
                document.querySelectorAll('.fixed, [class*="absolute"], [class*="z-"]').forEach(el => {
                    const text = el.textContent || '';
                    if (text.includes('another way to close') ||
                        (text.includes('close button') && text.includes('fake') && !text.includes('real one')) ||
                        (text.includes('won a prize') && text.includes('popup')) ||
                        (text.includes('amazing deals') && text.includes('popup'))) {
                        hideElement(el);
                        result.popups_removed++;
                        result.handled = true;
                    }
                });

                // Also hide any "That close button is fake!" warning overlays
                document.querySelectorAll('.fixed, [class*="absolute"]').forEach(el => {
                    const text = (el.textContent || '').trim();
                    if (text.includes('That close button is fake')) {
                        hideElement(el);
                        result.popups_removed++;
                        result.handled = true;
                    }
                });

                // 1. Handle scrollable modals - scroll to TOP to reveal radio options
                // Radio options are typically at the top, with filler text below
                const scrollContainers = document.querySelectorAll(
                    '[class*="overflow-y"], [class*="overflow-auto"], [style*="overflow"]'
                );
                scrollContainers.forEach(modal => {
                    if (modal.scrollHeight > modal.clientHeight) {
                        // Scroll to top first to reveal radio options
                        modal.scrollTop = 0;
                        result.modal_scrolled = true;
                        result.handled = true;
                    }
                });

                // Also handle modal-like containers (fixed/absolute with max-height)
                document.querySelectorAll('.fixed, [role="dialog"]').forEach(modal => {
                    const scrollable = modal.querySelector('[class*="overflow"], [style*="overflow"]');
                    if (scrollable && scrollable.scrollHeight > scrollable.clientHeight) {
                        scrollable.scrollTop = 0;
                        result.modal_scrolled = true;
                        result.handled = true;
                    }
                });

                // 2. Handle modal with radio/option selection
                // Multiple strategies since radio buttons may be custom-styled cards
                let radioSelected = false;
                const correctPatterns = ['correct', 'right choice', 'right answer'];
                const isCorrectOption = (text) => {
                    const t = text.toLowerCase();
                    return correctPatterns.some(p => t.includes(p)) && !t.includes('wrong');
                };

                // Strategy 1: Click actual radio inputs AND their containers
                const radios = document.querySelectorAll('input[type="radio"]');
                radios.forEach(radio => {
                    const container = radio.closest('[class*="cursor"], [class*="border"]') ||
                                      radio.closest('label') || radio.closest('div');
                    const label = container?.textContent || radio.parentElement?.textContent || '';
                    if (isCorrectOption(label)) {
                        radio.click();
                        if (container) container.click();
                        radioSelected = true;
                    }
                });

                // Strategy 2: Click option cards (custom radio - divs with cursor-pointer)
                if (!radioSelected) {
                    document.querySelectorAll('[class*="cursor-pointer"], [role="option"], [role="radio"]').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (isCorrectOption(text) && text.length < 50) {
                            el.click();
                            radioSelected = true;
                        }
                    });
                }

                // Strategy 3: Click bordered option-like divs
                if (!radioSelected) {
                    document.querySelectorAll('div[class*="border"][class*="rounded"], div[class*="p-"][class*="border"]').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (isCorrectOption(text) && text.length < 50) {
                            el.click();
                            radioSelected = true;
                        }
                    });
                }

                if (radioSelected) {
                    result.handled = true;
                    result.modal_closed = true;
                }

                // Click "Submit & Continue" if visible (only if radio was selected)
                document.querySelectorAll('button').forEach(btn => {
                    const btnText = btn.textContent || '';
                    if ((btnText.includes('Submit & Continue') || btnText.includes('Submit and Continue')) && !btn.disabled) {
                        btn.click();
                        result.handled = true;
                    }
                });

                // 2b. Handle "Modal Dialog" popups with REAL close buttons
                // These say "Click the button to dismiss" - the Close button actually works
                document.querySelectorAll('.fixed').forEach(el => {
                    const text = el.textContent || '';
                    if (text.includes('Click the button to dismiss') ||
                        text.includes('interact with this modal')) {
                        const closeBtn = el.querySelector('button');
                        if (closeBtn) {
                            closeBtn.click();
                            result.handled = true;
                            result.modal_closed = true;
                        }
                    }
                });

                // 2c. Handle "Wrong Button" modals - click Close to dismiss
                document.querySelectorAll('.fixed').forEach(el => {
                    const text = el.textContent || '';
                    if (text.includes('Wrong Button') || text.includes('Try Again')) {
                        const closeBtn = el.querySelector('button');
                        if (closeBtn) {
                            closeBtn.click();
                            result.handled = true;
                        }
                    }
                });

                // 2d. Handle "Click X to close" / "Limited time offer" popups
                document.querySelectorAll('.fixed, [class*="z-"]').forEach(el => {
                    const text = el.textContent || '';
                    if (text.includes('Limited time offer') || text.includes('Click X to close') ||
                        text.includes('popup message')) {
                        el.querySelectorAll('button').forEach(btn => btn.click());
                        hideElement(el);
                        result.popups_removed++;
                        result.handled = true;
                    }
                });

                // 3. Click elements that say "click here X more times to reveal"
                document.querySelectorAll('div, p, span').forEach(el => {
                    const text = el.textContent || '';
                    if (text.includes('click here') && text.includes('to reveal')) {
                        el.click();
                        result.reveal_clicked++;
                        result.handled = true;
                    }
                });

                // 3b. Click "I Remember" button (Memory Challenge - reveals timing/capture)
                document.querySelectorAll('button').forEach(btn => {
                    const text = (btn.textContent || '').trim().toLowerCase();
                    if (text.includes('i remember') && btn.offsetParent && !btn.disabled) {
                        btn.click();
                        result.reveal_clicked++;
                        result.handled = true;
                    }
                });

                // 4. Remove blocking overlays (bg-black/70) - disable pointer events
                document.querySelectorAll('.fixed').forEach(el => {
                    if (el.classList.contains('bg-black/70') ||
                        el.style.backgroundColor?.includes('rgba(0, 0, 0')) {
                        if (!el.textContent.includes('Cookie') &&
                            !el.textContent.includes('Step') &&
                            !el.querySelector('input[type="radio"]')) {
                            el.style.pointerEvents = 'none';
                            result.handled = true;
                        }
                    }
                });

                // 5. Handle countdown/timed reveals - wait for timer to finish
                // Only match ACTIVE countdowns like "4 seconds remaining", not static text
                document.querySelectorAll('div, span, p').forEach(el => {
                    const text = el.textContent || '';
                    // Check both patterns: "X seconds remaining/left" and "countdown: X seconds"
                    const timerMatch = text.match(/(\\d+)\\s*second[s]?\\s*(?:remaining|left)/i);
                    const altMatch = text.match(/countdown[:\\s]*(\\d+)\\s*second/i);
                    const match = timerMatch || altMatch;
                    if (!match) return;
                    const secs = parseInt(match[1]);
                    if (secs > 0 && secs <= 10) {
                        result.has_timer = true;
                        result.timer_seconds = secs;
                        result.handled = true;
                    }
                });

                // 6. Close popups with red X buttons (real close buttons)
                document.querySelectorAll('button').forEach(btn => {
                    if (btn.querySelector('img') || btn.querySelector('svg')) {
                        const style = getComputedStyle(btn);
                        const bg = style.backgroundColor;
                        const match = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                        if (match) {
                            const [_, r, g, b] = match.map(Number);
                            if (r > 180 && g < 100 && b < 100) {
                                btn.click();
                                result.handled = true;
                            }
                        }
                    }
                });

                return result;
            }
        """)

    async def _try_keyboard_sequence(self, html: str) -> bool:
        """Handle Keyboard Sequence Challenge - press key combos to reveal code."""
        try:
            import re as _re
            # Parse required keys from HTML
            # Look for patterns like "Control+A", "Control+C", "Control+V", "Shift+K", etc.
            key_pattern = _re.compile(r'((?:Control|Shift|Alt|Meta)\+[A-Za-z0-9])')
            keys = key_pattern.findall(html)
            if not keys:
                return False

            # Deduplicate while preserving order (the sequence shown on page)
            seen = set()
            unique_keys = []
            for k in keys:
                if k not in seen:
                    seen.add(k)
                    unique_keys.append(k)

            print(f"    -> keyboard sequence detected: {unique_keys}", flush=True)

            # Focus the page body first
            await self.browser.page.evaluate("() => document.body.focus()")
            await asyncio.sleep(0.1)

            # Press each key combo using Playwright (proper keyboard events)
            for key in unique_keys:
                await self.browser.page.keyboard.press(key)
                print(f"    -> pressed: {key}", flush=True)
                await asyncio.sleep(0.3)

            return True
        except Exception as e:
            print(f"    -> keyboard sequence error: {e}", flush=True)
            return False

    async def _try_hover_challenge(self) -> bool:
        """Handle Hover Challenge - hover over element for 1+ second to reveal code."""
        try:
            # Step 1: Hide ALL floating decoy elements that obstruct hover target
            await self.browser.page.evaluate("""
                () => {
                    const decoyTexts = new Set([
                        'Click Me!', 'Button!', 'Link!', 'Here!', 'Click Here',
                        'Click Here!', 'Try This!', 'Move On', 'Keep Going'
                    ]);
                    document.querySelectorAll('div, button, a, span').forEach(el => {
                        const style = getComputedStyle(el);
                        const text = (el.textContent || '').trim();
                        if ((style.position === 'absolute' || style.position === 'fixed') &&
                            decoyTexts.has(text)) {
                            el.style.display = 'none';
                            el.style.pointerEvents = 'none';
                            el.style.visibility = 'hidden';
                        }
                    });
                }
            """)
            await asyncio.sleep(0.3)

            # Step 2: Find hover target, prefer innermost child, dispatch React events
            target_info = await self.browser.page.evaluate("""
                () => {
                    // Remove old markers
                    document.querySelectorAll('[data-hover-target]').forEach(el => el.removeAttribute('data-hover-target'));

                    let best = null;

                    // Strategy 1: cursor-pointer elements - find innermost interactive child
                    const cursorEls = [...document.querySelectorAll('[class*="cursor-pointer"]')].filter(el => {
                        return el.offsetParent && el.offsetWidth > 50 && el.offsetHeight > 30;
                    });
                    for (const parent of cursorEls) {
                        // Prefer inner child with bg/border/padding classes (React event target)
                        const children = parent.querySelectorAll('div');
                        for (const child of children) {
                            const cls = child.className || '';
                            if (child.offsetWidth > 30 && child.offsetHeight > 20 &&
                                child.offsetParent &&
                                (cls.includes('bg-') || cls.includes('border') || cls.includes('p-'))) {
                                best = child;
                                break;
                            }
                        }
                        if (!best) best = parent;
                        break;
                    }

                    // Strategy 2: bordered box element (fallback)
                    if (!best) {
                        const borderEls = [...document.querySelectorAll('div')].filter(el => {
                            const cls = el.className || '';
                            return cls.includes('border-2') && cls.includes('rounded') &&
                                   el.offsetParent && el.offsetWidth > 50;
                        });
                        if (borderEls.length > 0) best = borderEls[0];
                    }

                    // Strategy 3: min-h element with border (fallback)
                    if (!best) {
                        const minHEls = [...document.querySelectorAll('div')].filter(el => {
                            const cls = el.className || '';
                            return cls.includes('min-h-') && cls.includes('border') &&
                                   el.offsetParent && el.offsetWidth > 50;
                        });
                        if (minHEls.length > 0) best = minHEls[0];
                    }

                    if (!best) return {found: false};

                    // Mark for Playwright locator
                    best.setAttribute('data-hover-target', 'true');
                    best.scrollIntoView({behavior: 'instant', block: 'center'});

                    // Dispatch mouse events directly (React needs mouseenter/mouseover)
                    const rect = best.getBoundingClientRect();
                    const cx = rect.x + rect.width / 2;
                    const cy = rect.y + rect.height / 2;
                    const opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy};
                    best.dispatchEvent(new MouseEvent('mouseenter', opts));
                    best.dispatchEvent(new MouseEvent('mouseover', opts));
                    best.dispatchEvent(new MouseEvent('mousemove', opts));

                    return {x: cx, y: cy, found: true};
                }
            """)

            if not target_info.get('found'):
                return False

            await asyncio.sleep(0.3)

            # Step 3: Use Playwright .hover() for full event chain (mouseenter+mouseover+mousemove)
            x, y = target_info['x'], target_info['y']
            try:
                el = self.browser.page.locator('[data-hover-target="true"]')
                if await el.count() > 0:
                    await el.first.hover(timeout=2000)
            except Exception:
                # Fallback to mouse.move
                await self.browser.page.mouse.move(x, y)

            print(f"    -> hovering at ({x:.0f}, {y:.0f})", flush=True)
            # Hold hover for 2 seconds (challenge typically requires 1s)
            await asyncio.sleep(2.0)

            # Clean up marker
            try:
                await self.browser.page.evaluate(
                    "document.querySelector('[data-hover-target]')?.removeAttribute('data-hover-target')"
                )
            except Exception:
                pass

            return True
        except Exception as e:
            print(f"    -> hover error: {e}", flush=True)
            return False

    async def _try_canvas_challenge(self) -> bool:
        """Handle Canvas Challenge - draw shapes or strokes on a canvas to reveal code."""
        try:
            # Find the canvas element and detect required shape
            canvas_info = await self.browser.page.evaluate("""
                () => {
                    const canvas = document.querySelector('canvas');
                    if (!canvas) return {found: false};
                    canvas.scrollIntoView({behavior: 'instant', block: 'center'});
                    const rect = canvas.getBoundingClientRect();
                    const text = document.body.textContent.toLowerCase();
                    let shape = 'strokes';
                    if (text.includes('square')) shape = 'square';
                    else if (text.includes('circle')) shape = 'circle';
                    else if (text.includes('triangle')) shape = 'triangle';
                    else if (text.includes('line')) shape = 'line';
                    return {found: true, x: rect.x, y: rect.y, w: rect.width, h: rect.height, shape};
                }
            """)

            if not canvas_info.get('found'):
                print(f"    -> no canvas element found", flush=True)
                return False

            cx = canvas_info['x']
            cy = canvas_info['y']
            cw = canvas_info['w']
            ch = canvas_info['h']
            shape = canvas_info.get('shape', 'strokes')
            print(f"    -> canvas found at ({cx:.0f},{cy:.0f}) size {cw:.0f}x{ch:.0f}, shape={shape}", flush=True)

            if shape == 'square':
                # Draw a square: 4 connected sides
                margin = 0.2
                x1 = cx + cw * margin
                y1 = cy + ch * margin
                x2 = cx + cw * (1 - margin)
                y2 = cy + ch * (1 - margin)
                # Draw 4 sides as connected strokes
                corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
                await self.browser.page.mouse.move(corners[0][0], corners[0][1])
                await self.browser.page.mouse.down()
                for corner in corners[1:]:
                    await self.browser.page.mouse.move(corner[0], corner[1], steps=15)
                    await asyncio.sleep(0.05)
                await self.browser.page.mouse.up()
                print(f"    -> drew square", flush=True)

            elif shape == 'circle':
                # Draw a circle using many points
                import math
                center_x = cx + cw / 2
                center_y = cy + ch / 2
                radius = min(cw, ch) * 0.35
                points = 36
                start_x = center_x + radius
                start_y = center_y
                await self.browser.page.mouse.move(start_x, start_y)
                await self.browser.page.mouse.down()
                for i in range(1, points + 1):
                    angle = (2 * math.pi * i) / points
                    px = center_x + radius * math.cos(angle)
                    py = center_y + radius * math.sin(angle)
                    await self.browser.page.mouse.move(px, py, steps=3)
                await self.browser.page.mouse.up()
                print(f"    -> drew circle", flush=True)

            elif shape == 'triangle':
                # Draw a triangle
                margin = 0.2
                top = (cx + cw / 2, cy + ch * margin)
                bl = (cx + cw * margin, cy + ch * (1 - margin))
                br = (cx + cw * (1 - margin), cy + ch * (1 - margin))
                corners = [top, br, bl, top]
                await self.browser.page.mouse.move(corners[0][0], corners[0][1])
                await self.browser.page.mouse.down()
                for corner in corners[1:]:
                    await self.browser.page.mouse.move(corner[0], corner[1], steps=15)
                    await asyncio.sleep(0.05)
                await self.browser.page.mouse.up()
                print(f"    -> drew triangle", flush=True)

            else:
                # Default: draw 4 varied strokes
                for i in range(4):
                    start_x = cx + cw * 0.2 + (i * cw * 0.15)
                    start_y = cy + ch * 0.3 + (i * ch * 0.1)
                    end_x = cx + cw * 0.5 + (i * cw * 0.1)
                    end_y = cy + ch * 0.7 - (i * ch * 0.05)
                    await self.browser.page.mouse.move(start_x, start_y)
                    await self.browser.page.mouse.down()
                    await asyncio.sleep(0.05)
                    await self.browser.page.mouse.move(end_x, end_y, steps=10)
                    await asyncio.sleep(0.05)
                    await self.browser.page.mouse.up()
                    print(f"    -> drew stroke {i+1}", flush=True)
                    await asyncio.sleep(0.3)

            # Click Complete/Done button after drawing
            await asyncio.sleep(0.5)
            await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('complete') || t.includes('done') || t.includes('check') ||
                             t.includes('verify') || t.includes('submit')) &&
                            !t.includes('clear') && btn.offsetParent && !btn.disabled) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            await asyncio.sleep(0.5)
            return True

        except Exception as e:
            print(f"    -> canvas error: {e}", flush=True)
            return False

    async def _try_split_parts_challenge(self) -> bool:
        """Handle Split Parts Challenge - scroll to and click all scattered parts."""
        try:
            for click_round in range(10):
                # Use JS to: find all parts, scroll each into view, click it
                result = await self.browser.page.evaluate("""
                    () => {
                        const text = document.body.textContent || '';
                        const foundMatch = text.match(/(\\d+)\\/(\\d+)\\s*found/);
                        const found = foundMatch ? parseInt(foundMatch[1]) : 0;
                        const total = foundMatch ? parseInt(foundMatch[2]) : 4;
                        if (found >= total) return {found, total, clicked: 0, done: true};

                        let clicked = 0;
                        // Find ALL part divs and click unclicked ones
                        document.querySelectorAll('div').forEach(el => {
                            const style = getComputedStyle(el);
                            const cls = el.className || '';
                            const elText = (el.textContent || '').trim();
                            if (!(style.position === 'absolute' || cls.includes('absolute'))) return;
                            if (!elText.match(/Part\\s*\\d/i)) return;
                            if (el.offsetWidth < 10) return;

                            // Skip already-clicked (green background)
                            const bg = style.backgroundColor;
                            const isGreen = bg.includes('134') || bg.includes('green') ||
                                cls.includes('bg-green');
                            if (isGreen) return;

                            // Scroll into view and click
                            el.scrollIntoView({behavior: 'instant', block: 'center'});
                            el.click();
                            clicked++;
                        });

                        return {found, total, clicked, done: false};
                    }
                """)
                print(f"    -> split: {result.get('found')}/{result.get('total')} found, "
                      f"clicked {result.get('clicked')} this round", flush=True)

                if result.get('done'):
                    print(f"    -> all parts collected!", flush=True)
                    break

                if result.get('clicked', 0) == 0:
                    # No unclicked parts found via JS - scroll down to reveal more
                    await self.browser.page.evaluate(
                        "() => window.scrollBy(0, 400)"
                    )
                await asyncio.sleep(0.5)

            # Read the assembled code from parts
            await asyncio.sleep(0.5)
            assembled = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    // Look for "Code: XXXXXX" pattern
                    const codeMatch = text.match(/(?:code|Code)[:\\s]+([A-Z0-9]{6})/);
                    if (codeMatch) return codeMatch[1];

                    // Build code from parts in order
                    const parts = [];
                    document.querySelectorAll('div').forEach(el => {
                        const t = (el.textContent || '').trim();
                        const m = t.match(/Part\\s*(\\d+)[:\\s]*([A-Z0-9]{2,3})/i);
                        if (m) parts.push({num: parseInt(m[1]), code: m[2]});
                    });
                    // Deduplicate by part number
                    const unique = {};
                    parts.forEach(p => { unique[p.num] = p.code; });
                    const sorted = Object.keys(unique).sort((a,b) => a-b).map(k => unique[k]);
                    if (sorted.length >= 2) return sorted.join('');
                    return null;
                }
            """)
            if assembled:
                print(f"    -> assembled code: {assembled}", flush=True)
                filled = await self._try_fill_code([assembled])
                if filled:
                    return True

            return True
        except Exception as e:
            print(f"    -> split parts error: {e}", flush=True)
            return False

    async def _try_timing_challenge(self) -> bool:
        """Handle Timing Challenge - click Capture while the window is active to reveal code."""
        try:
            # The challenge shows a code with a countdown. We must click "Capture Now!"
            # while the timer is still active. Then the real code is revealed.
            for attempt in range(5):
                # Check if there's an active timing window (timer > 0)
                state = await self.browser.page.evaluate("""
                    () => {
                        const text = document.body.textContent || '';
                        const timerMatch = text.match(/(\\d+\\.?\\d*)\\s*seconds?\\s*remaining/i);
                        const hasCapture = !!document.querySelector('button');
                        const btns = [...document.querySelectorAll('button')];
                        let captureBtn = null;
                        for (const btn of btns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t.includes('capture') && btn.offsetParent && !btn.disabled) {
                                captureBtn = t;
                                break;
                            }
                        }
                        return {
                            timer: timerMatch ? parseFloat(timerMatch[1]) : null,
                            captureBtn,
                            hasCapture
                        };
                    }
                """)
                print(f"    -> timing state: timer={state.get('timer')}, btn={state.get('captureBtn')}", flush=True)

                # Click Capture button immediately (timing is critical!)
                clicked = await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const btn of btns) {
                            const text = (btn.textContent || '').trim().toLowerCase();
                            if (text.includes('capture') && btn.offsetParent && !btn.disabled) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)

                if clicked:
                    print(f"    -> clicked Capture!", flush=True)
                    await asyncio.sleep(0.5)

                    # Check if real code was revealed
                    html = await self.browser.get_html()
                    codes = extract_hidden_codes(html)
                    if codes:
                        print(f"    -> post-capture codes: {codes}", flush=True)
                        return True

                    # If the window wasn't active, we might need to wait for the next cycle
                    await asyncio.sleep(1.0)
                else:
                    # No capture button, wait for next timing window
                    await asyncio.sleep(0.5)

            return True
        except Exception as e:
            print(f"    -> timing error: {e}", flush=True)
            return False

    async def _try_rotating_code_challenge(self) -> bool:
        """Handle Rotating Code Challenge - click Capture N times, then submit revealed code."""
        try:
            for attempt in range(15):
                # Check current state
                state = await self.browser.page.evaluate("""
                    () => {
                        const text = document.body.textContent || '';
                        // Parse "Capture (1/3)" or "1/3" pattern near capture button
                        const btns = [...document.querySelectorAll('button')];
                        let captureBtn = null;
                        let done = 0, required = 3;
                        for (const btn of btns) {
                            const t = (btn.textContent || '').trim();
                            const m = t.match(/[Cc]apture.*?(\\d+)\\/(\\d+)/);
                            if (m) {
                                captureBtn = btn;
                                done = parseInt(m[1]);
                                required = parseInt(m[2]);
                                break;
                            }
                        }
                        // Also check for a plain "Capture" button
                        if (!captureBtn) {
                            for (const btn of btns) {
                                const t = (btn.textContent || '').trim().toLowerCase();
                                if (t.includes('capture') && btn.offsetParent && !btn.disabled) {
                                    captureBtn = btn;
                                    break;
                                }
                            }
                        }
                        return {done, required, hasBtn: !!captureBtn, complete: done >= required};
                    }
                """)
                print(f"    -> rotate state: {state.get('done')}/{state.get('required')}, "
                      f"complete={state.get('complete')}", flush=True)

                if state.get('complete'):
                    # All captures done - look for revealed code
                    await asyncio.sleep(0.5)
                    return True

                # Click Capture button
                clicked = await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const btn of btns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t.includes('capture') && btn.offsetParent && !btn.disabled) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if clicked:
                    print(f"    -> clicked Capture", flush=True)
                    no_btn_count = 0
                else:
                    no_btn_count = getattr(self, '_rotate_no_btn', 0) + 1
                    self._rotate_no_btn = no_btn_count
                    print(f"    -> no Capture button found ({no_btn_count})", flush=True)
                    if no_btn_count >= 3:
                        print(f"    -> Capture gone, stopping early", flush=True)
                        return True

                # Wait for next rotation cycle (code changes every 3 seconds)
                await asyncio.sleep(1.0)

            self._rotate_no_btn = 0
            return True
        except Exception as e:
            print(f"    -> rotating code error: {e}", flush=True)
            return False

    async def _try_multi_tab_challenge(self) -> bool:
        """Handle Multi-Tab Challenge - click through all tabs to collect code parts."""
        try:
            # Find and click each tab button, collecting parts from each
            parts = {}
            for round_num in range(3):
                result = await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        const tabBtns = btns.filter(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return (t.includes('tab') || t.match(/^\\d+$/)) && b.offsetParent;
                        });

                        // Click each tab and collect content
                        const parts = {};
                        for (const btn of tabBtns) {
                            const tabName = btn.textContent.trim();
                            btn.click();
                            // Read content after clicking
                            const text = document.body.textContent || '';
                            // Look for code parts like "Part 1: AB" or just 2-3 char segments
                            const partMatches = text.matchAll(/Part\\s*(\\d+)[:\\s]*([A-Z0-9]{2,3})/gi);
                            for (const m of partMatches) {
                                parts[parseInt(m[1])] = m[2].toUpperCase();
                            }
                            // Also look for any 6-char code that appears
                            const codeMatch = text.match(/(?:code|Code)[:\\s]+([A-Z0-9]{6})/);
                            if (codeMatch) parts['full'] = codeMatch[1];
                        }

                        return {
                            tabCount: tabBtns.length,
                            tabNames: tabBtns.map(b => b.textContent.trim()),
                            parts,
                        };
                    }
                """)
                print(f"    -> tabs: {result.get('tabCount')}, names={result.get('tabNames')}, "
                      f"parts={result.get('parts')}", flush=True)

                tab_parts = result.get('parts', {})
                if tab_parts.get('full'):
                    print(f"    -> found full code: {tab_parts['full']}", flush=True)
                    filled = await self._try_fill_code([tab_parts['full']])
                    if filled:
                        return True

                # Merge parts
                for k, v in tab_parts.items():
                    if k != 'full':
                        parts[k] = v

                if len(parts) >= 2:
                    sorted_keys = sorted(k for k in parts if k != 'full')
                    assembled = ''.join(parts[k] for k in sorted_keys)
                    if len(assembled) == 6:
                        print(f"    -> assembled from tabs: {assembled}", flush=True)
                        filled = await self._try_fill_code([assembled])
                        if filled:
                            return True

                # Click tabs one by one with delay to trigger content changes
                tab_count = result.get('tabCount', 0)
                if tab_count > 0:
                    for i in range(tab_count):
                        await self.browser.page.evaluate(f"""
                            () => {{
                                const btns = [...document.querySelectorAll('button')];
                                const tabBtns = btns.filter(b => {{
                                    const t = (b.textContent || '').trim().toLowerCase();
                                    return (t.includes('tab') || t.match(/^\\d+$/)) && b.offsetParent;
                                }});
                                if (tabBtns[{i}]) tabBtns[{i}].click();
                            }}
                        """)
                        await asyncio.sleep(0.5)

                    # After visiting all tabs, check for revealed content
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"    -> post-tabs codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            return True

                await asyncio.sleep(0.5)

            return True
        except Exception as e:
            print(f"    -> multi-tab error: {e}", flush=True)
            return False

    async def _try_sequence_challenge(self) -> bool:
        """Handle Sequence Challenge - perform 4 actions: click, hover, type, scroll."""
        try:
            # Detect which actions are needed and their completion status
            state = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    const progMatch = text.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                    const done = progMatch ? parseInt(progMatch[1]) : 0;
                    const total = progMatch ? parseInt(progMatch[2]) : 4;

                    // Detect action items and their completion status
                    const actions = [];
                    // Look for action labels (pill-shaped items showing completion)
                    const allEls = [...document.querySelectorAll('span, div, button, li')];
                    for (const el of allEls) {
                        const t = (el.textContent || '').trim().toLowerCase();
                        const hasCheck = t.includes('✓') || t.includes('✔') || t.includes('☑');
                        if (t.includes('click button') || t.includes('click me')) {
                            actions.push({type: 'click', done: hasCheck, text: t});
                        } else if (t.includes('hover')) {
                            actions.push({type: 'hover', done: hasCheck, text: t});
                        } else if (t.includes('type text') || t.includes('type here')) {
                            actions.push({type: 'type', done: hasCheck, text: t});
                        } else if (t.includes('scroll box') || t.includes('scroll inside')) {
                            actions.push({type: 'scroll', done: hasCheck, text: t});
                        }
                    }
                    return {done, total, actions};
                }
            """)
            print(f"    -> sequence state: {state.get('done')}/{state.get('total')}, "
                  f"actions={state.get('actions')}", flush=True)

            # Action 1: Click the "Click Me" button (if not done)
            click_done = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t.includes('click me') && btn.offsetParent && !btn.disabled) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if click_done:
                print(f"    -> clicked 'Click Me' button", flush=True)
            await asyncio.sleep(0.3)

            # Action 2: Hover over the hover area
            hover_done = await self.browser.page.evaluate("""
                () => {
                    const els = [...document.querySelectorAll('div, span, p')];
                    // Prefer the most specific element (fewest children)
                    let best = null;
                    for (const el of els) {
                        const t = (el.textContent || '').trim().toLowerCase();
                        if ((t === 'hover over this area' || t.includes('hover over')) &&
                            el.offsetParent) {
                            if (!best || el.textContent.length < best.textContent.length) {
                                best = el;
                            }
                        }
                    }
                    if (best) {
                        best.scrollIntoView({behavior: 'instant', block: 'center'});
                        const rect = best.getBoundingClientRect();
                        return {found: true, x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                    }
                    return {found: false};
                }
            """)
            if hover_done.get('found'):
                await self.browser.page.mouse.move(hover_done['x'], hover_done['y'])
                await asyncio.sleep(0.5)
                # Also dispatch mouseenter/mouseover events for React
                await self.browser.page.evaluate(f"""
                    () => {{
                        const el = document.elementFromPoint({hover_done['x']}, {hover_done['y']});
                        if (el) {{
                            el.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true, clientX: {hover_done['x']}, clientY: {hover_done['y']}}}));
                            el.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true, clientX: {hover_done['x']}, clientY: {hover_done['y']}}}));
                        }}
                    }}
                """)
                await asyncio.sleep(0.8)
                print(f"    -> hovered over area", flush=True)

            # Action 3: Type text in the input field
            # Use multiple strategies to find and fill the sequence's text input
            type_done = False
            # Strategy A: Use Playwright locator to find non-code text inputs
            try:
                # Find inputs that are NOT the code submission input
                all_inputs = self.browser.page.locator(
                    'input[type="text"], input:not([type]):not([placeholder*="code" i]):not([placeholder*="Code"])'
                )
                count = await all_inputs.count()
                for i in range(count):
                    inp = all_inputs.nth(i)
                    if await inp.is_visible():
                        placeholder = await inp.get_attribute('placeholder') or ''
                        if 'code' in placeholder.lower():
                            continue
                        # Found a visible non-code text input - use fill() for React
                        await inp.scroll_into_view_if_needed()
                        await inp.click(timeout=1000)
                        await inp.fill("hello world")
                        # Also type a character to trigger keydown/keyup/input events
                        await inp.press("Space")
                        await asyncio.sleep(0.1)
                        await inp.evaluate("""el => {
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            nativeSetter.call(el, el.value || 'hello world');
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }""")
                        type_done = True
                        print(f"    -> typed text in input (Playwright fill)", flush=True)
                        break
            except Exception as e:
                print(f"    -> type input Playwright error: {e}", flush=True)

            if not type_done:
                # Strategy B: JS-based input finding with nativeInputValueSetter
                type_done = await self.browser.page.evaluate("""
                    () => {
                        const inputs = [...document.querySelectorAll('input[type="text"], input:not([type]), textarea')];
                        const nonCodeInputs = inputs.filter(inp => {
                            const ph = (inp.placeholder || '').toLowerCase();
                            return !ph.includes('code') && !ph.includes('proceed') && inp.offsetParent &&
                                   inp.type !== 'number' && inp.type !== 'hidden';
                        });
                        if (nonCodeInputs.length === 0) return false;
                        const inp = nonCodeInputs[0];
                        inp.scrollIntoView({behavior: 'instant', block: 'center'});
                        inp.focus();
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeSetter.call(inp, 'hello world');
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('blur', {bubbles: true}));
                        return true;
                    }
                """)
                if type_done:
                    print(f"    -> typed text in input (JS nativeSetter)", flush=True)
            await asyncio.sleep(0.3)

            # Action 4: Scroll inside the scroll box
            scroll_done = await self.browser.page.evaluate("""
                () => {
                    const els = [...document.querySelectorAll('div, textarea')];
                    for (const el of els) {
                        const t = (el.textContent || '').trim().toLowerCase();
                        const style = getComputedStyle(el);
                        const isScrollable = style.overflow === 'auto' || style.overflow === 'scroll' ||
                            style.overflowY === 'auto' || style.overflowY === 'scroll';
                        if (t.includes('scroll inside') && isScrollable && el.offsetParent) {
                            el.scrollIntoView({behavior: 'instant', block: 'center'});
                            el.scrollTop = el.scrollHeight;
                            const rect = el.getBoundingClientRect();
                            return {found: true, x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                        }
                    }
                    // Fallback: find any scrollable div that's not the page
                    for (const el of els) {
                        const style = getComputedStyle(el);
                        const isScrollable = style.overflow === 'auto' || style.overflow === 'scroll' ||
                            style.overflowY === 'auto' || style.overflowY === 'scroll';
                        if (isScrollable && el.scrollHeight > el.clientHeight + 10 &&
                            el.offsetParent && el.clientHeight < 400 && el.clientHeight > 30) {
                            el.scrollIntoView({behavior: 'instant', block: 'center'});
                            el.scrollTop = el.scrollHeight;
                            const rect = el.getBoundingClientRect();
                            return {found: true, x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                        }
                    }
                    return {found: false};
                }
            """)
            if scroll_done.get('found'):
                # Also use mouse wheel for more realistic scroll
                await self.browser.page.mouse.move(scroll_done['x'], scroll_done['y'])
                await self.browser.page.mouse.wheel(0, 300)
                await asyncio.sleep(0.3)
                print(f"    -> scrolled inside box", flush=True)

            await asyncio.sleep(0.5)

            # Click Complete button
            complete_clicked = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t.includes('complete') && btn.offsetParent && !btn.disabled) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if complete_clicked:
                print(f"    -> clicked Complete", flush=True)
            await asyncio.sleep(0.5)

            # Check final progress
            final = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    const progMatch = text.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                    return {done: progMatch ? parseInt(progMatch[1]) : 0,
                            total: progMatch ? parseInt(progMatch[2]) : 4};
                }
            """)
            print(f"    -> final progress: {final.get('done')}/{final.get('total')}", flush=True)

            return True
        except Exception as e:
            print(f"    -> sequence error: {e}", flush=True)
            return False

    async def _try_math_puzzle_challenge(self) -> bool:
        """Handle Math/Puzzle Challenge - solve expression, type answer, click Solve."""
        try:
            # Step 1: Parse the math expression
            expr = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    const mathMatch = text.match(/(\\d+)\\s*([+\\-*×÷\\/])\\s*(\\d+)\\s*=\\s*\\?/);
                    if (!mathMatch) return null;
                    const a = parseInt(mathMatch[1]);
                    const op = mathMatch[2];
                    const b = parseInt(mathMatch[3]);
                    let answer;
                    switch(op) {
                        case '+': answer = a + b; break;
                        case '-': answer = a - b; break;
                        case '*': case '×': answer = a * b; break;
                        case '/': case '÷': answer = Math.floor(a / b); break;
                        default: answer = a + b;
                    }
                    return {a, op, b, answer};
                }
            """)
            if not expr:
                return False
            print(f"    -> puzzle: {expr['a']} {expr['op']} {expr['b']} = {expr['answer']}", flush=True)

            # Step 2: Find and fill the number input via JS
            await self.browser.page.evaluate(f"""
                () => {{
                    const input = document.querySelector('input[type="number"]') ||
                                  document.querySelector('input[inputmode="numeric"]');
                    if (input) {{
                        input.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(input, '{expr["answer"]}');
                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}
            """)
            await asyncio.sleep(0.3)

            # Step 3: Click Solve button - focus input first, then try multiple safe approaches
            await self.browser.page.evaluate("""
                () => {
                    const input = document.querySelector('input[type="number"]') ||
                                  document.querySelector('input[inputmode="numeric"]');
                    if (input) {
                        input.scrollIntoView({behavior: 'instant', block: 'center'});
                        input.focus();
                    }
                }
            """)
            await asyncio.sleep(0.1)

            # Primary: press Enter on the focused number input (safest, avoids trap buttons)
            await self.browser.page.keyboard.press('Enter')
            print(f"    -> pressed Enter on puzzle input", flush=True)
            await asyncio.sleep(0.5)

            # Check if puzzle was solved by Enter
            puzzle_solved = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    return text.includes('solved') || text.includes('Code revealed') ||
                           text.includes('code revealed') || text.includes('Solved');
                }
            """)

            if not puzzle_solved:
                # Try clicking ONLY buttons with text "Solve"/"Check"/"Verify" near the input
                solved = await self.browser.page.evaluate("""
                    () => {
                        const input = document.querySelector('input[type="number"]') ||
                                      document.querySelector('input[inputmode="numeric"]');
                        if (!input) return false;
                        // Search in input's parent containers (up to 3 levels)
                        let container = input.parentElement;
                        for (let i = 0; i < 3 && container; i++) {
                            const btns = container.querySelectorAll('button');
                            for (const btn of btns) {
                                const t = (btn.textContent || '').trim().toLowerCase();
                                if ((t.includes('solve') || t.includes('check') || t.includes('verify') || t === 'go') && !btn.disabled) {
                                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                                    btn.click();
                                    return 'nearby_click: ' + t;
                                }
                            }
                            container = container.parentElement;
                        }
                        // Global fallback: ONLY click buttons explicitly saying "Solve"
                        const allBtns = [...document.querySelectorAll('button')];
                        for (const btn of allBtns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t === 'solve' && !btn.disabled) {
                                btn.scrollIntoView({behavior: 'instant', block: 'center'});
                                btn.click();
                                return 'global_solve: ' + t;
                            }
                        }
                        return false;
                    }
                """)
                if solved:
                    print(f"    -> solve button: {solved}", flush=True)

                if not solved:
                    # Playwright fallback - only exact matches
                    for text in ['Solve', 'Check', 'Verify']:
                        try:
                            await self.browser.page.click(f"button:has-text('{text}')", timeout=1000)
                            print(f"    -> Playwright clicked '{text}'", flush=True)
                            break
                        except Exception:
                            continue

            await asyncio.sleep(1.0)
            return True
        except Exception as e:
            print(f"    -> math puzzle error: {e}", flush=True)
            return False

    async def _try_audio_challenge(self) -> bool:
        """Handle Audio Challenge - intercept SpeechSynthesis/Audio/network, force-end speech."""
        try:
            import base64
            from google.genai import types

            # Patches are already installed via add_init_script in browser.py.
            # Reset capture state for this attempt.
            await self.browser.page.evaluate("""
                () => {
                    window.__capturedSpeechTexts = window.__capturedSpeechTexts || [];
                    window.__capturedSpeechUtterance = window.__capturedSpeechUtterance || null;
                    window.__speechDone = false;
                }
            """)

            # Network response listener for audio files
            audio_capture = {'data': None, 'mime': None}

            async def on_audio_response(response):
                ct = response.headers.get('content-type', '')
                url = response.url
                is_audio = ('audio' in ct or
                            any(ext in url.lower() for ext in ['.mp3', '.wav', '.ogg', '.webm', '.m4a']))
                if is_audio:
                    try:
                        body = await response.body()
                        audio_capture['data'] = body
                        audio_capture['mime'] = ct.split(';')[0] if ct else 'audio/mpeg'
                        print(f"    -> captured audio via network: {len(body)} bytes", flush=True)
                    except Exception:
                        pass

            self.browser.page.on('response', on_audio_response)

            # === PHASE 1: Click Play Audio button (but NOT "Playing...") ===
            play_result = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if (text.includes('play') && !text.includes('playing') && btn.offsetParent) {
                            btn.click();
                            return 'clicked';
                        }
                    }
                    for (const btn of btns) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if (text.includes('playing')) return 'already_playing';
                    }
                    return 'not_found';
                }
            """)
            if play_result == 'not_found':
                self.browser.page.remove_listener('response', on_audio_response)
                print(f"    -> no Play Audio button found", flush=True)
                return False
            print(f"    -> audio: {play_result}", flush=True)

            # === PHASE 2: Wait briefly for speech, then force-end it ===
            # Playwright's Chromium often has no TTS voices, so speechSynthesis.speak()
            # never actually plays and the 'end' event never fires.
            # Wait 3 seconds (enough for real speech), then force-trigger the end.
            await asyncio.sleep(3.0)

            # Check what we captured
            captured_info = await self.browser.page.evaluate("""
                () => ({
                    speechTexts: window.__capturedSpeechTexts || [],
                    audioSrc: window.__capturedAudioSrc,
                    blobUrl: window.__capturedBlobUrl || null,
                    hasBlobData: !!window.__capturedBlob,
                    hasUtterance: !!window.__capturedSpeechUtterance,
                    speaking: window.speechSynthesis ? window.speechSynthesis.speaking : false,
                })
            """)
            speech_texts = captured_info.get('speechTexts', [])
            print(f"    -> captured: speech={speech_texts}, audioSrc={bool(captured_info.get('audioSrc'))}, "
                  f"speaking={captured_info.get('speaking')}, hasUtt={captured_info.get('hasUtterance')}", flush=True)

            # Force-end the speech and dispatch 'end' event on the utterance.
            # This triggers the React component's onend callback -> sets isPlaying=false
            # -> button changes from "Playing..." to "Complete".
            force_result = await self.browser.page.evaluate("""
                () => {
                    const result = {speechCanceled: false, endDispatched: false, onendCalled: false};

                    // Cancel speechSynthesis
                    if (window.speechSynthesis) {
                        window.speechSynthesis.cancel();
                        result.speechCanceled = true;
                    }

                    // Dispatch 'end' event on the captured utterance
                    const utt = window.__capturedSpeechUtterance;
                    if (utt) {
                        // Try SpeechSynthesisEvent first
                        try {
                            utt.dispatchEvent(new SpeechSynthesisEvent('end', {utterance: utt}));
                            result.endDispatched = true;
                        } catch(e) {
                            try {
                                utt.dispatchEvent(new Event('end'));
                                result.endDispatched = true;
                            } catch(e2) {}
                        }
                        // Also call onend directly (handles case where challenge set it after speak)
                        if (utt.onend) {
                            try {
                                utt.onend(new Event('end'));
                                result.onendCalled = true;
                            } catch(e) {}
                        }
                    }

                    // Also force-end any Audio elements
                    if (window.__capturedAudio) {
                        window.__capturedAudio.pause();
                        if (window.__capturedAudio.duration && isFinite(window.__capturedAudio.duration))
                            window.__capturedAudio.currentTime = window.__capturedAudio.duration;
                        window.__capturedAudio.dispatchEvent(new Event('ended'));
                    }
                    document.querySelectorAll('audio').forEach(a => {
                        a.pause();
                        if (a.duration && isFinite(a.duration)) a.currentTime = a.duration;
                        a.dispatchEvent(new Event('ended'));
                    });

                    return result;
                }
            """)
            print(f"    -> force-end: {force_result}", flush=True)

            # Wait for React to re-render after forced state change
            await asyncio.sleep(1.0)

            # Gather transcript
            transcript = None
            if speech_texts:
                transcript = ' '.join(speech_texts)
                print(f"    -> speech transcript: '{transcript}'", flush=True)

            # If we have audio from network, transcribe with Gemini
            if not transcript and audio_capture['data']:
                audio_bytes = audio_capture['data']
                mime_type = audio_capture['mime'] or 'audio/mpeg'
                print(f"    -> sending {len(audio_bytes)} bytes ({mime_type}) to Gemini...", flush=True)
                try:
                    response = self.vision.client.models.generate_content(
                        model=self.vision.model_name,
                        contents=[
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                                    types.Part.from_text(text="Transcribe this audio exactly. It contains a 6-character alphanumeric code being spoken/spelled out. Return ONLY the code (6 characters, letters and numbers), nothing else.")
                                ]
                            )
                        ],
                        config=types.GenerateContentConfig(temperature=0.0)
                    )
                    transcript = response.text.strip()
                    print(f"    -> audio transcript: '{transcript}'", flush=True)
                except Exception as e:
                    print(f"    -> transcription error: {e}", flush=True)

            # Try fetching from captured audio src
            if not transcript and captured_info.get('audioSrc'):
                print(f"    -> trying fetch from captured src...", flush=True)
                audio_info = await self.browser.page.evaluate("""
                    async () => {
                        const src = window.__capturedAudioSrc;
                        if (!src) return {found: false};
                        try {
                            const resp = await fetch(src);
                            const ct = resp.headers.get('content-type') || 'audio/mpeg';
                            const ab = await resp.arrayBuffer();
                            const bytes = new Uint8Array(ab);
                            let bin = '';
                            for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                            return {found: true, data: btoa(bin), mime: ct};
                        } catch(e) { return {found: false, error: e.message}; }
                    }
                """)
                if audio_info.get('found'):
                    audio_bytes = base64.b64decode(audio_info['data'])
                    mime_type = audio_info.get('mime', 'audio/mpeg').split(';')[0]
                    print(f"    -> captured audio via src fetch: {len(audio_bytes)} bytes", flush=True)
                    try:
                        response = self.vision.client.models.generate_content(
                            model=self.vision.model_name,
                            contents=[
                                types.Content(
                                    role="user",
                                    parts=[
                                        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                                        types.Part.from_text(text="Transcribe this audio exactly. It contains a 6-character alphanumeric code. Return ONLY the code.")
                                    ]
                                )
                            ],
                            config=types.GenerateContentConfig(temperature=0.0)
                        )
                        transcript = response.text.strip()
                        print(f"    -> src transcript: '{transcript}'", flush=True)
                    except Exception as e:
                        print(f"    -> src transcription error: {e}", flush=True)

            self.browser.page.remove_listener('response', on_audio_response)

            if not transcript and not audio_capture['data'] and not speech_texts:
                print(f"    -> no audio data captured by any method", flush=True)

            # === PHASE 3: Click Complete button ===
            complete_clicked = False
            for poll in range(6):
                btn_state = await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const btn of btns) {
                            const text = (btn.textContent || '').trim().toLowerCase();
                            if ((text.includes('complete') || text.includes('done') || text.includes('finish')) &&
                                !text.includes('playing') && btn.offsetParent && !btn.disabled) {
                                btn.click();
                                return 'clicked';
                            }
                        }
                        return 'waiting';
                    }
                """)
                if btn_state == 'clicked':
                    print(f"    -> clicked Complete", flush=True)
                    complete_clicked = True
                    break
                await asyncio.sleep(0.5)

            if not complete_clicked:
                # Fallback: try Playwright click
                for text in ['Complete', 'Done', 'Finish']:
                    try:
                        loc = self.browser.page.get_by_text(text, exact=False)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=1000)
                            print(f"    -> Playwright clicked '{text}'", flush=True)
                            complete_clicked = True
                            break
                    except Exception:
                        continue

            if not complete_clicked:
                # Last resort: click the "Playing..." button itself
                # (it might be the same button that toggles to Complete)
                print(f"    -> Complete not found, clicking Playing button as fallback", flush=True)
                await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const btn of btns) {
                            const text = (btn.textContent || '').trim().toLowerCase();
                            if (text.includes('playing') && btn.offsetParent) {
                                btn.click();
                                return;
                            }
                        }
                    }
                """)

            await asyncio.sleep(1.0)

            # === PHASE 4: Extract codes from DOM after Complete reveals them ===
            html = await self.browser.get_html()
            dom_codes = extract_hidden_codes(html)
            if dom_codes:
                print(f"    -> post-complete dom_codes: {dom_codes}", flush=True)
                filled = await self._try_fill_code(dom_codes)
                if filled:
                    return True

            # Try transcript-based codes (the hint itself might be the code,
            # or the code might be embedded in the speech text)
            if transcript:
                import re
                codes_to_try = []
                # Extract the hint after "hint is:" pattern
                hint_match = re.search(r'hint\s+is[:\s]+(.+)', transcript, re.IGNORECASE)
                if hint_match:
                    hint_part = hint_match.group(1).strip()
                    hint_code = re.sub(r'[^A-Z0-9]', '', hint_part.upper())
                    if len(hint_code) == 6:
                        codes_to_try.append(hint_code)

                # Also try the whole transcript cleaned up
                clean_word = re.sub(r'[^A-Z0-9]', '', transcript.upper())
                if len(clean_word) == 6 and clean_word not in codes_to_try:
                    codes_to_try.append(clean_word)

                # Find all 6-char sequences
                found = re.findall(r'[A-Z0-9]{6}', transcript.upper().replace(' ', ''))
                for f in found:
                    if f not in codes_to_try:
                        codes_to_try.append(f)

                # Individual words
                for w in transcript.upper().split():
                    w = re.sub(r'[^A-Z0-9]', '', w)
                    if len(w) == 6 and w not in codes_to_try:
                        codes_to_try.append(w)

                # Spelled-out letters (e.g., "P 4 H W B Q" -> "P4HWBQ")
                letters = [w.strip().upper() for w in transcript.split() if len(w.strip()) == 1]
                if len(letters) >= 6:
                    spelled = ''.join(letters[:6])
                    if spelled not in codes_to_try:
                        codes_to_try.append(spelled)

                if codes_to_try:
                    print(f"    -> audio codes to try: {codes_to_try}", flush=True)
                    filled = await self._try_fill_code(codes_to_try)
                    if filled:
                        return True

            return True

        except Exception as e:
            print(f"    -> audio error: {e}", flush=True)
            return False

    async def _try_video_challenge(self) -> bool:
        """Handle Video Challenge - seek through frames to target, read code, submit."""
        try:
            # Parse target frame and current state from the page
            state = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';

                    // Find target frame number (e.g., "navigate to frame 44")
                    const targetMatch = text.match(/(?:frame|Frame)\\s+(\\d+)/g);
                    let targetFrame = null;
                    if (targetMatch) {
                        for (const m of targetMatch) {
                            const num = parseInt(m.match(/\\d+/)[0]);
                            // Skip "Frame 0/60" style current-frame indicators
                            if (num > 0 && num < 100) {
                                targetFrame = num;
                                break;
                            }
                        }
                    }

                    // Find current frame
                    const currentMatch = text.match(/Frame\\s+(\\d+)\\/(\\d+)/);
                    const currentFrame = currentMatch ? parseInt(currentMatch[1]) : 0;
                    const totalFrames = currentMatch ? parseInt(currentMatch[2]) : 60;

                    // Find seek requirement
                    const seekMatch = text.match(/(\\d+)\\/(\\d+)\\s*required/);
                    const seeksDone = seekMatch ? parseInt(seekMatch[1]) : 0;
                    const seeksRequired = seekMatch ? parseInt(seekMatch[2]) : 3;

                    // Find buttons
                    const btns = [...document.querySelectorAll('button')];
                    const btnTexts = btns.filter(b => b.offsetParent).map(b => b.textContent.trim());

                    return {targetFrame, currentFrame, totalFrames, seeksDone, seeksRequired, btnTexts};
                }
            """)
            print(f"    -> video state: target={state.get('targetFrame')}, "
                  f"current={state.get('currentFrame')}/{state.get('totalFrames')}, "
                  f"seeks={state.get('seeksDone')}/{state.get('seeksRequired')}", flush=True)

            target = state.get('targetFrame')
            if target is None:
                print(f"    -> no target frame found", flush=True)
                return False

            seeks_required = state.get('seeksRequired', 3)
            seeks_done = state.get('seeksDone', 0)

            # Step 1: Perform required seek operations using +1/-1/+10/-10 buttons
            seek_count = 0
            while seeks_done + seek_count < seeks_required:
                # Click +1 button (simple, reliable seek)
                clicked = await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const btn of btns) {
                            const text = btn.textContent.trim();
                            if (text === '+1' && btn.offsetParent) {
                                btn.click();
                                return true;
                            }
                        }
                        // Fallback: click any seek button
                        for (const btn of btns) {
                            const text = btn.textContent.trim();
                            if ((text === '-1' || text === '+10' || text === '-10') && btn.offsetParent) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if clicked:
                    seek_count += 1
                    print(f"    -> seek {seeks_done + seek_count}/{seeks_required}", flush=True)
                    await asyncio.sleep(0.3)
                else:
                    break

            # Step 2: Navigate to the target frame
            # Try clicking "Frame N" button directly
            nav_result = await self.browser.page.evaluate(f"""
                () => {{
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {{
                        const text = btn.textContent.trim();
                        if (text.includes('Frame {target}') || text.includes('frame {target}')) {{
                            btn.click();
                            return 'direct';
                        }}
                    }}
                    return 'not_found';
                }}
            """)
            print(f"    -> navigate to frame {target}: {nav_result}", flush=True)

            if nav_result == 'not_found':
                # Manual navigation: use +10/-10 and +1/-1 to reach target
                for _ in range(20):
                    current = await self.browser.page.evaluate("""
                        () => {
                            const text = document.body.textContent || '';
                            const m = text.match(/Frame\\s+(\\d+)\\//);
                            return m ? parseInt(m[1]) : 0;
                        }
                    """)
                    if current == target:
                        break
                    diff = target - current
                    if abs(diff) >= 10:
                        btn_text = '+10' if diff > 0 else '-10'
                    else:
                        btn_text = '+1' if diff > 0 else '-1'
                    await self.browser.page.evaluate(f"""
                        () => {{
                            const btns = [...document.querySelectorAll('button')];
                            for (const btn of btns) {{
                                if (btn.textContent.trim() === '{btn_text}' && btn.offsetParent) {{
                                    btn.click();
                                    return;
                                }}
                            }}
                        }}
                    """)
                    await asyncio.sleep(0.2)

            await asyncio.sleep(0.5)

            # Step 3: Click any "Seek N more times" or completion button
            await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const text = btn.textContent.trim().toLowerCase();
                        if ((text.includes('complete') || text.includes('done') ||
                             text.includes('reveal') || text.includes('submit')) &&
                            btn.offsetParent && !btn.disabled) {
                            btn.click();
                        }
                    }
                }
            """)
            await asyncio.sleep(0.5)

            # Step 4: Read the code displayed at the target frame
            frame_code = await self.browser.page.evaluate(f"""
                () => {{
                    const text = document.body.textContent || '';
                    // Check we're on the right frame
                    const frameMatch = text.match(/Frame\\s+(\\d+)\\//);
                    const currentFrame = frameMatch ? parseInt(frameMatch[1]) : -1;

                    // Look for 6-char code in the video area
                    const codeMatch = text.match(/\\b([A-Z0-9]{{6}})\\b/g);
                    return {{currentFrame, codes: codeMatch || []}};
                }}
            """)
            print(f"    -> at frame {frame_code.get('currentFrame')}, codes: {frame_code.get('codes')}", flush=True)

            return True

        except Exception as e:
            print(f"    -> video error: {e}", flush=True)
            return False

    async def _try_drag_and_drop(self) -> bool:
        """Handle Drag-and-Drop Challenge - fill slots with pieces to reveal code."""
        try:
            # Step 1: Hide floating decoy elements that obstruct drag area
            await self.browser.page.evaluate("""
                () => {
                    document.querySelectorAll('div, button, a, span').forEach(el => {
                        const style = getComputedStyle(el);
                        const text = (el.textContent || '').trim();
                        if (style.position === 'absolute' || style.position === 'fixed') {
                            if (['Click Me!', 'Button!', 'Link!', 'Here!', 'Click Here', 'Click Here!', 'Try This!'].includes(text)) {
                                el.style.display = 'none';
                                el.style.pointerEvents = 'none';
                            }
                        }
                    });
                }
            """)
            await asyncio.sleep(0.3)

            # Step 2: Try JS-based DragEvent simulation
            js_result = await self.browser.page.evaluate("""
                () => {
                    const pieces = [...document.querySelectorAll('[draggable="true"]')];
                    if (pieces.length === 0) return {filled: 0, error: 'no pieces'};

                    // Find drop zones - dashed border elements containing "Slot N"
                    const allDivs = [...document.querySelectorAll('div')];
                    const slots = allDivs.filter(el => {
                        const text = (el.textContent || '').trim();
                        const classes = el.className || '';
                        const style = el.getAttribute('style') || '';
                        return (text.match(/^Slot \\d+$/) &&
                               (classes.includes('dashed') || style.includes('dashed'))) ||
                               (classes.includes('border-dashed') && el.children.length <= 2 && el.offsetWidth > 40);
                    });

                    if (slots.length === 0) return {filled: 0, error: 'no slots', pieces: pieces.length};

                    let filled = 0;
                    const numToFill = Math.min(pieces.length, slots.length, 6);

                    for (let i = 0; i < numToFill; i++) {
                        try {
                            const piece = pieces[i];
                            const slot = slots[i];
                            const dt = new DataTransfer();
                            dt.setData('text/plain', piece.textContent.trim());

                            piece.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            slot.dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            slot.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            slot.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            piece.dispatchEvent(new DragEvent('dragend', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            filled++;
                        } catch(e) {}
                    }

                    return {filled, pieces: pieces.length, slots: slots.length};
                }
            """)
            print(f"    -> drag JS: {js_result}", flush=True)

            # Check actual fill count from page (JS may over-report)
            fill_count = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    const match = text.match(/(\\d+)\\/(\\d+)\\s*filled/);
                    return match ? parseInt(match[1]) : 0;
                }
            """)
            print(f"    -> actual fill: {fill_count}/6", flush=True)
            if fill_count >= 6:
                await asyncio.sleep(0.5)
                return True

            # Step 3: Playwright mouse drag for remaining empty slots
            # Use JS to get positions of unplaced pieces and empty slots
            for drag_round in range(6):
                state = await self.browser.page.evaluate("""
                    () => {
                        const text = document.body.textContent || '';
                        const match = text.match(/(\\d+)\\/(\\d+)\\s*filled/);
                        const filled = match ? parseInt(match[1]) : 0;
                        if (filled >= 6) return {filled, done: true};

                        // Find empty slots (still show "Slot N" text)
                        const emptySlots = [...document.querySelectorAll('div')].filter(el => {
                            const t = (el.textContent || '').trim();
                            return t.match(/^Slot \\d+$/) &&
                                   (el.className.includes('dashed') || (el.getAttribute('style') || '').includes('dashed'));
                        }).map(el => {
                            const rect = el.getBoundingClientRect();
                            return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                        });

                        // Find available pieces NOT inside drop zones
                        // Pieces in the "available" area are above the drop zone area
                        const dropZones = [...document.querySelectorAll('[class*="border-dashed"]')];
                        const dropZoneSet = new Set(dropZones);
                        const pieces = [...document.querySelectorAll('[draggable="true"]')].filter(el => {
                            // Skip if this piece is inside a drop zone
                            let parent = el.parentElement;
                            while (parent) {
                                if (dropZoneSet.has(parent)) return false;
                                parent = parent.parentElement;
                            }
                            return el.offsetParent !== null;
                        }).map(el => {
                            const rect = el.getBoundingClientRect();
                            return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, text: el.textContent.trim()};
                        });

                        return {filled, done: false, emptySlots, pieces: pieces.slice(0, 6)};
                    }
                """)

                if state.get('done') or state.get('filled', 0) >= 6:
                    print(f"    -> all slots filled!", flush=True)
                    return True

                empty_slots = state.get('emptySlots', [])
                avail_pieces = state.get('pieces', [])
                if not empty_slots or not avail_pieces:
                    print(f"    -> no empty slots ({len(empty_slots)}) or pieces ({len(avail_pieces)})", flush=True)
                    break

                # Drag first available piece to first empty slot
                piece = avail_pieces[0]
                slot = empty_slots[0]
                try:
                    await self.browser.page.mouse.move(piece['x'], piece['y'])
                    await self.browser.page.mouse.down()
                    await asyncio.sleep(0.05)
                    await self.browser.page.mouse.move(slot['x'], slot['y'], steps=15)
                    await asyncio.sleep(0.05)
                    await self.browser.page.mouse.up()
                    print(f"    -> dragged '{piece['text']}' to empty slot (round {drag_round+1})", flush=True)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"    -> drag round {drag_round+1} failed: {e}", flush=True)

            # Final count check
            final = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.textContent || '';
                    const match = text.match(/(\\d+)\\/(\\d+)\\s*filled/);
                    return match ? parseInt(match[1]) : 0;
                }
            """)
            print(f"    -> final fill: {final}/6", flush=True)
            return final > 0

        except Exception as e:
            print(f"    -> drag-and-drop error: {e}", flush=True)
            return False

    async def _brute_force_click(self) -> dict:
        """Click buttons aggressively - no penalty for wrong clicks. Returns what was clicked."""
        return await self.browser.page.evaluate("""
            () => {
                const result = {accept: 0, red: 0, gray: 0, submit: 0, reveal: 0, skipped_traps: 0};
                const clicked = new Set();

                // Detect trap button pages: many navigation-style buttons = don't click them
                const TRAP_WORDS = ['proceed', 'continue reading', 'next step', 'next page',
                    'next section', 'move on', 'go forward', 'keep going', 'advance',
                    'continue journey', 'click here', 'proceed forward'];
                const allBtns = [...document.querySelectorAll('button')];
                let trapCount = 0;
                for (const btn of allBtns) {
                    const t = (btn.textContent || '').trim().toLowerCase();
                    if (TRAP_WORDS.some(w => t.includes(w))) trapCount++;
                }
                const hasTrapButtons = trapCount >= 3;

                // Click Accept buttons first
                document.querySelectorAll('button').forEach(btn => {
                    if (btn.textContent.includes('Accept') && btn.offsetParent) {
                        btn.click();
                        clicked.add(btn);
                        result.accept++;
                    }
                });

                // Click "Reveal Code" or similar buttons EARLY
                document.querySelectorAll('button').forEach(btn => {
                    const text = btn.textContent.toLowerCase();
                    if ((text.includes('reveal') || text.includes('show') || text.includes('unlock')) && btn.offsetParent && !clicked.has(btn)) {
                        btn.click();
                        clicked.add(btn);
                        result.reveal++;
                    }
                });

                // Click ALL X-like buttons (red, gray, any close button)
                // BUT skip red/pink buttons on trap button pages
                document.querySelectorAll('button').forEach(btn => {
                    if (btn.offsetParent && !clicked.has(btn)) {
                        const style = getComputedStyle(btn);
                        const bg = style.backgroundColor;
                        const text = btn.textContent.trim();

                        // Check for X symbol (always safe to click)
                        if (text === '×' || text === 'X' || text === '✕') {
                            btn.click();
                            clicked.add(btn);
                            result.gray++;
                            return;
                        }

                        // On trap pages, skip ALL red/pink buttons (they're decoys)
                        if (hasTrapButtons) {
                            const match = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                            if (match) {
                                const [_, r, g, b] = match.map(Number);
                                if (r > 180 && g < 100 && b < 100) {
                                    result.skipped_traps++;
                                    return;
                                }
                                // Also skip pink (high R, medium G/B)
                                if (r > 200 && g > 80 && g < 160 && b > 80 && b < 170) {
                                    result.skipped_traps++;
                                    return;
                                }
                            }
                        }

                        // Check for red/pink background (only on non-trap pages)
                        const match = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                        if (match) {
                            const [_, r, g, b] = match.map(Number);
                            // Red buttons
                            if (r > 180 && g < 100 && b < 100) {
                                btn.click();
                                clicked.add(btn);
                                result.red++;
                            }
                            // Gray buttons (close buttons often gray)
                            else if (r > 100 && r < 180 && g > 100 && g < 180 && b > 100 && b < 180) {
                                btn.click();
                                clicked.add(btn);
                                result.gray++;
                            }
                        }
                    }
                });

                // Click Submit buttons
                document.querySelectorAll('button').forEach(btn => {
                    if (btn.textContent.includes('Submit') && btn.offsetParent && !clicked.has(btn)) {
                        btn.click();
                        result.submit++;
                    }
                });

                return result;
            }
        """)


    async def _try_radio_selection(self) -> bool:
        """Try to select correct radio option using Playwright native clicks.
        Multiple options may SOUND correct but only one actually works.
        Try each, submit, check URL - if wrong, try next option."""
        try:
            # First, scroll modal to TOP so radio options are visible
            await self.browser.page.evaluate("""
                () => {
                    document.querySelectorAll('[class*="overflow-y"], [class*="overflow-auto"]').forEach(el => {
                        if (el.scrollHeight > el.clientHeight) {
                            el.scrollTop = 0;
                        }
                    });
                }
            """)
            await asyncio.sleep(0.3)

            url_before = await self.browser.get_url()

            # Try each pattern - multiple may exist but only one is correct
            patterns = [
                'Correct answer', 'correct answer',
                'This is correct', 'this is correct',
                'The right choice', 'the right choice',
                'Correct Choice', 'correct choice',
                'Right answer', 'right answer',
            ]
            any_clicked = False
            for pattern in patterns:
                try:
                    locator = self.browser.page.get_by_text(pattern, exact=False)
                    count = await locator.count()
                    if count > 0:
                        await locator.first.click(timeout=1000)
                        print(f"    -> Playwright clicked option: '{pattern}'", flush=True)
                        any_clicked = True
                        await asyncio.sleep(0.3)

                        # Click Submit & Continue
                        submitted = False
                        try:
                            submit = self.browser.page.get_by_text('Submit & Continue')
                            if await submit.count() > 0:
                                await submit.first.click(timeout=1000)
                                submitted = True
                            else:
                                submit = self.browser.page.get_by_text('Submit and Continue')
                                if await submit.count() > 0:
                                    await submit.first.click(timeout=1000)
                                    submitted = True
                        except Exception:
                            pass

                        if submitted:
                            print(f"    -> Clicked Submit & Continue", flush=True)
                            await asyncio.sleep(0.5)
                            url_after = await self.browser.get_url()
                            if url_after != url_before:
                                print(f"    -> Option '{pattern}' was correct!", flush=True)
                                return True
                            print(f"    -> '{pattern}' was wrong, trying next", flush=True)
                except Exception:
                    continue

            return any_clicked
        except Exception as e:
            print(f"    -> radio selection error: {e}", flush=True)
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
        """Try to fill each code into input field. Returns True if any code was submitted."""
        # Skip codes that already failed on THIS step (not previous steps - same code can be valid across steps)
        untried = [c for c in codes if c not in self.failed_codes_this_step]
        if not untried and codes:
            print(f"    -> all {len(codes)} codes already tried this step, skipping", flush=True)
            return False

        url_before = await self.browser.get_url()
        any_submitted = False

        for code in untried:
            try:
                # Scroll the code input into view via JS first
                await self.browser.page.evaluate("""
                    () => {
                        const input = document.querySelector('input[placeholder*="code"], input[placeholder*="Code"], input[type="text"]');
                        if (input) input.scrollIntoView({behavior: 'instant', block: 'center'});
                    }
                """)
                await asyncio.sleep(0.1)

                # Find the input field
                input_loc = self.browser.page.locator(
                    'input[placeholder*="code"], input[placeholder*="Code"], input[type="text"]'
                ).first
                if not await input_loc.count():
                    print(f"    -> no input field found", flush=True)
                    return False

                # Clear properly for React: triple-click to select all, then backspace
                try:
                    await input_loc.click(click_count=3, timeout=1000)
                except Exception:
                    # Fallback: floating elements may block click - use JS focus + select
                    await self.browser.page.evaluate("""
                        () => {
                            const input = document.querySelector('input[placeholder*="code"], input[placeholder*="Code"], input[type="text"]');
                            if (input) { input.focus(); input.select(); }
                        }
                    """)
                await self.browser.page.keyboard.press('Backspace')
                await asyncio.sleep(0.1)

                # Type character by character - this properly triggers React state updates
                await self.browser.page.keyboard.type(code, delay=30)
                print(f"    -> typed '{code}'", flush=True)

                # Wait for React to update
                await asyncio.sleep(0.2)

                # Click submit button via JS - find button NEAR the code input (avoid trap buttons)
                clicked = await self.browser.page.evaluate("""
                    () => {
                        const TRAP_WORDS = ['proceed', 'continue', 'next step', 'next page',
                            'next section', 'move on', 'go forward', 'keep going', 'advance',
                            'continue reading', 'continue journey', 'click here', 'proceed forward'];
                        const isTrap = (t) => TRAP_WORDS.some(w => t.toLowerCase().includes(w));

                        const input = document.querySelector('input[placeholder*="code" i], input[placeholder*="Code"], input[type="text"]');
                        if (!input) return false;

                        // Strategy 1: Find submit/go button in same container as input (up to 3 levels)
                        let container = input.parentElement;
                        for (let i = 0; i < 3 && container; i++) {
                            const btns = container.querySelectorAll('button');
                            for (const btn of btns) {
                                const t = (btn.textContent || '').trim();
                                if (!btn.disabled && !isTrap(t) &&
                                    (btn.type === 'submit' || t.includes('Submit') || t.includes('Go') || t === '→' || t === '>' || t.length <= 2)) {
                                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                                    btn.click();
                                    return true;
                                }
                            }
                            // Also: if only 1 non-trap button in container, click it
                            const nonTrapBtns = [...btns].filter(b => !b.disabled && !isTrap((b.textContent || '').trim()));
                            if (nonTrapBtns.length === 1) {
                                nonTrapBtns[0].scrollIntoView({behavior: 'instant', block: 'center'});
                                nonTrapBtns[0].click();
                                return true;
                            }
                            container = container.parentElement;
                        }

                        // Strategy 2: Find button in same form as input
                        const form = input.closest('form');
                        if (form) {
                            const btn = form.querySelector('button[type="submit"], button');
                            if (btn && !btn.disabled) {
                                btn.scrollIntoView({behavior: 'instant', block: 'center'});
                                btn.click();
                                return true;
                            }
                        }

                        // Strategy 3: Global fallback - only exact "Submit" text (not trap words)
                        const allBtns = document.querySelectorAll('button');
                        for (const b of allBtns) {
                            const t = (b.textContent || '').trim();
                            if (t === 'Submit' && !b.disabled) {
                                b.scrollIntoView({behavior: 'instant', block: 'center'});
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                if clicked:
                    print(f"    -> clicked Submit", flush=True)
                else:
                    await self.browser.page.keyboard.press('Enter')
                    print(f"    -> pressed Enter", flush=True)

                any_submitted = True

                # Check if URL changed (code was correct)
                await asyncio.sleep(0.5)
                url_after = await self.browser.get_url()
                if url_after != url_before:
                    print(f"    -> URL changed! Code {code} worked", flush=True)
                    return True

                # Code was wrong - track it so we don't retry it this step
                self.failed_codes_this_step.add(code)
                print(f"    -> code {code} didn't work, trying next", flush=True)
            except Exception as e:
                print(f"    -> fill error: {e}", flush=True)

        return any_submitted

    async def _close_blocking_popups(self) -> None:
        """Close any popups that might be blocking interactions."""
        # Try to close popups with X button (not Dismiss which is fake)
        close_selectors = [
            "button:has-text('×')",
            "button:has-text('✕')",
            "button:has-text('X'):not(:has-text('Dismiss'))",
            "[aria-label*='close' i]",
            ".close-button",
            "button:has(svg)",  # Often X buttons use SVG icons
        ]
        for sel in close_selectors:
            try:
                await self.browser.page.click(sel, timeout=300)
                await asyncio.sleep(0.1)
            except Exception:
                continue

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

        elif action.action_type == ActionType.HOVER:
            if action.target_selector:
                try:
                    el = self.browser.page.locator(action.target_selector)
                    if await el.count() > 0:
                        await el.first.hover(timeout=2000)
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

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
