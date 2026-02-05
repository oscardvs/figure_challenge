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
            # Wait for page to load and click START button
            await asyncio.sleep(1.5)
            print("Clicking START button...", flush=True)
            start_clicked = await self.browser.click_by_text("START")
            if not start_clicked:
                start_clicked = await self.browser.click("button:has-text('Start')")
            if not start_clicked:
                start_clicked = await self.browser.click("a:has-text('Start')")
            print(f"START clicked: {start_clicked}")
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

        # Wait for React to render the page content
        content_loaded = await self._wait_for_content()
        if not content_loaded:
            print(f"  WARNING: page content didn't load, continuing anyway", flush=True)

        for attempt in range(15):  # Reduced from 20 to save time budget
            url = await self.browser.get_url()
            print(f"  [{attempt+1}] url={url[-35:]}", flush=True)

            # Log page text on first attempt for debugging
            if attempt == 0:
                try:
                    page_text = await self.browser.page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
                    print(f"  page_text: {page_text[:400]}", flush=True)
                except Exception:
                    pass

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
            # More precise detection: require "hover" near reveal/code instructions
            is_hover_challenge = ('hover' in html_lower and 'reveal' in html_lower) or \
                                 ('hover over' in html_lower) or ('hover area' in html_lower)
            if is_hover_challenge:
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
                    # Extract code with enhanced post-puzzle extraction
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    # Also try to extract the code from the "Puzzle solved" text directly
                    puzzle_code = await self.browser.page.evaluate("""
                        () => {
                            const text = document.body.textContent || '';
                            const blacklist = new Set(['AWRONG', 'PUZZLE', 'SOLVED', 'ONMOVE', 'REVEAL']);

                            // Look for "Code: XXXXXX" or "Code XXXXXX" or "Code revealed: XXXXXX"
                            const codePatterns = [
                                /[Cc]ode[:\\s]+([A-Z0-9]{6})/,
                                /[Cc]ode\\s+revealed[:\\s]+([A-Z0-9]{6})/,
                                /[Cc]ode\\s+is[:\\s]+([A-Z0-9]{6})/,
                                /[Cc]ode[:\\s]+[a-z]+[:\\s]+([A-Z0-9]{6})/,
                            ];
                            for (const p of codePatterns) {
                                const m = text.match(p);
                                if (m && !blacklist.has(m[1])) return m[1];
                            }

                            // Look for any 6-char code in green/success-styled elements
                            const succEls = document.querySelectorAll(
                                '[class*="green"], [class*="success"], [class*="emerald"], ' +
                                '[class*="code"], [class*="font-mono"], [class*="font-bold"], ' +
                                '[class*="text-2xl"], [class*="text-3xl"]'
                            );
                            for (const el of succEls) {
                                const t = el.textContent.trim();
                                if (/^[A-Z0-9]{6}$/.test(t) && !blacklist.has(t)) return t;
                            }

                            // Look for any 6-char code displayed after "solved"
                            const solvedIdx = text.indexOf('solved');
                            if (solvedIdx >= 0) {
                                const after = text.substring(solvedIdx, solvedIdx + 300);
                                // Find all codes after "solved" and prefer mixed alpha+num
                                const allCodes = after.match(/([A-Z0-9]{6})/g) || [];
                                const filtered = allCodes.filter(c => !blacklist.has(c) && !/^\\d{1,3}[A-Z]{3,5}$/.test(c));
                                const mixed = filtered.filter(c => /[A-Z]/.test(c) && /[0-9]/.test(c));
                                if (mixed.length > 0) return mixed[0];
                                if (filtered.length > 0) return filtered[0];
                            }

                            // Look for "Code" followed by anything then a 6-char code (broader)
                            const broadMatch = text.match(/[Cc]ode[\s\S]{0,30}?([A-Z0-9]{6})/);
                            if (broadMatch && !blacklist.has(broadMatch[1])) return broadMatch[1];

                            return null;
                        }
                    """)
                    if puzzle_code and puzzle_code not in (dom_codes or []):
                        dom_codes = [puzzle_code] + (dom_codes or [])
                        print(f"  puzzle_code extracted: {puzzle_code}", flush=True)
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

            # Handle Encoded/Base64 Challenge (decode Base64, hex, ROT13 etc. to find code)
            if ('base64' in html_lower or 'decode' in html_lower or 'encoded' in html_lower or
                'cipher' in html_lower or 'rot13' in html_lower or 'hex' in html_lower):
                enc_result = await self._try_encoded_challenge()
                if enc_result:
                    print(f"  encoded_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-encoded codes: {dom_codes}", flush=True)
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

            # Handle Shadow DOM Challenge (navigate through shadow DOM layers)
            # Be specific: check for "Shadow DOM" or "Shadow Level" in text (not CSS shadow-* classes)
            if ('shadow dom' in html_lower or 'shadow level' in html_lower or
                'shadow layer' in html_lower):
                shadow_result = await self._try_shadow_dom_challenge()
                if shadow_result:
                    print(f"  shadow_dom: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-shadow codes: {dom_codes}", flush=True)
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

            # Handle Recursive Iframe Challenge (navigate through nested iframes)
            if 'iframe' in html_lower and ('nested' in html_lower or 'recursive' in html_lower or 'level' in html_lower):
                iframe_result = await self._try_iframe_challenge()
                if iframe_result:
                    print(f"  iframe_challenge: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-iframe codes: {dom_codes}", flush=True)
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

            # Handle Service Worker Challenge (register SW, wait for cache, retrieve code)
            if 'service worker' in html_lower or 'serviceworker' in html_lower:
                sw_result = await self._try_service_worker_challenge()
                if sw_result:
                    print(f"  service_worker: completed", flush=True)
                    await asyncio.sleep(0.5)
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-sw codes: {dom_codes}", flush=True)
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
                filled = await self._try_fill_code(dom_codes)
                print(f"  filled: {filled}", flush=True)
            else:
                print(f"  dom_codes: none found", flush=True)
                # If page is still blank, wait a bit more
                if len(html) < 1000:
                    print(f"  (page still blank, waiting...)", flush=True)
                    await asyncio.sleep(1)
                    continue

            # Check progress after code fill
            url = await self.browser.get_url()
            if self._check_progress(url, challenge_num):
                self.metrics.end_challenge(
                    challenge_num, success=True,
                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                )
                print(f"  >>> PASSED <<<", flush=True)
                return True

            # === FALLBACK STRATEGIES when stuck (attempt 3+) ===
            if attempt >= 3 and not dom_codes:
                fallback_result = await self._try_fallback_strategies(html, attempt)
                if fallback_result:
                    await asyncio.sleep(0.3)
                    url = await self.browser.get_url()
                    if self._check_progress(url, challenge_num):
                        self.metrics.end_challenge(
                            challenge_num, success=True,
                            tokens_in=total_tokens_in, tokens_out=total_tokens_out
                        )
                        print(f"  >>> PASSED <<<", flush=True)
                        return True
                    # Re-extract codes after fallback
                    html = await self.browser.get_html()
                    dom_codes = extract_hidden_codes(html)
                    if dom_codes:
                        print(f"  post-fallback codes: {dom_codes}", flush=True)
                        filled = await self._try_fill_code(dom_codes)
                        if filled:
                            await asyncio.sleep(0.3)
                            url = await self.browser.get_url()
                            if self._check_progress(url, challenge_num):
                                self.metrics.end_challenge(
                                    challenge_num, success=True,
                                    tokens_in=total_tokens_in, tokens_out=total_tokens_out
                                )
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

            # Use vision every 5th attempt as fallback, with escalating thinking
            vision_call_num = attempt // 5  # 0th, 1st, 2nd... vision call
            if attempt > 0 and attempt % 5 == 0:
                try:
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
                except Exception as ve:
                    print(f"  vision ERROR (non-fatal): {type(ve).__name__}: {str(ve)[:100]}", flush=True)

            await asyncio.sleep(0.1)

        return False

    async def _try_fallback_strategies(self, html: str, attempt: int) -> bool:
        """Try various fallback strategies when the main handlers fail."""
        html_lower = html.lower()
        print(f"  fallback (attempt {attempt})...", flush=True)

        # Strategy 1: Try scrolling the page itself to reveal hidden content
        if attempt == 3:
            await self.browser.scroll_to_bottom()
            await asyncio.sleep(0.5)
            print(f"    -> scrolled page to bottom", flush=True)
            return True

        # Strategy 2: Click ALL visible buttons one by one (not just brute force)
        if attempt == 4:
            clicked_any = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button, [role="button"], a[href]')];
                    let count = 0;
                    for (const btn of btns) {
                        const text = (btn.textContent || '').trim();
                        // Skip known-bad buttons
                        if (['Dismiss', 'Click Me!', 'Button!'].includes(text)) continue;
                        if (btn.offsetParent && !btn.disabled) {
                            btn.click();
                            count++;
                        }
                    }
                    return count;
                }
            """)
            print(f"    -> clicked {clicked_any} buttons individually", flush=True)
            return clicked_any > 0

        # Strategy 3: Try clicking specific Tailwind-styled elements
        if attempt == 5:
            clicked = await self.browser.page.evaluate("""
                () => {
                    let count = 0;
                    // Click elements with bg-blue, bg-green, bg-primary (action buttons)
                    document.querySelectorAll('[class*="bg-blue"], [class*="bg-green"], [class*="bg-primary"], [class*="bg-indigo"]').forEach(el => {
                        if (el.offsetParent) { el.click(); count++; }
                    });
                    // Click "Next", "Continue", "Proceed", "Go" buttons
                    document.querySelectorAll('button, a').forEach(el => {
                        const t = (el.textContent || '').trim().toLowerCase();
                        if (['next', 'continue', 'proceed', 'go', 'start', 'begin', 'enter', 'open'].includes(t) && el.offsetParent) {
                            el.click(); count++;
                        }
                    });
                    return count;
                }
            """)
            print(f"    -> clicked {clicked} styled/nav elements", flush=True)
            return clicked > 0

        # Strategy 4: Try to find and interact with any input elements
        if attempt == 6:
            typed = await self.browser.page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea');
                    for (const input of inputs) {
                        if (input.offsetParent) {
                            input.focus();
                            input.value = 'test';
                            input.dispatchEvent(new Event('input', {bubbles: true}));
                            input.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if typed:
                print(f"    -> typed in input field", flush=True)
            return typed

        # Strategy 5: Try keyboard shortcuts
        if attempt == 7:
            for key in ['Enter', 'Space', 'Tab', 'Escape']:
                await self.browser.page.keyboard.press(key)
                await asyncio.sleep(0.2)
            print(f"    -> tried keyboard shortcuts", flush=True)
            return True

        # Strategy 6: Try hovering over all interactive elements systematically
        if attempt == 8:
            positions = await self.browser.page.evaluate("""
                () => {
                    const elements = document.querySelectorAll('div, button, span, a, section');
                    const positions = [];
                    for (const el of elements) {
                        if (el.offsetParent && el.offsetWidth > 30 && el.offsetHeight > 20) {
                            const rect = el.getBoundingClientRect();
                            const cx = rect.x + rect.width/2;
                            const cy = rect.y + rect.height/2;
                            // Only positions within viewport
                            if (cx > 0 && cy > 0 && cx < 1280 && cy < 800) {
                                positions.push({x: cx, y: cy});
                            }
                        }
                    }
                    return positions.slice(0, 20);
                }
            """)
            for pos in positions:
                await self.browser.page.mouse.move(pos['x'], pos['y'])
                await asyncio.sleep(0.3)
            print(f"    -> hovered over {len(positions)} elements", flush=True)
            return len(positions) > 0

        # Strategy 7: Try the timing challenge pattern (click rapidly)
        if attempt >= 9:
            # Click any "Capture" buttons rapidly
            await self.browser.page.evaluate("""
                () => {
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('capture') || t.includes('reveal') ||
                             t.includes('show') || t.includes('start') ||
                             t.includes('play') || t.includes('complete')) &&
                            btn.offsetParent && !btn.disabled) {
                            btn.click();
                        }
                    });
                }
            """)
            await asyncio.sleep(0.5)
            print(f"    -> clicked action buttons", flush=True)
            return True

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

            # Step 2: Find the hover target element and scroll it into view
            target_info = await self.browser.page.evaluate("""
                () => {
                    // Strategy 1: cursor-pointer element inside hover section
                    const cursorEls = [...document.querySelectorAll('[class*="cursor-pointer"]')].filter(el => {
                        return el.offsetParent && el.offsetWidth > 50 && el.offsetHeight > 30;
                    });
                    // Strategy 2: bordered box element
                    const borderEls = [...document.querySelectorAll('div')].filter(el => {
                        const cls = el.className || '';
                        return cls.includes('border-2') && cls.includes('rounded') &&
                               el.offsetParent && el.offsetWidth > 50;
                    });
                    // Strategy 3: min-h element with border
                    const minHEls = [...document.querySelectorAll('div')].filter(el => {
                        const cls = el.className || '';
                        return cls.includes('min-h-') && cls.includes('border') &&
                               el.offsetParent && el.offsetWidth > 50;
                    });

                    const candidates = [...cursorEls, ...borderEls, ...minHEls];
                    if (candidates.length > 0) {
                        const el = candidates[0];
                        el.scrollIntoView({behavior: 'instant', block: 'center'});
                        const rect = el.getBoundingClientRect();
                        return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, found: true};
                    }
                    return {found: false};
                }
            """)

            if not target_info.get('found'):
                return False

            await asyncio.sleep(0.3)

            # Step 3: Hover using mouse.move for precise control
            x, y = target_info['x'], target_info['y']
            await self.browser.page.mouse.move(x, y)
            print(f"    -> hovering at ({x:.0f}, {y:.0f})", flush=True)
            # Hold hover for 1.5 seconds (challenge typically requires 1s)
            await asyncio.sleep(1.5)

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
                else:
                    print(f"    -> no Capture button found", flush=True)

                # Wait for next rotation cycle (code changes every 3 seconds)
                await asyncio.sleep(1.0)

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
                    for (const el of els) {
                        const t = (el.textContent || '').trim().toLowerCase();
                        if (t.includes('hover over') && el.offsetParent) {
                            el.scrollIntoView({behavior: 'instant', block: 'center'});
                            const rect = el.getBoundingClientRect();
                            return {found: true, x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                        }
                    }
                    return {found: false};
                }
            """)
            if hover_done.get('found'):
                await self.browser.page.mouse.move(hover_done['x'], hover_done['y'])
                await asyncio.sleep(1.0)
                print(f"    -> hovered over area", flush=True)

            # Action 3: Type text in the input field
            type_done = await self.browser.page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
                    for (const inp of inputs) {
                        const ph = (inp.placeholder || '').toLowerCase();
                        if ((ph.includes('type') || ph.includes('click')) && inp.offsetParent) {
                            inp.scrollIntoView({behavior: 'instant', block: 'center'});
                            const rect = inp.getBoundingClientRect();
                            return {found: true, x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                        }
                    }
                    return {found: false};
                }
            """)
            if type_done.get('found'):
                await self.browser.page.mouse.click(type_done['x'], type_done['y'])
                await asyncio.sleep(0.2)
                await self.browser.page.keyboard.type("hello", delay=50)
                print(f"    -> typed text in input", flush=True)
                await asyncio.sleep(0.3)

            # Action 4: Scroll inside the scroll box
            # Find scrollable element, scroll it with JS + dispatch events
            scroll_done = await self.browser.page.evaluate("""
                () => {
                    const findScrollable = () => {
                        const els = [...document.querySelectorAll('div, textarea')];
                        // Strategy 1: element containing "scroll inside" text
                        for (const el of els) {
                            const t = (el.textContent || '').trim().toLowerCase();
                            const style = getComputedStyle(el);
                            const isScrollable = style.overflow === 'auto' || style.overflow === 'scroll' ||
                                style.overflowY === 'auto' || style.overflowY === 'scroll';
                            if (t.includes('scroll inside') && isScrollable && el.offsetParent) return el;
                        }
                        // Strategy 2: class-based detection (overflow-y-auto, overflow-auto)
                        for (const el of document.querySelectorAll('[class*="overflow-y"], [class*="overflow-auto"]')) {
                            if (el.scrollHeight > el.clientHeight + 10 && el.offsetParent &&
                                el.clientHeight < 500 && el.clientHeight > 30) return el;
                        }
                        // Strategy 3: any scrollable div
                        for (const el of els) {
                            const style = getComputedStyle(el);
                            const isScrollable = style.overflow === 'auto' || style.overflow === 'scroll' ||
                                style.overflowY === 'auto' || style.overflowY === 'scroll';
                            if (isScrollable && el.scrollHeight > el.clientHeight + 10 &&
                                el.offsetParent && el.clientHeight < 500 && el.clientHeight > 30) return el;
                        }
                        return null;
                    };
                    const el = findScrollable();
                    if (!el) return {found: false};
                    el.scrollIntoView({behavior: 'instant', block: 'center'});
                    // Scroll incrementally and dispatch events for React detection
                    const step = Math.max(50, Math.floor(el.scrollHeight / 5));
                    for (let pos = step; pos <= el.scrollHeight; pos += step) {
                        el.scrollTop = pos;
                        el.dispatchEvent(new Event('scroll', {bubbles: true}));
                    }
                    el.scrollTop = el.scrollHeight;
                    el.dispatchEvent(new Event('scroll', {bubbles: true}));
                    const rect = el.getBoundingClientRect();
                    return {found: true, x: rect.x + rect.width/2, y: rect.y + rect.height/2,
                            scrolled: el.scrollTop, max: el.scrollHeight};
                }
            """)
            if scroll_done.get('found'):
                # Also use mouse wheel for more realistic scroll (triggers React listeners)
                await self.browser.page.mouse.move(scroll_done['x'], scroll_done['y'])
                for _ in range(5):
                    await self.browser.page.mouse.wheel(0, 300)
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.3)
                print(f"    -> scrolled inside box (scrollTop={scroll_done.get('scrolled')}/{scroll_done.get('max')})", flush=True)

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
            # Step -1: Check if puzzle is already solved (from previous visit)
            already_solved = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    if (!text.toLowerCase().includes('solved')) return null;
                    const blacklist = new Set(['AWRONG', 'PUZZLE', 'SOLVED', 'ONMOVE', 'REVEAL']);
                    // Look for code after "solved" text
                    const patterns = [
                        /[Cc]ode[\\s\\S]{0,30}?([A-Z0-9]{6})/,
                        /solved[\\s\\S]{0,50}?([A-Z0-9]{6})/,
                        /revealed[\\s\\S]{0,30}?([A-Z0-9]{6})/,
                    ];
                    for (const p of patterns) {
                        const m = text.match(p);
                        if (m && !blacklist.has(m[1]) && !/^\\d{1,3}[A-Z]{3,5}$/.test(m[1])) return m[1];
                    }
                    // Check success-styled elements
                    const els = document.querySelectorAll(
                        '[class*="green"], [class*="success"], [class*="font-mono"], [class*="font-bold"]'
                    );
                    for (const el of els) {
                        const t = el.textContent.trim();
                        if (/^[A-Z0-9]{6}$/.test(t) && !blacklist.has(t)) return t;
                    }
                    return null;
                }
            """)
            # Don't short-circuit on "already solved" - the code may be stale from a previous step.
            # Instead, always try to solve the puzzle fresh first.

            # Step 0: Force puzzle inputs and buttons visible (without removing overlays which can break layout)
            await self.browser.page.evaluate("""
                () => {
                    // Force Solve/Check buttons to be visible and clickable
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t === 'solve' || t.includes('solve') ||
                            t === 'check' || t === 'verify') {
                            let el = btn;
                            while (el && el !== document.body) {
                                const cs = getComputedStyle(el);
                                if (cs.display === 'none') el.style.display = 'block';
                                if (cs.visibility === 'hidden') el.style.visibility = 'visible';
                                if (cs.opacity === '0') el.style.opacity = '1';
                                el.style.pointerEvents = 'auto';
                                el = el.parentElement;
                            }
                            btn.style.zIndex = '9999';
                        }
                    });
                }
            """)
            await asyncio.sleep(0.3)

            # Step 0.5: If puzzle shows as "solved" (stale from previous step),
            # force React Router to re-navigate and reset component state
            has_number_input = await self.browser.page.evaluate("""
                () => !!document.querySelector('input[type="number"]')
            """)
            if not has_number_input:
                print(f"    -> no number input found, trying React Router reset", flush=True)
                current_path = await self.browser.page.evaluate("() => window.location.pathname + window.location.search")
                # Navigate away to root
                await self.browser.page.evaluate("""
                    () => {
                        window.history.pushState(null, '', '/');
                        window.dispatchEvent(new PopStateEvent('popstate'));
                    }
                """)
                await asyncio.sleep(0.5)
                # Navigate back to the step
                await self.browser.page.evaluate(f"""
                    () => {{
                        window.history.pushState(null, '', '{current_path}');
                        window.dispatchEvent(new PopStateEvent('popstate'));
                    }}
                """)
                await asyncio.sleep(1.5)
                # Check if input appeared
                has_input_now = await self.browser.page.evaluate("""
                    () => !!document.querySelector('input[type="number"]')
                """)
                print(f"    -> after React reset, has number input: {has_input_now}", flush=True)

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

            # Snapshot text BEFORE solving (to diff later)
            text_before = await self.browser.page.evaluate("() => document.body.innerText || ''")

            # Step 2: Fill the number input
            answer_str = str(expr['answer'])

            # Discover all input fields on the page for debugging
            input_info = await self.browser.page.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll('input')];
                    return inputs.map(i => {
                        const r = i.getBoundingClientRect();
                        return {
                            type: i.type, placeholder: i.placeholder, id: i.id,
                            name: i.name, value: i.value,
                            visible: r.width > 0 && r.height > 0,
                            rect: Math.round(r.width) + 'x' + Math.round(r.height)
                        };
                    });
                }
            """)
            print(f"    -> all inputs on page: {input_info}", flush=True)

            # Find the puzzle answer input and set value
            # IMPORTANT: Inputs may be 0x0 due to CSS/overlays but we can still set values via JS
            input_found = await self.browser.page.evaluate(f"""
                () => {{
                    // First try: force all puzzle-related elements to be visible
                    document.querySelectorAll('input').forEach(inp => {{
                        if (inp.type === 'number' || (inp.placeholder || '').toLowerCase().includes('answer')) {{
                            // Force the input and ALL its parents to be visible
                            let el = inp;
                            while (el && el !== document.body) {{
                                el.style.display = '';
                                el.style.visibility = 'visible';
                                el.style.opacity = '1';
                                el.style.position = el.style.position || '';
                                el.style.overflow = 'visible';
                                const cs = getComputedStyle(el);
                                if (cs.display === 'none') el.style.display = 'block';
                                if (cs.height === '0px') el.style.height = 'auto';
                                if (cs.width === '0px') el.style.width = 'auto';
                                el = el.parentElement;
                            }}
                        }}
                    }});

                    // Now find the puzzle input (type=number or placeholder has "answer")
                    const input = document.querySelector('input[type="number"]') ||
                                  document.querySelector('input[placeholder*="answer" i]') ||
                                  document.querySelector('input[inputmode="numeric"]');
                    if (!input) return {{found: false, reason: 'no matching input'}};

                    // Set value using native React setter
                    input.focus();
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(input, '{answer_str}');
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));

                    const rect = input.getBoundingClientRect();
                    return {{
                        found: true,
                        type: input.type,
                        placeholder: input.placeholder,
                        valueAfterSet: input.value,
                        rect: Math.round(rect.width) + 'x' + Math.round(rect.height)
                    }};
                }}
            """)
            print(f"    -> JS setter result: {input_found}", flush=True)
            await asyncio.sleep(0.2)

            # Also try Playwright fill() - it needs visible element, so try after forcing visibility
            pw_filled = False
            try:
                for sel in ['input[type="number"]', 'input[inputmode="numeric"]',
                           'input[placeholder*="answer" i]']:
                    loc = self.browser.page.locator(sel).first
                    if await loc.count() > 0:
                        try:
                            await loc.fill(answer_str, timeout=2000)
                            print(f"    -> Playwright fill('{answer_str}') on {sel}", flush=True)
                            pw_filled = True
                            break
                        except Exception:
                            continue
            except Exception as e:
                print(f"    -> Playwright fill failed: {e}", flush=True)

            # Keyboard approach on the now-visible input
            if not pw_filled:
                try:
                    loc = self.browser.page.locator('input[type="number"]').first
                    if await loc.count() > 0:
                        await loc.click(click_count=3, timeout=2000)
                        await self.browser.page.keyboard.press('Backspace')
                        await self.browser.page.keyboard.type(answer_str, delay=30)
                        print(f"    -> keyboard typed '{answer_str}'", flush=True)
                except Exception as e:
                    print(f"    -> keyboard type failed: {e}", flush=True)

            await asyncio.sleep(0.3)

            # Verify the input value was actually set
            input_val = await self.browser.page.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll('input')];
                    return inputs.map(i => ({type: i.type, value: i.value, visible: !!i.offsetParent}));
                }
            """)
            print(f"    -> input values before solve: {input_val}", flush=True)

            # Step 3: Click Solve button - multiple strategies
            # Strategy A: JS click on any Solve/Check/Verify button (even if hidden)
            solved = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t === 'solve' || t.includes('solve') ||
                             t === 'check' || t === 'verify') && !btn.disabled) {
                            btn.scrollIntoView({behavior: 'instant', block: 'center'});
                            btn.click();
                            return 'js_click: ' + t;
                        }
                    }
                    // Click pink/rose/red colored button that's not submit
                    for (const btn of btns) {
                        const cls = btn.className || '';
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((cls.includes('pink') || cls.includes('rose') || cls.includes('red') ||
                             cls.includes('purple') || cls.includes('indigo')) &&
                            !t.includes('submit') && !btn.disabled && t.length < 20) {
                            btn.scrollIntoView({behavior: 'instant', block: 'center'});
                            btn.click();
                            return 'color_click: ' + t;
                        }
                    }
                    return false;
                }
            """)
            print(f"    -> solve result: {solved}", flush=True)

            # Strategy B: Press Enter (submits the form)
            await self.browser.page.keyboard.press('Enter')
            await asyncio.sleep(0.3)

            # Strategy C: Playwright text-based click
            if not solved:
                for text in ['Solve', 'Check', 'Verify', 'Submit']:
                    try:
                        await self.browser.page.click(f"button:has-text('{text}')", timeout=500)
                        print(f"    -> Playwright clicked '{text}'", flush=True)
                        solved = True
                        break
                    except Exception:
                        continue

            await asyncio.sleep(1.5)

            # Check immediate post-solve state
            post_solve_check = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    const html = document.body.innerHTML || '';
                    const hasSuccess = html.includes('success') || html.includes('correct') ||
                                       html.includes('Correct') || html.includes('solved');
                    const hasError = html.includes('wrong') || html.includes('incorrect') ||
                                     html.includes('Wrong') || html.includes('Incorrect');
                    // Find any 6-char codes in the HTML (not just visible text)
                    const htmlCodes = (html.match(/[A-Z0-9]{6}/g) || []).filter(c =>
                        !['PUZZLE','SOLVED','AWRONG','CWRONG','DWRONG','HEREGO','ONETHE'].includes(c) &&
                        !c.endsWith('WRONG') && /[A-Z]/.test(c) && /[0-9]/.test(c)
                    );
                    return {
                        textLen: text.length,
                        htmlLen: html.length,
                        hasSuccess, hasError,
                        textSnippet: text.substring(0, 200),
                        htmlCodesFound: htmlCodes.slice(0, 10),
                        url: window.location.href
                    };
                }
            """)
            print(f"    -> post-solve state: {post_solve_check}", flush=True)

            # Step 4: Scroll to reveal code that might be below fold
            await self.browser.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)
            await self.browser.page.evaluate("() => window.scrollTo(0, 0)")
            await asyncio.sleep(0.3)

            # Step 5: Click any post-solve buttons ("click here", "reveal", etc.)
            await self.browser.page.evaluate("""
                () => {
                    document.querySelectorAll('button, a, div, span').forEach(el => {
                        const t = (el.textContent || '').trim().toLowerCase();
                        if ((t.includes('click here') || t === 'reveal' || t === 'show code' ||
                             t === 'next' || t === 'continue') &&
                            el.offsetParent && t.length < 30) {
                            el.click();
                        }
                    });
                }
            """)
            await asyncio.sleep(0.5)

            # Step 6: Extract the revealed code - try targeted patterns FIRST, then diff
            text_after = await self.browser.page.evaluate("() => document.body.innerText || ''")
            url_before_extract = await self.browser.get_url()

            # Priority 1: Look for "Code revealed:" or similar pattern in the text
            revealed_code = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    const blacklist = new Set(['AWRONG', 'THISIS', 'WRONG0', 'PUZZLE', 'SOLVED', 'CWRONG', 'HEREGO', '1WRONG']);
                    const patterns = [
                        /[Cc]ode[:\\s]+([A-Z0-9]{6})/,
                        /[Cc]ode\\s+revealed[:\\s]+([A-Z0-9]{6})/,
                        /[Rr]evealed[:\\s]+([A-Z0-9]{6})/,
                        /[Cc]ode[\\s\\S]{0,30}?([A-Z0-9]{6})/,
                        /solved[\\s\\S]{0,50}?([A-Z0-9]{6})/i,
                    ];
                    for (const p of patterns) {
                        const m = text.match(p);
                        if (m && !blacklist.has(m[1]) && !m[1].endsWith('WRONG')) return m[1];
                    }
                    // Check styled elements (green, success, font-mono)
                    const els = document.querySelectorAll(
                        '[class*="green"], [class*="success"], [class*="font-mono"], [class*="font-bold"]'
                    );
                    for (const el of els) {
                        const t = el.textContent.trim();
                        if (/^[A-Z0-9]{6}$/.test(t) && !blacklist.has(t) && !t.endsWith('WRONG')) return t;
                    }
                    return null;
                }
            """)
            if revealed_code:
                print(f"    -> puzzle revealed code (pattern): {revealed_code}", flush=True)
                await self._try_fill_code([revealed_code])
                await asyncio.sleep(0.3)
                url_after_extract = await self.browser.get_url()
                if url_after_extract != url_before_extract:
                    return True

            # Priority 2: Diff approach - but limit to max 5 codes to avoid wasting time
            before_words = set(re.findall(r'[A-Z0-9]{6}', text_before.upper()))
            after_words = set(re.findall(r'[A-Z0-9]{6}', text_after.upper()))
            new_codes = after_words - before_words
            blacklist = {'AWRONG', 'THISIS', 'WRONG0', 'PUZZLE', 'SOLVED', 'CWRONG', 'HEREGO',
                         '1WRONG', '2WRONG', '3WRONG', 'DECOYC', 'FAKECO', 'ONETHE',
                         'WORKER', 'BROKEN', 'BUTTON', 'CHOOSE', 'DETAIL', 'PLEASE',
                         'IMPORT', 'INFORM', 'SELECT', 'OPTION', 'ANSWER', 'CORREC',
                         'EXTEND', 'ATTEMP', 'CHOICE', 'ACCEPT'}
            new_codes = {c for c in new_codes if c not in blacklist and not c.endswith('WRONG')
                        and not c.isdigit() and not c.endswith('MS')}
            if new_codes:
                # Prefer mixed alphanumeric, then skip already-tried revealed_code
                mixed = [c for c in new_codes if any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c)]
                alpha_only = [c for c in new_codes if c not in mixed and c != revealed_code]
                new_codes_list = mixed + alpha_only[:3]  # limit to avoid wasting time
                if new_codes_list:
                    print(f"    -> diff codes (max 5): {new_codes_list[:5]}", flush=True)
                    await self._try_fill_code(new_codes_list[:5])

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

    async def _try_shadow_dom_challenge(self) -> bool:
        """Handle Shadow DOM Challenge - click through shadow DOM layers to reveal code."""
        try:
            max_layers = 10

            # First try: click "Reveal Code" button specifically
            for _ in range(3):
                clicked_reveal = await self.browser.page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')];
                        for (const btn of btns) {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if (t.includes('reveal') && !btn.disabled) {
                                btn.click();
                                return t;
                            }
                        }
                        return null;
                    }
                """)
                if clicked_reveal:
                    print(f"    -> clicked reveal: {clicked_reveal}", flush=True)
                    await asyncio.sleep(1.0)
                else:
                    break

            for attempt in range(max_layers):
                # Click buttons that reveal/navigate layers - look in regular DOM and shadow roots
                result = await self.browser.page.evaluate("""
                    () => {
                        // Recursively search through shadow roots
                        function findAndClickButton(root) {
                            const btns = [...root.querySelectorAll('button, a, div[role="button"]')];
                            // Priority 1: Buttons specifically about revealing/unlocking/layers
                            for (const btn of btns) {
                                const t = (btn.textContent || '').trim().toLowerCase();
                                if ((t.includes('reveal') || t.includes('unlock') ||
                                     t.includes('open layer') || t.includes('enter layer') ||
                                     t.includes('go deeper') || t.includes('enter level') ||
                                     (t.includes('layer') && !t.includes('next')) ||
                                     (t.includes('level') && (t.includes('enter') || t.includes('reveal') || t.includes('open')))) &&
                                    !btn.disabled && t.length < 40) {
                                    btn.click();
                                    return {clicked: t, level: 'regular'};
                                }
                            }
                            // Search shadow roots
                            const allEls = root.querySelectorAll('*');
                            for (const el of allEls) {
                                if (el.shadowRoot) {
                                    const result = findAndClickButton(el.shadowRoot);
                                    if (result) return result;
                                }
                            }
                            return null;
                        }
                        return findAndClickButton(document);
                    }
                """)
                if result:
                    print(f"    -> shadow click: {result}", flush=True)
                    await asyncio.sleep(0.5)
                else:
                    print(f"    -> no more shadow buttons to click", flush=True)
                    break

                # Check for revealed code
                code = await self.browser.page.evaluate("""
                    () => {
                        function findCode(root) {
                            const text = root.textContent || '';
                            const m = text.match(/[Cc]ode[:\\s]+([A-Z0-9]{6})/);
                            if (m) return m[1];

                            // Check styled elements
                            const els = root.querySelectorAll('[class*="green"], [class*="success"], [class*="font-mono"]');
                            for (const el of els) {
                                const t = el.textContent.trim();
                                if (/^[A-Z0-9]{6}$/.test(t)) return t;
                            }

                            // Recurse into shadow roots
                            const allEls = root.querySelectorAll('*');
                            for (const el of allEls) {
                                if (el.shadowRoot) {
                                    const result = findCode(el.shadowRoot);
                                    if (result) return result;
                                }
                            }
                            return null;
                        }
                        return findCode(document);
                    }
                """)
                if code:
                    print(f"    -> shadow code found: {code}", flush=True)
                    await self._try_fill_code([code])
                    return True

            # Final check: look for codes in any remaining shadow roots
            code = await self.browser.page.evaluate("""
                () => {
                    function findAllCodes(root) {
                        const text = root.textContent || '';
                        const codes = text.match(/[A-Z0-9]{6}/g) || [];
                        const blacklist = ['SHADOW', 'LAYERS', 'LEVELS', 'BUTTON', 'REVEAL', 'NESTED'];
                        const filtered = codes.filter(c => !blacklist.includes(c) &&
                            /[A-Z]/.test(c) && /[0-9]/.test(c));
                        if (filtered.length > 0) return filtered[0];

                        const allEls = root.querySelectorAll('*');
                        for (const el of allEls) {
                            if (el.shadowRoot) {
                                const result = findAllCodes(el.shadowRoot);
                                if (result) return result;
                            }
                        }
                        return null;
                    }
                    return findAllCodes(document);
                }
            """)
            if code:
                print(f"    -> shadow final code: {code}", flush=True)
                await self._try_fill_code([code])
                return True

            return True
        except Exception as e:
            print(f"    -> shadow DOM error: {e}", flush=True)
            return False

    async def _try_iframe_challenge(self) -> bool:
        """Handle Recursive Iframe Challenge - navigate through nested iframe levels."""
        try:
            max_depth = 10  # Safety limit

            for depth in range(max_depth):
                # Check current depth and look for code
                state = await self.browser.page.evaluate("""
                    () => {
                        // Recursively traverse iframes to find the deepest content
                        function findDeepContent(doc, level) {
                            const text = doc.body ? doc.body.innerText || '' : '';
                            // Look for code pattern at this level
                            const codeMatch = text.match(/[Cc]ode[:\\s]+([A-Z0-9]{6})/);
                            if (codeMatch) return {code: codeMatch[1], level};

                            // Look for 6-char codes in styled elements
                            const els = doc.querySelectorAll('[class*="green"], [class*="success"], [class*="font-mono"], [class*="font-bold"]');
                            for (const el of els) {
                                const t = el.textContent.trim();
                                if (/^[A-Z0-9]{6}$/.test(t)) return {code: t, level};
                            }

                            // Recurse into iframes
                            const iframes = doc.querySelectorAll('iframe');
                            for (const iframe of iframes) {
                                try {
                                    const result = findDeepContent(iframe.contentDocument, level + 1);
                                    if (result) return result;
                                } catch(e) {} // cross-origin
                            }
                            return null;
                        }
                        const result = findDeepContent(document, 0);
                        return result;
                    }
                """)
                if state and state.get('code'):
                    code = state['code']
                    print(f"    -> iframe code found at level {state.get('level')}: {code}", flush=True)
                    await self._try_fill_code([code])
                    return True

                # Try to enter the next level by clicking "Enter Level X" button
                clicked = await self.browser.page.evaluate("""
                    () => {
                        // Check main document and all iframes recursively
                        function findAndClickEnter(doc) {
                            const btns = [...doc.querySelectorAll('button, a')];
                            for (const btn of btns) {
                                const t = (btn.textContent || '').trim().toLowerCase();
                                if ((t.includes('enter level') || t.includes('go deeper') ||
                                     t.includes('next level') || t.includes('descend') ||
                                     t.includes('go to level')) && !btn.disabled) {
                                    // Force visibility and click
                                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                                    btn.style.visibility = 'visible';
                                    btn.style.display = '';
                                    btn.click();
                                    return t;
                                }
                            }
                            // Recurse into iframes
                            const iframes = doc.querySelectorAll('iframe');
                            for (const iframe of iframes) {
                                try {
                                    const result = findAndClickEnter(iframe.contentDocument);
                                    if (result) return result;
                                } catch(e) {}
                            }
                            return null;
                        }
                        return findAndClickEnter(document);
                    }
                """)
                if clicked:
                    print(f"    -> clicked: {clicked}", flush=True)
                    await asyncio.sleep(0.5)
                else:
                    # Try Playwright text-based click
                    pw_clicked = False
                    for text in ['Enter Level', 'Go Deeper', 'Next Level', 'Descend']:
                        try:
                            await self.browser.page.click(f"button:has-text('{text}')", timeout=1000)
                            print(f"    -> Playwright clicked '{text}'", flush=True)
                            pw_clicked = True
                            await asyncio.sleep(0.5)
                            break
                        except Exception:
                            continue
                    if pw_clicked:
                        continue

                    # No button found - try using Playwright frame handling
                    frames = self.browser.page.frames
                    print(f"    -> no enter button, {len(frames)} frames total", flush=True)

                    # Look for code in all frames
                    for frame in frames:
                        try:
                            text = await frame.evaluate("() => document.body?.innerText || ''")
                            codes = re.findall(r'[A-Z0-9]{6}', text)
                            blacklist = {'IFRAME', 'LEVELS', 'NESTED', 'WORKER', 'BROKEN'}
                            codes = [c for c in codes if c not in blacklist and
                                    any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c)]
                            if codes:
                                print(f"    -> frame code found: {codes[0]}", flush=True)
                                await self._try_fill_code(codes[:3])
                                return True
                        except Exception:
                            continue

                    # Try to find clickable elements in child frames
                    for frame in frames[1:]:  # Skip main frame
                        try:
                            clicked_in_frame = await frame.evaluate("""
                                () => {
                                    const btns = [...document.querySelectorAll('button, a')];
                                    for (const btn of btns) {
                                        const t = (btn.textContent || '').trim().toLowerCase();
                                        if ((t.includes('enter') || t.includes('level') ||
                                             t.includes('deeper') || t.includes('next')) &&
                                            btn.offsetParent !== null && t.length < 30) {
                                            btn.click();
                                            return t;
                                        }
                                    }
                                    return null;
                                }
                            """)
                            if clicked_in_frame:
                                print(f"    -> clicked in frame: {clicked_in_frame}", flush=True)
                                await asyncio.sleep(0.5)
                                break
                        except Exception:
                            continue
                    else:
                        # No more buttons to click, break
                        print(f"    -> no more levels to navigate", flush=True)
                        break

            # Final extraction: try all frames for codes
            for frame in self.browser.page.frames:
                try:
                    text = await frame.evaluate("() => document.body?.innerText || ''")
                    # Look for "Code:" pattern
                    m = re.search(r'[Cc]ode[:\s]+([A-Z0-9]{6})', text)
                    if m:
                        print(f"    -> final frame code: {m.group(1)}", flush=True)
                        await self._try_fill_code([m.group(1)])
                        return True
                    # Any 6-char code
                    codes = re.findall(r'[A-Z0-9]{6}', text)
                    blacklist = {'IFRAME', 'LEVELS', 'NESTED', 'WORKER', 'BROKEN', 'BUTTON',
                                'PLEASE', 'SCROLL', 'HIDDEN', 'REVEAL'}
                    codes = [c for c in codes if c not in blacklist]
                    if codes:
                        print(f"    -> final frame codes: {codes[:5]}", flush=True)
                        await self._try_fill_code(codes[:3])
                        return True
                except Exception:
                    continue

            return True
        except Exception as e:
            print(f"    -> iframe error: {e}", flush=True)
            return False

    async def _try_service_worker_challenge(self) -> bool:
        """Handle Service Worker Challenge - register SW, wait for cache, retrieve code."""
        try:
            # Step 1: Click "Register Service Worker" button
            registered = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('register') || t.includes('service worker') ||
                             t.includes('install')) && !btn.disabled) {
                            btn.click();
                            return t;
                        }
                    }
                    return null;
                }
            """)
            print(f"    -> clicked register button: {registered}", flush=True)
            if not registered:
                return False

            # Step 2: Wait for service worker to register and cache to populate
            # Try multiple times with increasing waits
            for wait_idx in range(6):
                await asyncio.sleep(1.0 + wait_idx * 0.5)

                # Check cache status
                status = await self.browser.page.evaluate("""
                    () => {
                        const text = document.body.innerText || '';
                        const hasCached = text.includes('Cached') || text.includes('cached') ||
                                         text.includes('Ready') || text.includes('ready') ||
                                         text.includes('Populated') || text.includes('populated');
                        const hasRegistered = text.includes('Registered') || text.includes('registered') ||
                                             text.includes('Active') || text.includes('active');
                        return {hasCached, hasRegistered, text: text.substring(0, 300)};
                    }
                """)
                print(f"    -> SW status check {wait_idx}: reg={status.get('hasRegistered')}, cache={status.get('hasCached')}", flush=True)

                if status.get('hasCached') or status.get('hasRegistered'):
                    break

            # Step 3: Click "Retrieve from Cache" button
            retrieved = await self.browser.page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('retrieve') || t.includes('cache') || t.includes('get code') ||
                             t.includes('fetch') || t.includes('check cache')) && !btn.disabled) {
                            btn.click();
                            return t;
                        }
                    }
                    return null;
                }
            """)
            print(f"    -> clicked retrieve button: {retrieved}", flush=True)
            await asyncio.sleep(1.0)

            # Step 4: Also try to read from Cache API directly
            cache_code = await self.browser.page.evaluate("""
                async () => {
                    try {
                        // Try reading from Cache API
                        const cacheNames = await caches.keys();
                        for (const name of cacheNames) {
                            const cache = await caches.open(name);
                            const keys = await cache.keys();
                            for (const key of keys) {
                                const resp = await cache.match(key);
                                if (resp) {
                                    const text = await resp.text();
                                    const match = text.match(/[A-Z0-9]{6}/);
                                    if (match) return {code: match[0], source: 'cache', cacheName: name};
                                }
                            }
                        }
                    } catch(e) {}

                    // Also check page text for revealed code
                    const text = document.body.innerText || '';
                    const m = text.match(/[Cc]ode[\s:]+([A-Z0-9]{6})/);
                    if (m) return {code: m[1], source: 'text'};

                    return null;
                }
            """)
            if cache_code:
                print(f"    -> SW cache code: {cache_code}", flush=True)
                await self._try_fill_code([cache_code['code']])

            return True
        except Exception as e:
            print(f"    -> service worker error: {e}", flush=True)
            return False

    async def _try_encoded_challenge(self) -> bool:
        """Handle Encoded/Base64/Hex/ROT13 Challenge - decode encoded string to find code."""
        try:
            import base64 as b64

            # Extract encoded strings and encoding type from the page
            enc_info = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    const textLower = text.toLowerCase();

                    // Detect encoding type
                    let encType = 'base64';
                    if (textLower.includes('rot13')) encType = 'rot13';
                    else if (textLower.includes('hexadecimal') || textLower.includes('hex string') || textLower.includes('hex code')) encType = 'hex';
                    else if (textLower.includes('caesar')) encType = 'caesar';
                    else if (textLower.includes('binary')) encType = 'binary';
                    else if (textLower.includes('base64')) encType = 'base64';
                    else if (textLower.includes('decode') || textLower.includes('encoded')) encType = 'base64';

                    // Extract encoded strings from page
                    const encodedStrings = [];

                    // Strategy 1: Look for code/mono styled elements with encoded content
                    document.querySelectorAll('code, pre, [class*="mono"], [class*="font-mono"], [class*="bg-gray"], [class*="bg-slate"]').forEach(el => {
                        const t = el.textContent.trim();
                        if (t.length >= 4 && t.length <= 100) {
                            encodedStrings.push(t);
                        }
                    });

                    // Strategy 2: Look for text after "decode" or "string" keywords
                    // Find patterns like "Decode this: XXXX" or "Base64 string: XXXX"
                    const patterns = [
                        /(?:decode|string|encoded)[:\\s]+([A-Za-z0-9+/=]{4,})/gi,
                        /(?:base64|hex|cipher)[:\\s]+([A-Za-z0-9+/=]{4,})/gi,
                        /\\b([A-Za-z0-9+/=]{8,})\\b/g,
                    ];
                    for (const p of patterns) {
                        const matches = [...text.matchAll(p)];
                        for (const m of matches) {
                            const s = m[1] || m[0];
                            if (s.length >= 4 && s.length <= 100 && !encodedStrings.includes(s)) {
                                encodedStrings.push(s);
                            }
                        }
                    }

                    // Strategy 3: Look in bold/highlighted elements
                    document.querySelectorAll('strong, b, [class*="font-bold"], [class*="text-lg"], [class*="text-xl"]').forEach(el => {
                        const t = el.textContent.trim();
                        if (t.length >= 4 && t.length <= 100 && /^[A-Za-z0-9+/=]+$/.test(t)) {
                            if (!encodedStrings.includes(t)) encodedStrings.push(t);
                        }
                    });

                    return {encType, encodedStrings: encodedStrings.slice(0, 10), pageText: text.substring(0, 500)};
                }
            """)
            enc_type = enc_info.get('encType', 'base64')
            encoded_strings = enc_info.get('encodedStrings', [])
            print(f"    -> encoding type: {enc_type}, found {len(encoded_strings)} encoded strings", flush=True)

            if not encoded_strings:
                print(f"    -> no encoded strings found, page: {enc_info.get('pageText', '')[:200]}", flush=True)
                return False

            decoded_codes = []
            for s in encoded_strings:
                try:
                    decoded = None
                    if enc_type == 'base64':
                        # Try Base64 decode
                        try:
                            decoded = b64.b64decode(s).decode('utf-8', errors='ignore').strip()
                        except Exception:
                            # Try with padding
                            padded = s + '=' * (4 - len(s) % 4) if len(s) % 4 else s
                            try:
                                decoded = b64.b64decode(padded).decode('utf-8', errors='ignore').strip()
                            except Exception:
                                pass
                    elif enc_type == 'hex':
                        # Hex decode
                        clean = s.replace(' ', '').replace('0x', '')
                        try:
                            decoded = bytes.fromhex(clean).decode('utf-8', errors='ignore').strip()
                        except Exception:
                            pass
                    elif enc_type == 'rot13':
                        # ROT13 decode
                        decoded = ''
                        for c in s:
                            if 'a' <= c <= 'z':
                                decoded += chr((ord(c) - ord('a') + 13) % 26 + ord('a'))
                            elif 'A' <= c <= 'Z':
                                decoded += chr((ord(c) - ord('A') + 13) % 26 + ord('A'))
                            else:
                                decoded += c
                    elif enc_type == 'binary':
                        # Binary decode
                        clean = s.replace(' ', '')
                        try:
                            decoded = ''.join(chr(int(clean[i:i+8], 2)) for i in range(0, len(clean), 8))
                        except Exception:
                            pass
                    elif enc_type == 'caesar':
                        # Try all Caesar shifts (1-25)
                        for shift in range(1, 26):
                            attempt = ''
                            for c in s:
                                if 'a' <= c <= 'z':
                                    attempt += chr((ord(c) - ord('a') - shift) % 26 + ord('a'))
                                elif 'A' <= c <= 'Z':
                                    attempt += chr((ord(c) - ord('A') - shift) % 26 + ord('A'))
                                else:
                                    attempt += c
                            # Check if decoded contains a valid code
                            code_match = re.search(r'[A-Z0-9]{6}', attempt.upper())
                            if code_match:
                                decoded = code_match.group(0)
                                break

                    if decoded:
                        print(f"    -> decoded '{s[:30]}...' -> '{decoded[:50]}'", flush=True)
                        # Extract 6-char alphanumeric codes from decoded text
                        code_matches = re.findall(r'[A-Z0-9]{6}', decoded.upper())
                        for code in code_matches:
                            if code not in decoded_codes:
                                decoded_codes.append(code)
                        # Also try the whole decoded string if it's exactly 6 chars
                        clean_decoded = re.sub(r'[^A-Z0-9]', '', decoded.upper())
                        if len(clean_decoded) == 6 and clean_decoded not in decoded_codes:
                            decoded_codes.append(clean_decoded)
                except Exception as e:
                    print(f"    -> decode error for '{s[:30]}': {e}", flush=True)

            if decoded_codes:
                print(f"    -> decoded codes to try: {decoded_codes}", flush=True)
                # Try to fill decoded codes
                filled = await self._try_fill_code(decoded_codes)
                if filled:
                    return True

            # Also try decoding in JS (atob for Base64) and submit directly
            js_decoded = await self.browser.page.evaluate("""
                () => {
                    const results = [];
                    // Find encoded strings in code/mono elements
                    document.querySelectorAll('code, pre, [class*="mono"], [class*="font-mono"]').forEach(el => {
                        const s = el.textContent.trim();
                        if (s.length < 4 || s.length > 100) return;
                        try {
                            const decoded = atob(s);
                            if (decoded) results.push(decoded);
                        } catch(e) {}
                    });
                    // Also try any long alphanumeric strings on the page
                    const text = document.body.innerText || '';
                    const matches = text.match(/[A-Za-z0-9+/=]{8,}/g) || [];
                    for (const m of matches) {
                        try {
                            const decoded = atob(m);
                            if (decoded) results.push(decoded);
                        } catch(e) {}
                    }
                    return results;
                }
            """)
            if js_decoded:
                for d in js_decoded:
                    codes = re.findall(r'[A-Z0-9]{6}', d.upper())
                    for code in codes:
                        if code not in decoded_codes:
                            decoded_codes.append(code)
                    clean = re.sub(r'[^A-Z0-9]', '', d.upper())
                    if len(clean) == 6 and clean not in decoded_codes:
                        decoded_codes.append(clean)

                if decoded_codes:
                    print(f"    -> JS decoded codes: {decoded_codes}", flush=True)
                    filled = await self._try_fill_code(decoded_codes)
                    if filled:
                        return True

            return len(decoded_codes) > 0
        except Exception as e:
            print(f"    -> encoded challenge error: {e}", flush=True)
            return False

    async def _try_checkbox_challenge(self) -> bool:
        """Handle Checkbox/Toggle Challenge - check all boxes or toggle switches."""
        try:
            result = await self.browser.page.evaluate("""
                () => {
                    let checked = 0;
                    // Click all unchecked checkboxes
                    document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                        if (!cb.checked && cb.offsetParent) {
                            cb.click();
                            checked++;
                        }
                    });
                    // Click toggle switches (common in React - buttons with role="switch")
                    document.querySelectorAll('[role="switch"], [class*="toggle"], [class*="switch"]').forEach(el => {
                        if (el.offsetParent && !el.classList.contains('active') &&
                            el.getAttribute('aria-checked') !== 'true') {
                            el.click();
                            checked++;
                        }
                    });
                    // Click any "Select All" or "Check All" button
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('select all') || t.includes('check all') || t.includes('enable all')) &&
                            btn.offsetParent && !btn.disabled) {
                            btn.click();
                            checked++;
                        }
                    });
                    return checked;
                }
            """)
            print(f"    -> checked/toggled {result} items", flush=True)

            if result > 0:
                await asyncio.sleep(0.5)
                # Click Verify/Submit/Complete
                await self.browser.page.evaluate("""
                    () => {
                        document.querySelectorAll('button').forEach(btn => {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if ((t.includes('verify') || t.includes('submit') || t.includes('complete') ||
                                 t.includes('check') || t.includes('done')) &&
                                btn.offsetParent && !btn.disabled) {
                                btn.click();
                            }
                        });
                    }
                """)
                await asyncio.sleep(0.5)

            return result > 0
        except Exception as e:
            print(f"    -> checkbox error: {e}", flush=True)
            return False

    async def _try_slider_challenge(self) -> bool:
        """Handle Slider/Range Challenge - move slider to target value."""
        try:
            # Get slider info and target value
            slider_info = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    // Find target value (e.g., "Move slider to 75" or "Set value to 42")
                    const targetMatch = text.match(/(?:to|value|target)[:\\s]+(\\d+)/i);
                    const target = targetMatch ? parseInt(targetMatch[1]) : null;

                    const slider = document.querySelector('input[type="range"]');
                    if (!slider) return {found: false};

                    const rect = slider.getBoundingClientRect();
                    const min = parseFloat(slider.min) || 0;
                    const max = parseFloat(slider.max) || 100;
                    const current = parseFloat(slider.value);

                    return {found: true, target, min, max, current,
                            x: rect.x, y: rect.y + rect.height/2, width: rect.width};
                }
            """)

            if not slider_info.get('found'):
                return False

            target = slider_info.get('target')
            smin = slider_info.get('min', 0)
            smax = slider_info.get('max', 100)
            print(f"    -> slider: target={target}, range={smin}-{smax}, current={slider_info.get('current')}", flush=True)

            if target is not None:
                # Set slider value via JS with React-compatible events
                await self.browser.page.evaluate(f"""
                    () => {{
                        const slider = document.querySelector('input[type="range"]');
                        if (!slider) return;
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(slider, '{target}');
                        slider.dispatchEvent(new Event('input', {{bubbles: true}}));
                        slider.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                """)
                await asyncio.sleep(0.3)

                # Also try mouse drag to the target position
                x_start = slider_info['x']
                y = slider_info['y']
                width = slider_info['width']
                ratio = (target - smin) / (smax - smin) if smax > smin else 0.5
                x_target = x_start + width * ratio

                await self.browser.page.mouse.click(x_target, y)
                await asyncio.sleep(0.3)
            else:
                # No target specified - try moving to various positions
                for ratio in [0.25, 0.5, 0.75, 1.0]:
                    x = slider_info['x'] + slider_info['width'] * ratio
                    await self.browser.page.mouse.click(x, slider_info['y'])
                    await asyncio.sleep(0.3)

            # Click verify/submit
            await self.browser.page.evaluate("""
                () => {
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('verify') || t.includes('submit') || t.includes('check') ||
                             t.includes('confirm') || t.includes('done')) &&
                            btn.offsetParent && !btn.disabled) {
                            btn.click();
                        }
                    });
                }
            """)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"    -> slider error: {e}", flush=True)
            return False

    async def _try_color_challenge(self) -> bool:
        """Handle Color Challenge - click correct color element."""
        try:
            result = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText.toLowerCase();
                    // Parse target color from instructions
                    const colors = ['red', 'blue', 'green', 'yellow', 'purple', 'orange', 'pink', 'cyan', 'white', 'black'];
                    let targetColor = null;
                    for (const c of colors) {
                        if (text.includes('click') && text.includes(c) ||
                            text.includes('select') && text.includes(c) ||
                            text.includes('find') && text.includes(c)) {
                            targetColor = c;
                            break;
                        }
                    }

                    if (!targetColor) return {clicked: 0, error: 'no target color found'};

                    // Click elements matching the target color
                    let clicked = 0;
                    const colorMap = {
                        'red': ['bg-red', 'red-500', 'red-600', 'red-700'],
                        'blue': ['bg-blue', 'blue-500', 'blue-600', 'blue-700'],
                        'green': ['bg-green', 'green-500', 'green-600', 'green-700'],
                        'yellow': ['bg-yellow', 'yellow-500', 'yellow-600', 'yellow-400'],
                        'purple': ['bg-purple', 'purple-500', 'purple-600', 'purple-700'],
                        'orange': ['bg-orange', 'orange-500', 'orange-600', 'orange-400'],
                        'pink': ['bg-pink', 'pink-500', 'pink-600', 'pink-400'],
                        'cyan': ['bg-cyan', 'cyan-500', 'cyan-600', 'cyan-400'],
                    };
                    const targetClasses = colorMap[targetColor] || [`bg-${targetColor}`];

                    document.querySelectorAll('div, button, span').forEach(el => {
                        const cls = el.className || '';
                        for (const tc of targetClasses) {
                            if (cls.includes(tc) && el.offsetParent && el.offsetWidth > 15) {
                                el.click();
                                clicked++;
                            }
                        }
                    });

                    // Also check by computed background color
                    if (clicked === 0) {
                        document.querySelectorAll('div, button').forEach(el => {
                            if (!el.offsetParent || el.offsetWidth < 15) return;
                            const bg = getComputedStyle(el).backgroundColor;
                            const m = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                            if (!m) return;
                            const [_, r, g, b] = m.map(Number);
                            let match = false;
                            if (targetColor === 'red' && r > 180 && g < 100 && b < 100) match = true;
                            if (targetColor === 'blue' && r < 100 && g < 100 && b > 180) match = true;
                            if (targetColor === 'green' && r < 100 && g > 150 && b < 100) match = true;
                            if (targetColor === 'yellow' && r > 200 && g > 200 && b < 100) match = true;
                            if (match) { el.click(); clicked++; }
                        });
                    }

                    return {clicked, targetColor};
                }
            """)
            print(f"    -> color: {result}", flush=True)

            if result.get('clicked', 0) > 0:
                await asyncio.sleep(0.5)
                await self.browser.page.evaluate("""
                    () => {
                        document.querySelectorAll('button').forEach(btn => {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if ((t.includes('verify') || t.includes('submit') || t.includes('check')) &&
                                btn.offsetParent && !btn.disabled) btn.click();
                        });
                    }
                """)
                await asyncio.sleep(0.5)

            return result.get('clicked', 0) > 0
        except Exception as e:
            print(f"    -> color challenge error: {e}", flush=True)
            return False

    async def _try_sort_challenge(self) -> bool:
        """Handle Sort/Order Challenge - arrange items in correct order."""
        try:
            # Try clicking items in the correct order or sorting via drag
            result = await self.browser.page.evaluate("""
                () => {
                    const text = document.body.innerText.toLowerCase();
                    const isAscending = text.includes('ascending') || text.includes('smallest') ||
                                        text.includes('a to z') || text.includes('1 to');
                    const isDescending = text.includes('descending') || text.includes('largest') ||
                                         text.includes('z to a');

                    // Find sortable items (numbered items, list items)
                    const items = [];
                    document.querySelectorAll('[draggable="true"], [class*="cursor-grab"], li, [class*="sortable"]').forEach(el => {
                        if (el.offsetParent && el.offsetWidth > 20) {
                            const t = el.textContent.trim();
                            const numMatch = t.match(/\\d+/);
                            const num = numMatch ? parseInt(numMatch[0]) : null;
                            const rect = el.getBoundingClientRect();
                            items.push({text: t, num, y: rect.y, x: rect.x, w: rect.width, h: rect.height});
                        }
                    });

                    // Try clicking sort buttons
                    let sortClicked = false;
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('sort') || t.includes('order') || t.includes('arrange')) &&
                            btn.offsetParent && !btn.disabled) {
                            btn.click();
                            sortClicked = true;
                        }
                    });

                    // Click up/down arrows to rearrange
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim();
                        if ((t === '▲' || t === '▼' || t === '↑' || t === '↓') && btn.offsetParent) {
                            // Click multiple times to sort
                            for (let i = 0; i < 5; i++) btn.click();
                        }
                    });

                    return {items: items.length, sortClicked, direction: isAscending ? 'asc' : isDescending ? 'desc' : 'unknown'};
                }
            """)
            print(f"    -> sort: {result}", flush=True)

            # Click verify/submit
            await asyncio.sleep(0.5)
            await self.browser.page.evaluate("""
                () => {
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('verify') || t.includes('submit') || t.includes('check') ||
                             t.includes('done') || t.includes('complete')) &&
                            btn.offsetParent && !btn.disabled) btn.click();
                    });
                }
            """)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"    -> sort error: {e}", flush=True)
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
                const result = {accept: 0, red: 0, gray: 0, submit: 0, reveal: 0};
                const clicked = new Set();

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
                document.querySelectorAll('button').forEach(btn => {
                    if (btn.offsetParent && !clicked.has(btn)) {
                        const style = getComputedStyle(btn);
                        const bg = style.backgroundColor;
                        const text = btn.textContent.trim();

                        // Check for X symbol
                        if (text === '×' || text === 'X' || text === '✕') {
                            btn.click();
                            clicked.add(btn);
                            result.gray++;
                            return;
                        }

                        // Check for red/pink background
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
        url_before = await self.browser.get_url()
        any_submitted = False

        for code in codes:
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

                # Click submit button via JS (to bypass any blocking popups)
                clicked = await self.browser.page.evaluate("""
                    () => {
                        let btn = document.querySelector('button[type="submit"]');
                        if (!btn) {
                            const btns = document.querySelectorAll('button');
                            for (const b of btns) {
                                if (b.textContent.includes('Submit') && !b.disabled) {
                                    btn = b;
                                    break;
                                }
                            }
                        }
                        if (btn && !btn.disabled) {
                            btn.click();
                            return true;
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

                # Code was wrong, try the next one
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
