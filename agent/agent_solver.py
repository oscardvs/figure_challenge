"""Agent-based challenge solver using Gemini 3 vision models.

Instead of hardcoding 20+ challenge types with heuristics, this solver
uses Gemini 3 Flash/Pro to SEE the page and REASON about what to do.
The AI agent handles the tricky challenges (scroll reveals, hover codes,
trap buttons) while deterministic JS handles popups and code submission.
"""
import asyncio
import re
import time
import base64

from browser import BrowserController
from agent_vision import AgentVision, ActionType
from dom_parser import extract_hidden_codes
from metrics import MetricsTracker


class AgentChallengeSolver:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.browser = BrowserController()
        self.vision = AgentVision(api_key)
        self.metrics = MetricsTracker()
        self.current_step = 0

    async def run(self, start_url: str, headless: bool = False) -> dict:
        """Run through all 30 challenges."""
        await self.browser.start(start_url, headless=headless)

        try:
            await asyncio.sleep(2)
            print("Clicking START button...", flush=True)
            await self.browser.click_by_text("START")
            await asyncio.sleep(1)

            run_start = time.time()
            for step in range(1, 31):
                self.current_step = step
                self.metrics.start_challenge(step)
                step_start = time.time()
                elapsed = step_start - run_start
                print(f"\n{'='*60}", flush=True)
                print(f"  STEP {step}/30  (elapsed: {elapsed:.1f}s)", flush=True)
                print(f"{'='*60}", flush=True)

                success = await self._solve_step(step)

                step_time = time.time() - step_start
                status = "PASSED" if success else "FAILED"
                print(f"  [{step_time:.1f}s] Step {step} {status}", flush=True)

                if not success:
                    self.metrics.end_challenge(step, success=False, error="Max attempts reached")
        finally:
            await self.browser.stop()
            self.metrics.print_summary()

        return self.metrics.get_summary()

    async def _solve_step(self, step: int) -> bool:
        """Solve a single challenge step using the agent loop."""
        total_tin = 0
        total_tout = 0
        failed_codes: list[str] = []
        action_history: list[str] = []
        max_attempts = 15

        # Wait for React to render
        await self._wait_for_content()

        for attempt in range(max_attempts):
            # 1. Check if already progressed
            url = await self.browser.get_url()
            if self._check_progress(url, step):
                self.metrics.end_challenge(step, True, total_tin, total_tout)
                print(f"  >>> PASSED <<<", flush=True)
                return True

            # 2. Clear popups (fast, deterministic JS)
            cleared = await self._clear_popups()
            if cleared > 0:
                print(f"  Cleared {cleared} popups", flush=True)
                await asyncio.sleep(0.2)

            # 3. Attempt 0: fast path - DOM extraction + basic interactions
            if attempt == 0:
                # Scroll to trigger scroll-reveal challenges
                await self.browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3)
                await self.browser.page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.2)
                # Scroll again (some need 500px+)
                await self.browser.page.evaluate("window.scrollTo(0, 1000)")
                await asyncio.sleep(0.3)

                # Click Reveal Code buttons
                await self.browser.page.evaluate("""() => {
                    document.querySelectorAll('button').forEach(btn => {
                        const t = btn.textContent.toLowerCase();
                        if ((t.includes('reveal') || t.includes('accept')) && btn.offsetParent && !btn.disabled) {
                            btn.click();
                        }
                    });
                }""")
                await asyncio.sleep(0.3)

                # Click "click here to reveal" elements multiple times
                for _ in range(5):
                    clicked = await self.browser.page.evaluate("""() => {
                        let clicked = 0;
                        document.querySelectorAll('div, p, span').forEach(el => {
                            const text = el.textContent || '';
                            if (text.includes('click here') && text.includes('to reveal')) {
                                el.click();
                                clicked++;
                            }
                        });
                        return clicked;
                    }""")
                    if clicked == 0:
                        break
                    await asyncio.sleep(0.2)

                # Scroll modal containers
                await self.browser.page.evaluate("""() => {
                    document.querySelectorAll('[class*="overflow-y"], [class*="overflow-auto"], [class*="max-h"]').forEach(el => {
                        if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
                    });
                }""")

                # Try DOM codes
                html = await self.browser.get_html()
                codes = extract_hidden_codes(html)
                if codes:
                    print(f"  DOM codes: {codes}", flush=True)
                    for code in codes:
                        if code in failed_codes:
                            continue
                        if await self._fill_and_submit(code, step):
                            self.metrics.end_challenge(step, True, total_tin, total_tout)
                            print(f"  >>> PASSED <<<", flush=True)
                            return True
                        failed_codes.append(code)

                # Check for radio modal - brute force (handles native + custom)
                if await self._brute_force_radio(step):
                    self.metrics.end_challenge(step, True, total_tin, total_tout)
                    print(f"  >>> PASSED <<<", flush=True)
                    return True

                # Handle keyboard sequences
                html_text = await self.browser.page.evaluate("() => document.body.textContent || ''")
                if 'keyboard sequence' in html_text.lower() or ('press' in html_text.lower() and 'keys' in html_text.lower()):
                    keys = re.findall(r'((?:Control|Shift|Alt|Meta)\+[A-Za-z0-9])', html_text)
                    seen = set()
                    unique_keys = []
                    for k in keys:
                        if k not in seen:
                            seen.add(k)
                            unique_keys.append(k)
                    if unique_keys:
                        print(f"  Keyboard sequence: {unique_keys}", flush=True)
                        await self.browser.page.evaluate("() => document.body.focus()")
                        for k in unique_keys:
                            await self.browser.page.keyboard.press(k)
                            await asyncio.sleep(0.3)

                # Handle math puzzles
                if 'puzzle' in html_text.lower() and ('= ?' in html_text or '=?' in html_text):
                    math_code = await self._try_math_puzzle()
                    if math_code and math_code not in failed_codes:
                        if await self._fill_and_submit(math_code, step):
                            self.metrics.end_challenge(step, True, total_tin, total_tout)
                            print(f"  >>> PASSED <<<", flush=True)
                            return True
                        failed_codes.append(math_code)

                # Handle timing/capture challenges
                if 'capture' in html_text.lower() and ('timing' in html_text.lower() or 'second' in html_text.lower()):
                    for _ in range(5):
                        await self.browser.page.evaluate("""() => {
                            const btns = [...document.querySelectorAll('button')];
                            for (const btn of btns) {
                                const t = (btn.textContent || '').trim().toLowerCase();
                                if (t.includes('capture') && btn.offsetParent && !btn.disabled) {
                                    btn.click(); return true;
                                }
                            }
                            return false;
                        }""")
                        await asyncio.sleep(1.0)

                # Handle hover challenge
                if 'hover' in html_text.lower() and ('reveal' in html_text.lower() or 'code' in html_text.lower()):
                    # Find hover targets and hover for 1.5s
                    target = await self.browser.page.evaluate("""() => {
                        // Remove floating decoys first
                        const decoys = ['Click Me!', 'Button!', 'Link!', 'Here!', 'Click Here!', 'Try This!'];
                        document.querySelectorAll('div, button, span').forEach(el => {
                            const style = getComputedStyle(el);
                            if ((style.position === 'absolute' || style.position === 'fixed') && decoys.includes(el.textContent.trim())) {
                                el.style.display = 'none';
                            }
                        });
                        // Find hover target
                        const candidates = [...document.querySelectorAll('[class*="cursor-pointer"]')].filter(el =>
                            el.offsetParent && el.offsetWidth > 50 && el.offsetHeight > 30 &&
                            !el.closest('.fixed:not(:has(input[type="text"]))'));
                        if (candidates.length === 0) {
                            const bordered = [...document.querySelectorAll('div')].filter(el => {
                                const cls = el.className || '';
                                return cls.includes('border-2') && cls.includes('rounded') && el.offsetParent && el.offsetWidth > 50;
                            });
                            if (bordered.length > 0) candidates.push(...bordered);
                        }
                        if (candidates.length === 0) return null;
                        const el = candidates[0];
                        el.scrollIntoView({behavior: 'instant', block: 'center'});
                        const rect = el.getBoundingClientRect();
                        // Dispatch hover events
                        const opts = {bubbles: true, clientX: rect.x + rect.width/2, clientY: rect.y + rect.height/2};
                        el.dispatchEvent(new MouseEvent('mouseenter', opts));
                        el.dispatchEvent(new MouseEvent('mouseover', opts));
                        return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                    }""")
                    if target:
                        await self.browser.page.mouse.move(target['x'], target['y'])
                        await asyncio.sleep(1.5)
                        print(f"  Hovered target for 1.5s", flush=True)

                # Handle "I Remember" buttons (memory challenge)
                await self.browser.page.evaluate("""() => {
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t.includes('i remember') && btn.offsetParent && !btn.disabled) btn.click();
                    });
                }""")

                # Handle audio challenge (headless Chromium has no TTS voices)
                if 'audio' in html_text.lower() and ('play' in html_text.lower() or 'listen' in html_text.lower()):
                    await self._try_audio_challenge()

                # Handle canvas drawing challenge
                has_canvas = await self.browser.page.evaluate("() => !!document.querySelector('canvas')")
                if has_canvas and ('draw' in html_text.lower() or 'canvas' in html_text.lower() or 'stroke' in html_text.lower()):
                    await self._try_canvas_challenge()

                # Handle split parts challenge
                if 'part' in html_text.lower() and ('found' in html_text.lower() or 'collect' in html_text.lower()):
                    await self._try_split_parts()

                # Handle rotating code challenge
                if 'rotat' in html_text.lower() and 'capture' in html_text.lower():
                    await self._try_rotating_code()

                # Handle multi-tab challenge
                if 'tab' in html_text.lower() and ('click' in html_text.lower() or 'visit' in html_text.lower()):
                    await self._try_multi_tab()

                # Handle sequence challenge (click, hover, type, scroll)
                if 'sequence' in html_text.lower() or ('click' in html_text.lower() and 'hover' in html_text.lower() and 'type' in html_text.lower()):
                    await self._try_sequence_challenge()

                # Handle video frames challenge
                if 'frame' in html_text.lower() and ('navigate' in html_text.lower() or '+1' in html_text or '-1' in html_text):
                    await self._try_video_challenge()

                # Handle "Scroll Down to Find Navigation" - check headers, main container, and page structure
                is_scroll_challenge = await self.browser.page.evaluate("""() => {
                    // Check heading/prominent text elements for scroll instruction
                    const els = document.querySelectorAll('h1, h2, h3, .text-2xl, .text-3xl, .text-xl, .font-bold, .text-lg');
                    for (const el of els) {
                        const t = (el.textContent || '').toLowerCase();
                        if (t.includes('scroll down to find') || t.includes('scroll to find')) return true;
                    }
                    // Check the main challenge box
                    const mainBox = document.querySelector('.max-w-6xl, .max-w-4xl, .max-w-3xl');
                    if (mainBox) {
                        const t = (mainBox.textContent || '').toLowerCase();
                        if (t.includes('scroll down') && (t.includes('navigation') || t.includes('navigate') || t.includes('nav button'))) return true;
                    }
                    // Check body text for scroll-related instructions
                    const bodyText = (document.body.textContent || '').toLowerCase();
                    if (bodyText.includes('keep scrolling') && bodyText.includes('navigation button')) return true;
                    // Structural check: many sections with filler text + scroll height > 5000px
                    if (document.body.scrollHeight > 5000) {
                        const sections = document.querySelectorAll('[class*="section"], [class*="Section"]');
                        const sectionDivs = [...document.querySelectorAll('div')].filter(el => {
                            const t = (el.textContent || '').trim();
                            return t.match(/^Section \\d+/) && t.length > 50;
                        });
                        if (sections.length > 10 || sectionDivs.length > 10) return true;
                    }
                    return false;
                }""")
                if is_scroll_challenge:
                    all_codes = list(codes) + list(failed_codes) if codes else list(failed_codes)
                    if await self._try_scroll_to_find_nav(all_codes):
                        url = await self.browser.get_url()
                        if self._check_progress(url, step):
                            self.metrics.end_challenge(step, True, total_tin, total_tout)
                            print(f"  >>> PASSED <<<", flush=True)
                            return True

                # Handle "Delayed Reveal" - wait for timer (only if there's an actual countdown)
                has_timer = await self.browser.page.evaluate("""() => {
                    const text = document.body.textContent || '';
                    return !!(text.match(/\\d+\\.?\\d*\\s*s(?:econds?)?\\s*remaining/i) ||
                              text.match(/delayed\\s+reveal/i));
                }""")
                if has_timer:
                    await asyncio.sleep(4.0)
                    print(f"  Waited 4.0s for delayed reveal", flush=True)

                # Extract codes right after math puzzle and delayed reveal (before scroll changes page)
                html_fresh = await self.browser.get_html()
                fresh_codes = extract_hidden_codes(html_fresh)
                for code in fresh_codes:
                    if code in failed_codes:
                        continue
                    if await self._fill_and_submit(code, step):
                        self.metrics.end_challenge(step, True, total_tin, total_tout)
                        print(f"  >>> PASSED <<<", flush=True)
                        return True
                    failed_codes.append(code)

                # Hide floating decoy elements that obstruct drag area
                await self.browser.page.evaluate("""() => {
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
                }""")
                # Handle drag-and-drop via JS events
                await self.browser.page.evaluate("""() => {
                    const pieces = [...document.querySelectorAll('[draggable="true"]')];
                    const slots = [...document.querySelectorAll('div')].filter(el => {
                        const text = (el.textContent || '').trim();
                        const cls = el.className || '';
                        const style = el.getAttribute('style') || '';
                        return (text.match(/^Slot \\d+$/) &&
                               (cls.includes('dashed') || cls.includes('border-dashed') || style.includes('dashed'))) ||
                               (cls.includes('border-dashed') && el.children.length <= 2 && el.offsetWidth > 40);
                    });
                    const n = Math.min(pieces.length, slots.length, 6);
                    for (let i = 0; i < n; i++) {
                        try {
                            const dt = new DataTransfer();
                            dt.setData('text/plain', pieces[i].textContent.trim());
                            pieces[i].dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            slots[i].dispatchEvent(new DragEvent('dragenter', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            slots[i].dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            slots[i].dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
                            pieces[i].dispatchEvent(new DragEvent('dragend', {dataTransfer: dt, bubbles: true, cancelable: true}));
                        } catch(e) {}
                    }
                    // Click Complete/Done button
                    document.querySelectorAll('button').forEach(btn => {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if ((t.includes('complete') || t.includes('done') || t.includes('verify')) &&
                            !t.includes('clear') && btn.offsetParent && !btn.disabled) btn.click();
                    });
                }""")
                await asyncio.sleep(0.3)

                # Playwright mouse-based drag-and-drop fallback (if JS events didn't fill all slots)
                fill_count = await self.browser.page.evaluate("""() => {
                    const text = document.body.textContent || '';
                    const match = text.match(/(\\d+)\\/(\\d+)\\s*filled/);
                    return match ? parseInt(match[1]) : -1;
                }""")
                if fill_count >= 0 and fill_count < 6:
                    await self._try_mouse_drag_and_drop()

                # Re-extract codes after all fast path actions
                html = await self.browser.get_html()
                codes = extract_hidden_codes(html)
                for code in codes:
                    if code in failed_codes:
                        continue
                    if await self._fill_and_submit(code, step):
                        self.metrics.end_challenge(step, True, total_tin, total_tout)
                        print(f"  >>> PASSED <<<", flush=True)
                        return True
                    failed_codes.append(code)

                # Check progress after fast path
                url = await self.browser.get_url()
                if self._check_progress(url, step):
                    self.metrics.end_challenge(step, True, total_tin, total_tout)
                    print(f"  >>> PASSED <<<", flush=True)
                    return True

                # If all extracted codes failed and there are many trap buttons, try scroll-to-find
                if failed_codes:
                    trap_count = await self.browser.page.evaluate("""() => {
                        const TRAPS = ['proceed', 'continue', 'next step', 'next page', 'next section'];
                        return [...document.querySelectorAll('button')].filter(b => {
                            const t = (b.textContent || '').trim().toLowerCase();
                            return t.length < 40 && TRAPS.some(w => t.includes(w));
                        }).length;
                    }""")
                    if trap_count >= 8:
                        print(f"  {trap_count} trap buttons detected, trying scroll-to-find...", flush=True)
                        # Many trap buttons strongly suggests a scroll-to-find challenge
                        if await self._try_scroll_to_find_nav(list(failed_codes), deep_scroll=True):
                            url = await self.browser.get_url()
                            if self._check_progress(url, step):
                                self.metrics.end_challenge(step, True, total_tin, total_tout)
                                print(f"  >>> PASSED <<<", flush=True)
                                return True

                action_history.append("fast_path: scrolled, clicked reveals, tried DOM codes, handled specials")
                continue

            # 4. AI Agent: take screenshot, ask Gemini what to do
            print(f"  [attempt {attempt+1}] Asking Gemini...", flush=True)

            # Alternate scroll position for variety
            if attempt % 3 == 0:
                await self.browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif attempt % 3 == 1:
                await self.browser.page.evaluate("window.scrollTo(0, 0)")
            else:
                await self.browser.page.evaluate("window.scrollTo(0, 500)")
            await asyncio.sleep(0.3)

            screenshot = await self.browser.screenshot()
            html = await self.browser.get_html()
            dom_codes = extract_hidden_codes(html)

            action, tin, tout = self.vision.analyze(
                screenshot_bytes=screenshot,
                html_snippet=html[:6000],
                step=step,
                attempt=attempt,
                dom_codes=dom_codes,
                failed_codes=failed_codes,
                history=action_history,
            )
            total_tin += tin
            total_tout += tout

            # 5. If the agent found a code, try it immediately
            if action.code_found:
                code = action.code_found.upper().strip()
                if len(code) == 6 and code not in failed_codes:
                    print(f"  Agent found code: {code}", flush=True)
                    if await self._fill_and_submit(code, step):
                        self.metrics.end_challenge(step, True, total_tin, total_tout)
                        print(f"  >>> PASSED <<<", flush=True)
                        return True
                    failed_codes.append(code)

            # 6. Execute the agent's suggested action
            action_desc = await self._execute_action(action)
            action_history.append(action_desc)

            # 7. After action, try DOM codes again (action may have revealed new ones)
            await asyncio.sleep(0.3)
            html = await self.browser.get_html()
            new_codes = extract_hidden_codes(html)
            for code in new_codes:
                if code in failed_codes:
                    continue
                print(f"  New code after action: {code}", flush=True)
                if await self._fill_and_submit(code, step):
                    self.metrics.end_challenge(step, True, total_tin, total_tout)
                    print(f"  >>> PASSED <<<", flush=True)
                    return True
                failed_codes.append(code)

            # 8. Check progress
            url = await self.browser.get_url()
            if self._check_progress(url, step):
                self.metrics.end_challenge(step, True, total_tin, total_tout)
                print(f"  >>> PASSED <<<", flush=True)
                return True

            # 9. Every 4th attempt, try trap buttons with all known codes
            if attempt >= 4 and attempt % 4 == 0 and failed_codes:
                if await self._try_trap_buttons(step, failed_codes):
                    self.metrics.end_challenge(step, True, total_tin, total_tout)
                    print(f"  >>> PASSED <<<", flush=True)
                    return True

            # 10. Re-run key fast path handlers periodically
            if attempt >= 3 and attempt % 3 == 0:
                # Re-try scroll-to-find-nav, audio, canvas, etc.
                html_text = await self.browser.page.evaluate("() => document.body.textContent || ''")
                is_scroll_ch = await self.browser.page.evaluate("""() => {
                    const els = document.querySelectorAll('h1, h2, h3, .text-2xl, .text-3xl, .text-xl, .font-bold, .text-lg');
                    for (const el of els) {
                        const t = (el.textContent || '').toLowerCase();
                        if (t.includes('scroll down to find') || t.includes('scroll to find')) return true;
                    }
                    const mainBox = document.querySelector('.max-w-6xl, .max-w-4xl, .max-w-3xl');
                    if (mainBox) {
                        const t = (mainBox.textContent || '').toLowerCase();
                        if (t.includes('scroll down') && (t.includes('navigation') || t.includes('navigate') || t.includes('nav button'))) return true;
                    }
                    const bodyText = (document.body.textContent || '').toLowerCase();
                    if (bodyText.includes('keep scrolling') && bodyText.includes('navigation button')) return true;
                    if (document.body.scrollHeight > 5000) {
                        const sectionDivs = [...document.querySelectorAll('div')].filter(el => {
                            const t = (el.textContent || '').trim();
                            return t.match(/^Section \\d+/) && t.length > 50;
                        });
                        if (sectionDivs.length > 10) return true;
                    }
                    return false;
                }""")
                if is_scroll_ch:
                    if await self._try_scroll_to_find_nav(list(failed_codes)):
                        url = await self.browser.get_url()
                        if self._check_progress(url, step):
                            self.metrics.end_challenge(step, True, total_tin, total_tout)
                            print(f"  >>> PASSED <<<", flush=True)
                            return True
                if 'audio' in html_text.lower() and 'play' in html_text.lower():
                    await self._try_audio_challenge()
                if 'delayed' in html_text.lower() and 'remaining' in html_text.lower():
                    await asyncio.sleep(5.5)
                has_canvas = await self.browser.page.evaluate("() => !!document.querySelector('canvas')")
                if has_canvas:
                    await self._try_canvas_challenge()
                # Re-try drag-and-drop
                fill_count = await self.browser.page.evaluate("""() => {
                    const text = document.body.textContent || '';
                    const match = text.match(/(\\d+)\\/(\\d+)\\s*filled/);
                    return match ? parseInt(match[1]) : -1;
                }""")
                if fill_count >= 0 and fill_count < 6:
                    await self._try_mouse_drag_and_drop()
                # Re-extract codes
                html = await self.browser.get_html()
                new_codes = extract_hidden_codes(html)
                for code in new_codes:
                    if code in failed_codes:
                        continue
                    if await self._fill_and_submit(code, step):
                        self.metrics.end_challenge(step, True, total_tin, total_tout)
                        print(f"  >>> PASSED <<<", flush=True)
                        return True
                    failed_codes.append(code)

            # 11. Hide stuck modals after attempt 5 (they've been tried)
            if attempt == 5:
                hidden = await self._hide_stuck_modals()
                if hidden > 0:
                    print(f"  Hidden {hidden} stuck modals", flush=True)
                    # Re-try brute force radio (new options may be revealed)
                    if await self._brute_force_radio(step):
                        self.metrics.end_challenge(step, True, total_tin, total_tout)
                        print(f"  >>> PASSED <<<", flush=True)
                        return True

            await asyncio.sleep(0.1)

        return False

    # ── Deterministic helpers (no AI cost) ──────────────────────────────

    async def _wait_for_content(self) -> bool:
        """Wait for React SPA to render meaningful content."""
        for _ in range(10):
            html = await self.browser.get_html()
            if len(html) > 1000 and ("button" in html.lower() or "input" in html.lower()):
                return True
            await asyncio.sleep(0.5)
        return False

    def _check_progress(self, url: str, step: int) -> bool:
        """Check if URL indicates we've moved past current step."""
        url_lower = url.lower()
        if f"step{step + 1}" in url_lower or f"step-{step + 1}" in url_lower or f"step/{step + 1}" in url_lower:
            return True
        if step == 30 and ("complete" in url_lower or "finish" in url_lower or "done" in url_lower):
            return True
        match = re.search(r"step[/-]?(\d+)", url_lower)
        if match and int(match.group(1)) > step:
            return True
        return False

    async def _clear_popups(self) -> int:
        """Clear blocking popups using deterministic JS. Returns count cleared."""
        return await self.browser.page.evaluate("""() => {
            let cleared = 0;
            const hide = (el) => {
                el.style.display = 'none';
                el.style.pointerEvents = 'none';
                el.style.visibility = 'hidden';
                el.style.zIndex = '-1';
            };

            document.querySelectorAll('.fixed, [class*="absolute"], [class*="z-"]').forEach(el => {
                const text = el.textContent || '';

                // Popup with real dismiss button (fake one labeled, real one isn't)
                if (text.includes('fake') && text.includes('real one')) {
                    el.querySelectorAll('button').forEach(btn => {
                        const bt = (btn.textContent || '').trim();
                        if (!bt.toLowerCase().includes('fake') && bt.length > 0 && bt.length < 30) {
                            btn.click();
                            cleared++;
                        }
                    });
                }

                // Popup where ALL close buttons are fake
                if (text.includes('another way to close') ||
                    (text.includes('close button') && text.includes('fake') && !text.includes('real one')) ||
                    text.includes('won a prize') || text.includes('amazing deals')) {
                    hide(el);
                    cleared++;
                }

                // "That close button is fake!" warnings
                if (text.includes('That close button is fake')) {
                    hide(el);
                    cleared++;
                }

                // Cookie consent
                if (text.includes('Cookie') || text.includes('cookie')) {
                    const btn = [...el.querySelectorAll('button')].find(b => b.textContent.includes('Accept'));
                    if (btn) { btn.click(); cleared++; }
                }

                // Limited time offer / Click X to close
                if (text.includes('Limited time offer') || text.includes('Click X to close') ||
                    text.includes('popup message')) {
                    el.querySelectorAll('button').forEach(btn => btn.click());
                    hide(el);
                    cleared++;
                }

                // "Click the button to dismiss" modals
                if (text.includes('Click the button to dismiss') || text.includes('interact with this modal')) {
                    const btn = el.querySelector('button');
                    if (btn) { btn.click(); cleared++; }
                }

                // "Wrong Button" modals
                if (text.includes('Wrong Button') || text.includes('Try Again')) {
                    const btn = el.querySelector('button');
                    if (btn) { btn.click(); cleared++; }
                }
            });

            // Disable bg-black/70 overlays
            document.querySelectorAll('.fixed').forEach(el => {
                if (el.classList.contains('bg-black/70') ||
                    (el.style.backgroundColor || '').includes('rgba(0, 0, 0')) {
                    if (!el.textContent.includes('Step') && !el.querySelector('input[type="radio"]')) {
                        el.style.pointerEvents = 'none';
                        cleared++;
                    }
                }
            });

            return cleared;
        }""")

    async def _fill_and_submit(self, code: str, step: int) -> bool:
        """Fill code into input, click submit, check if URL changed."""
        url_before = await self.browser.get_url()

        try:
            # Scroll input into view
            await self.browser.page.evaluate("""() => {
                const input = document.querySelector('input[placeholder*="code" i], input[type="text"]');
                if (input) input.scrollIntoView({behavior: 'instant', block: 'center'});
            }""")
            await asyncio.sleep(0.1)

            # Clear and type
            inp = self.browser.page.locator('input[placeholder*="code" i], input[type="text"]').first
            try:
                await inp.click(click_count=3, timeout=1000)
            except Exception:
                await self.browser.page.evaluate("""() => {
                    const input = document.querySelector('input[placeholder*="code" i], input[type="text"]');
                    if (input) { input.focus(); input.select(); }
                }""")
            await self.browser.page.keyboard.press("Backspace")
            await asyncio.sleep(0.05)
            await self.browser.page.keyboard.type(code, delay=20)
            await asyncio.sleep(0.15)

            # Click submit (avoid trap buttons)
            clicked = await self.browser.page.evaluate("""() => {
                const TRAPS = ['proceed', 'continue', 'next step', 'next page', 'next section',
                    'move on', 'go forward', 'keep going', 'advance', 'continue reading',
                    'continue journey', 'click here', 'proceed forward'];
                const isTrap = (t) => TRAPS.some(w => t.toLowerCase().includes(w));

                const input = document.querySelector('input[placeholder*="code" i], input[type="text"]');
                if (!input) return false;

                // Search in parent containers
                let container = input.parentElement;
                for (let i = 0; i < 4 && container; i++) {
                    const btns = container.querySelectorAll('button');
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim();
                        if (!btn.disabled && !isTrap(t) &&
                            (btn.type === 'submit' || t.includes('Submit') || t.includes('Go') || t === '→' || t.length <= 2)) {
                            btn.scrollIntoView({behavior: 'instant', block: 'center'});
                            btn.click();
                            return true;
                        }
                    }
                    // Single non-trap button in container
                    const safe = [...btns].filter(b => !b.disabled && !isTrap((b.textContent || '').trim()));
                    if (safe.length === 1) { safe[0].click(); return true; }
                    container = container.parentElement;
                }
                // Fallback: exact "Submit" or "Submit Code"
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.textContent || '').trim();
                    if ((t === 'Submit' || t === 'Submit Code') && !b.disabled) { b.click(); return true; }
                }
                return false;
            }""")

            if not clicked:
                await self.browser.page.keyboard.press("Enter")

            await asyncio.sleep(0.4)
            url_after = await self.browser.get_url()

            if url_after != url_before:
                print(f"    Code '{code}' WORKED!", flush=True)
                return True
            else:
                print(f"    Code '{code}' failed", flush=True)
                return False
        except Exception as e:
            print(f"    Fill error: {e}", flush=True)
            return False

    async def _brute_force_radio(self, step: int) -> bool:
        """Try all radio/option elements (native + custom). Brute force each + Submit."""
        # Scroll modal containers to reveal radio options (multiple strategies)
        await self.browser.page.evaluate("""() => {
            // Strategy 1: CSS class-based scroll
            document.querySelectorAll('[class*="overflow-y"], [class*="overflow-auto"], [class*="max-h"]').forEach(el => {
                if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
            });
            // Strategy 2: Scroll ALL scrollable children inside fixed modals
            document.querySelectorAll('.fixed').forEach(modal => {
                const scrollables = modal.querySelectorAll('*');
                scrollables.forEach(el => {
                    if (el.scrollHeight > el.clientHeight + 10) el.scrollTop = el.scrollHeight;
                });
            });
        }""")
        await asyncio.sleep(0.2)
        # Also use Playwright mouse wheel to scroll inside the modal
        modal_center = await self.browser.page.evaluate("""() => {
            const modal = [...document.querySelectorAll('.fixed')].find(el =>
                el.textContent.includes('Please Select') || el.textContent.includes('Submit & Continue'));
            if (!modal) return null;
            const rect = modal.getBoundingClientRect();
            return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
        }""")
        if modal_center:
            await self.browser.page.mouse.move(modal_center['x'], modal_center['y'])
            for _ in range(5):
                await self.browser.page.mouse.wheel(0, 500)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)

        # Count options: native radios, role-based, OR custom option cards
        count = await self.browser.page.evaluate("""() => {
            // Strategy 1: Native radio inputs
            let opts = document.querySelectorAll('input[type="radio"]');
            if (opts.length > 0) return {count: opts.length, type: 'native'};
            // Strategy 2: Role-based radios
            opts = document.querySelectorAll('[role="radio"]');
            if (opts.length > 0) return {count: opts.length, type: 'role'};
            // Strategy 3: Custom option cards near Submit button
            const submitBtn = [...document.querySelectorAll('button')].find(b =>
                b.textContent.includes('Submit & Continue') || b.textContent.includes('Submit and Continue'));
            if (!submitBtn) return {count: 0, type: 'none'};
            // Walk up to find the modal container
            let modal = submitBtn.parentElement;
            while (modal && modal !== document.body) {
                if (modal.querySelector('[class*="overflow"]') || modal.querySelector('[class*="max-h"]') ||
                    modal.classList.contains('fixed')) break;
                modal = modal.parentElement;
            }
            if (!modal || modal === document.body) modal = submitBtn.closest('div[class*="bg-white"], div[class*="rounded"]');
            if (!modal) return {count: 0, type: 'none'};
            const cards = [...modal.querySelectorAll('[class*="cursor-pointer"], [class*="border"][class*="rounded"]')].filter(el => {
                const text = el.textContent.trim();
                return text.length > 0 && text.length < 80 &&
                    !text.includes('Submit') && !text.includes('Section') &&
                    !text.includes('lorem ipsum') && !text.includes('Introduction');
            });
            return {count: cards.length, type: 'custom'};
        }""")

        radio_count = count.get('count', 0) if isinstance(count, dict) else 0
        radio_type = count.get('type', 'none') if isinstance(count, dict) else 'none'

        # Also detect "Please Select an Option" text as radio modal indicator
        if radio_count == 0:
            has_text = await self.browser.page.evaluate("""() => {
                const text = (document.body.textContent || '').toLowerCase();
                return text.includes('please select an option') && text.includes('submit');
            }""")
            if has_text:
                radio_type = 'text_detected'
                # Try clicking all bordered divs inside the modal
                radio_count = await self.browser.page.evaluate("""() => {
                    const modal = [...document.querySelectorAll('.fixed')].find(el =>
                        el.textContent.includes('Please Select') || el.textContent.includes('Submit & Continue'));
                    if (!modal) return 0;
                    const cards = [...modal.querySelectorAll('div[class*="border"], div[class*="cursor"], label')].filter(el => {
                        const t = el.textContent.trim();
                        return t.length > 0 && t.length < 80 && !t.includes('Submit') && el.offsetParent;
                    });
                    return cards.length;
                }""")

        if radio_count == 0:
            return False

        print(f"  Brute-forcing {radio_count} {radio_type} options...", flush=True)

        for i in range(radio_count):
            await self.browser.page.evaluate("""(idx) => {
                // Find all option elements (re-find for React re-renders)
                let options = [...document.querySelectorAll('input[type="radio"]')];
                if (options.length === 0) options = [...document.querySelectorAll('[role="radio"]')];
                if (options.length === 0) {
                    // Custom option cards
                    const submitBtn = [...document.querySelectorAll('button')].find(b =>
                        b.textContent.includes('Submit & Continue') || b.textContent.includes('Submit and Continue'));
                    if (!submitBtn) return;
                    let modal = submitBtn.parentElement;
                    while (modal && modal !== document.body) {
                        if (modal.querySelector('[class*="overflow"]') || modal.classList.contains('fixed')) break;
                        modal = modal.parentElement;
                    }
                    if (!modal || modal === document.body) modal = submitBtn.closest('.fixed') || submitBtn.closest('div[class*="bg-white"]');
                    if (modal) {
                        options = [...modal.querySelectorAll('[class*="cursor-pointer"], [class*="border"][class*="rounded"], label')].filter(el => {
                            const t = el.textContent.trim();
                            return t.length > 0 && t.length < 80 && !t.includes('Submit') && !t.includes('Section');
                        });
                    }
                }
                const opt = options[idx];
                if (!opt) return;
                opt.click();
                // Also click inner radio if exists
                const innerRadio = opt.querySelector('input[type="radio"]');
                if (innerRadio) innerRadio.click();
                // Click parent card for custom components
                const card = opt.closest('label, [class*="cursor-pointer"]');
                if (card && card !== opt) card.click();
                // Click Submit
                const btns = [...document.querySelectorAll('button')];
                const sub = btns.find(b => b.textContent.includes('Submit'));
                if (sub) sub.click();
            }""", i)
            await asyncio.sleep(0.15)
            url = await self.browser.get_url()
            if self._check_progress(url, step):
                print(f"  Radio option {i+1}/{radio_count} CORRECT!", flush=True)
                return True

        # Hide modal if all wrong
        print(f"  All {radio_count} radio options wrong, hiding modal", flush=True)
        await self.browser.page.evaluate("""() => {
            document.querySelectorAll('.fixed').forEach(el => {
                const text = el.textContent || '';
                if (el.querySelector('input[type="radio"]') || el.querySelector('[role="radio"]') ||
                    text.includes('Please Select') || text.includes('Submit & Continue')) {
                    el.style.display = 'none';
                    el.style.visibility = 'hidden';
                    el.style.pointerEvents = 'none';
                }
            });
        }""")
        return False

    async def _try_trap_buttons(self, step: int, codes: list[str]) -> bool:
        """Try clicking trap-labeled buttons with each code (some are actually real)."""
        TRAP_WORDS_JS = """['proceed', 'continue', 'next step', 'next page', 'next section',
            'move on', 'go forward', 'keep going', 'advance', 'continue journey',
            'click here', 'proceed forward', 'continue reading', 'next', 'go', 'submit code', 'submit']"""

        count = await self.browser.page.evaluate(f"""() => {{
            const TRAPS = {TRAP_WORDS_JS};
            return [...document.querySelectorAll('button, a')].filter(el => {{
                const t = (el.textContent || '').trim().toLowerCase();
                return t.length < 40 && TRAPS.some(w => t === w || t.includes(w));
            }}).length;
        }}""")

        if count == 0:
            return False

        print(f"  Trying {len(codes)} codes with {count} trap buttons...", flush=True)

        for code in codes[:5]:
            for i in range(min(count, 40)):
                # Clear any blocking popups from previous wrong-button clicks
                await self._clear_popups()

                await self.browser.page.evaluate(f"""(code) => {{
                    const input = document.querySelector('input[placeholder*="code" i], input[type="text"]');
                    if (input) {{
                        const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        s.call(input, code);
                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                        input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}""", code)
                await asyncio.sleep(0.05)

                await self.browser.page.evaluate(f"""(idx) => {{
                    const TRAPS = {TRAP_WORDS_JS};
                    const btns = [...document.querySelectorAll('button, a')].filter(el => {{
                        const t = (el.textContent || '').trim().toLowerCase();
                        return t.length < 40 && TRAPS.some(w => t === w || t.includes(w));
                    }});
                    const btn = btns[idx];
                    if (btn) {{
                        btn.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        btn.click();
                    }}
                }}""", i)
                await asyncio.sleep(0.15)
                url = await self.browser.get_url()
                if self._check_progress(url, step):
                    return True

        return False

    async def _try_math_puzzle(self) -> str | None:
        """Solve math expression, type answer, click Solve. Returns revealed code if found."""
        expr = await self.browser.page.evaluate("""() => {
            const text = document.body.textContent || '';
            const m = text.match(/(\\d+)\\s*([+\\-*×÷\\/])\\s*(\\d+)\\s*=\\s*\\?/);
            if (!m) return null;
            const a = parseInt(m[1]), op = m[2], b = parseInt(m[3]);
            let answer;
            switch(op) {
                case '+': answer = a + b; break;
                case '-': answer = a - b; break;
                case '*': case '×': answer = a * b; break;
                case '/': case '÷': answer = Math.floor(a / b); break;
                default: answer = a + b;
            }
            return String(answer);
        }""")
        if not expr:
            return None
        print(f"  Math puzzle answer: {expr}", flush=True)
        # Record codes BEFORE solving to detect new ones
        codes_before = set(await self.browser.page.evaluate("""() => {
            const text = document.body.textContent || '';
            const codes = text.match(/\\b[A-Z0-9]{6}\\b/g) || [];
            return [...new Set(codes)];
        }"""))
        await self.browser.page.evaluate(f"""() => {{
            const input = document.querySelector('input[type="number"]') ||
                          document.querySelector('input[inputmode="numeric"]');
            if (input) {{
                const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                s.call(input, '{expr}');
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
                input.focus();
            }}
        }}""")
        await asyncio.sleep(0.2)
        await self.browser.page.keyboard.press("Enter")
        await asyncio.sleep(0.5)
        # Also click Solve button
        await self.browser.page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button')];
            for (const btn of btns) {
                const t = (btn.textContent || '').trim().toLowerCase();
                if ((t === 'solve' || t.includes('check') || t.includes('verify') || t === 'submit') && !btn.disabled) {
                    btn.click(); return;
                }
            }
        }""")
        await asyncio.sleep(1.0)

        # Collect codes AFTER solving and find new ones via delta
        codes_after = set(await self.browser.page.evaluate("""() => {
            const text = document.body.textContent || '';
            const codes = text.match(/\\b[A-Z0-9]{6}\\b/g) || [];
            return [...new Set(codes)];
        }"""))
        new_codes = codes_after - codes_before
        # Filter out common Latin/lorem ipsum false positives
        LATIN = {'BEATAE','LABORE','DOLORE','VENIAM','NOSTRU','ALIQUA','EXERCI',
                 'TEMPOR','INCIDI','LABORI','MAGNAM','VOLUPT','SAPIEN','FUGIAT',
                 'COMMOD','EXCEPT','OFFICI','MOLLIT','PROIDE','REPUDI'}
        new_codes = {c for c in new_codes if c not in LATIN}
        if new_codes:
            code = next(iter(new_codes))
            print(f"  Math puzzle delta code: {code} (new: {new_codes})", flush=True)
            return code

        # Fallback: pattern-based extraction
        puzzle_code = await self.browser.page.evaluate("""() => {
            const text = document.body.textContent || '';
            const patterns = [
                /(?:code(?:\\s+is)?|revealed?)\\s*[:=]\\s*([A-Z0-9]{6})/i,
                /\\b([A-Z0-9]{6})\\b(?=[^A-Z0-9]*(?:submit|enter|type|input))/i
            ];
            for (const p of patterns) {
                const m = text.match(p);
                if (m) return m[1].toUpperCase();
            }
            const successEls = document.querySelectorAll('.text-green-600, .text-green-500, .bg-green-100, .bg-green-50, .text-emerald-600');
            for (const el of successEls) {
                const t = (el.textContent || '').trim();
                const m = t.match(/\\b([A-Z0-9]{6})\\b/);
                if (m) return m[1];
            }
            return null;
        }""")
        if puzzle_code:
            print(f"  Math puzzle pattern code: {puzzle_code}", flush=True)
            return puzzle_code
        return None

    async def _hide_stuck_modals(self) -> int:
        """Hide any modals that are blocking the page after failed radio attempts."""
        return await self.browser.page.evaluate("""() => {
            let hidden = 0;
            document.querySelectorAll('.fixed').forEach(el => {
                const text = el.textContent || '';
                if ((text.includes('Please Select') || text.includes('Submit & Continue') ||
                     text.includes('Submit and Continue')) && !el.querySelector('input[type="text"]')) {
                    el.style.display = 'none';
                    el.style.visibility = 'hidden';
                    el.style.pointerEvents = 'none';
                    hidden++;
                }
            });
            return hidden;
        }""")

    async def _try_mouse_drag_and_drop(self) -> bool:
        """Drag pieces into slots using Playwright mouse (fallback when JS DragEvent fails)."""
        try:
            for round_num in range(6):
                state = await self.browser.page.evaluate("""() => {
                    const text = document.body.textContent || '';
                    const match = text.match(/(\\d+)\\/(\\d+)\\s*filled/);
                    const filled = match ? parseInt(match[1]) : 0;
                    if (filled >= 6) return {filled, done: true};

                    // Find empty slots
                    const emptySlots = [...document.querySelectorAll('div')].filter(el => {
                        const t = (el.textContent || '').trim();
                        return t.match(/^Slot \\d+$/) &&
                               (el.className.includes('dashed') || (el.getAttribute('style') || '').includes('dashed'));
                    }).map(el => {
                        el.scrollIntoView({behavior: 'instant', block: 'center'});
                        const rect = el.getBoundingClientRect();
                        return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                    });

                    // Find available pieces NOT inside drop zones
                    const dropZones = [...document.querySelectorAll('[class*="border-dashed"]')];
                    const dropZoneSet = new Set(dropZones);
                    const pieces = [...document.querySelectorAll('[draggable="true"]')].filter(el => {
                        // Skip if this piece is inside a drop zone (already placed)
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
                }""")
                if state.get('done') or state.get('filled', 0) >= 6:
                    print(f"  Drag: all slots filled!", flush=True)
                    # Click Complete/Done
                    await self.browser.page.evaluate("""() => {
                        document.querySelectorAll('button').forEach(btn => {
                            const t = (btn.textContent || '').trim().toLowerCase();
                            if ((t.includes('complete') || t.includes('done') || t.includes('verify')) &&
                                !t.includes('clear') && btn.offsetParent && !btn.disabled) btn.click();
                        });
                    }""")
                    return True

                slots = state.get('emptySlots', [])
                pieces = state.get('pieces', [])
                if not slots or not pieces:
                    break

                piece = pieces[0]
                slot = slots[0]
                await self.browser.page.mouse.move(piece['x'], piece['y'])
                await self.browser.page.mouse.down()
                await asyncio.sleep(0.05)
                await self.browser.page.mouse.move(slot['x'], slot['y'], steps=15)
                await asyncio.sleep(0.05)
                await self.browser.page.mouse.up()
                print(f"  Drag: moved '{piece.get('text', '?')}' to slot (round {round_num+1})", flush=True)
                await asyncio.sleep(0.3)
            return False
        except Exception as e:
            print(f"  Drag error: {e}", flush=True)
            return False

    async def _try_scroll_to_find_nav(self, codes_to_try: list[str] | None = None, deep_scroll: bool = True) -> bool:
        """Handle 'Scroll Down to Find Navigation' - scroll to find hidden submit/nav button.

        Phase 1: Scroll through page, click SAFE_WORDS buttons at each position.
        Phase 2: Find outlier buttons (rare labels) and click them.
        Phase 3: Fast full-page scan - scroll to trigger rendering, then click ALL buttons.
        """
        try:
            print(f"  Scroll-to-find: searching...", flush=True)

            # Fill code in input if available
            if codes_to_try:
                code = codes_to_try[-1]
                await self.browser.page.evaluate(f"""() => {{
                    const inp = document.querySelector('input[placeholder*="code" i], input[type="text"]');
                    if (inp) {{
                        const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        s.call(inp, '{code}');
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}""")

            TRAP_WORDS = {'proceed', 'continue', 'next step', 'next page', 'next section',
                          'go to next', 'click here', 'go forward', 'advance'}

            # ===== Phase 0: Mouse.wheel scrolling + DOM diffing =====
            # window.scrollTo() may NOT fire wheel/scroll event listeners.
            # mouse.wheel() fires real wheel events for dynamic DOM injection.
            print(f"  Scroll-to-find: phase 0 - mouse.wheel + DOM diffing...", flush=True)
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.2)

            baseline_interactive = await self.browser.page.evaluate(
                "() => document.querySelectorAll('button, a, [role=\"button\"], [tabindex]').length"
            )
            prev_scroll_y = 0
            phase0_start = time.time()
            phase0_codes = set()  # Accumulate codes during scroll

            while time.time() - phase0_start < 15:
                await self.browser.page.mouse.wheel(0, 800)
                await asyncio.sleep(0.10)

                # Check auto-navigation (IntersectionObserver pattern)
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Phase 0: auto-navigation detected!", flush=True)
                    return True

                # Accumulate codes from current viewport (virtualized content)
                vp_codes = await self.browser.page.evaluate("""() => {
                    const text = document.body.innerText || '';
                    return (text.match(/\\b[A-Z0-9]{6}\\b/g) || []).filter((v,i,a) => a.indexOf(v) === i);
                }""")
                phase0_codes.update(vp_codes)

                # Detect bottom (scrollY stopped increasing)
                cur_y = await self.browser.page.evaluate("() => window.scrollY")
                if cur_y <= prev_scroll_y and prev_scroll_y > 100:
                    # At bottom - extra wheel events in case threshold is exactly at bottom
                    for _ in range(5):
                        await self.browser.page.mouse.wheel(0, 800)
                        await asyncio.sleep(0.15)
                        url = await self.browser.get_url()
                        if self._check_progress(url, self.current_step):
                            print(f"  Phase 0: auto-nav at bottom!", flush=True)
                            return True
                    break
                prev_scroll_y = cur_y

                # DOM diffing: new interactive elements injected?
                cur_interactive = await self.browser.page.evaluate(
                    "() => document.querySelectorAll('button, a, [role=\"button\"], [tabindex]').length"
                )
                if cur_interactive > baseline_interactive:
                    # New interactive elements! Find them near current viewport
                    new_els = await self.browser.page.evaluate("""() => {
                        const vh = window.innerHeight;
                        const sel = 'button, a, [role="button"], [tabindex], [onclick]';
                        const standard = [...document.querySelectorAll(sel)];
                        const pointers = [...document.querySelectorAll('div, span, p')].filter(el =>
                            window.getComputedStyle(el).cursor === 'pointer' && !el.querySelector('button, a')
                        );
                        return [...standard, ...pointers].filter(el => {
                            if (el.closest('.fixed') || el.disabled) return false;
                            const r = el.getBoundingClientRect();
                            return r.top >= vh * 0.3 && r.top < vh + 50 && r.width > 10 && r.height > 10;
                        }).map(el => {
                            const r = el.getBoundingClientRect();
                            return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                                    text: (el.textContent || '').trim().substring(0, 40), tag: el.tagName};
                        });
                    }""")
                    if new_els:
                        print(f"  Phase 0: {cur_interactive - baseline_interactive} new interactive els at scrollY={cur_y}", flush=True)
                        for el in new_els:
                            await self._clear_popups()
                            try:
                                await self.browser.page.mouse.click(el['x'], el['y'])
                                await asyncio.sleep(0.1)
                            except Exception:
                                pass
                            url = await self.browser.get_url()
                            if self._check_progress(url, self.current_step):
                                print(f"  Phase 0: '{el['text']}' ({el['tag']}) WORKED!", flush=True)
                                return True
                    baseline_interactive = cur_interactive

            # Try accumulated codes from Phase 0 scrolling
            LATIN = {'BEATAE','LABORE','DOLORE','VENIAM','NOSTRU','ALIQUA','EXERCI',
                     'TEMPOR','INCIDI','LABORI','MAGNAM','VOLUPT','SAPIEN','FUGIAT',
                     'COMMOD','EXCEPT','OFFICI','MOLLIT','PROIDE','REPUDI','FILLER',
                     'SCROLL','HIDDEN','BUTTON','SUBMIT','OPTION','CHOICE','REVEAL',
                     'PUZZLE','CANVAS','STROKE','SECOND','MEMORY','LOADED','BLOCKS',
                     'CHANGE','DELETE','CREATE','SEARCH','FILTER','NOTICE','STATUS',
                     'RESULT','OUTPUT','INPUTS','BEFORE','LAYOUT','RENDER','EFFECT',
                     'TOGGLE','HANDLE','CUSTOM','STRING','NUMBER','PROMPT','GLOBAL',
                     'MODULE','SHOULD','COOKIE','MOVING','FILLED','PIECES','VERIFY',
                     'DEVICE','SCREEN','MOBILE','TABLET','SELECT','PLEASE','SIMPLE',
                     'NEEDED','EXTEND','RANDOM','ACTIVE','PLAYED','ESCAPE','ALMOST',
                     'INSIDE','SOLVED','CENTER','BOTTOM','SHADOW','CURSOR','ROTATE',
                     'COLORS','IMAGES','CANCEL','RETURN','UPDATE','ALERTS','ERRORS'}
            p0_new = [c for c in phase0_codes if c not in LATIN and not c.isdigit()
                      and c not in (codes_to_try or [])
                      and not re.match(r'^\d+(?:PX|VH|VW|EM|REM|MS|FR)$', c)]
            p0_new.sort(key=lambda c: (c.isalpha(), c))
            if p0_new:
                print(f"  Phase 0 accumulated codes: {p0_new[:10]}", flush=True)
                for code in p0_new[:5]:
                    if await self._fill_and_submit(code, self.current_step):
                        return True

            # ===== Phase 0a: Scrollable containers =====
            # The scroll listener might be on a nested div, not the window
            containers = await self.browser.page.evaluate("""() => {
                const results = [];
                const els = document.querySelectorAll('div, section, main, article');
                for (const el of els) {
                    if (el.closest('.fixed')) continue;
                    const s = window.getComputedStyle(el);
                    const overflow = s.overflow + s.overflowY;
                    if (!(overflow.includes('auto') || overflow.includes('scroll'))) continue;
                    if (el.scrollHeight <= el.clientHeight + 10) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 100 || r.height < 100) continue;
                    results.push({
                        x: Math.round(r.x + r.width/2),
                        y: Math.round(r.y + r.height/2),
                        scrollable: el.scrollHeight - el.clientHeight,
                        tag: el.tagName,
                        cls: (el.className || '').substring(0, 60)
                    });
                }
                return results;
            }""")
            if containers:
                print(f"  Phase 0a: {len(containers)} scrollable containers found", flush=True)
                for cont in containers[:3]:
                    print(f"    Container: {cont['tag']}.{cont['cls'][:30]} scrollable={cont['scrollable']}px", flush=True)
                    # Move mouse to container center, then wheel-scroll it
                    await self.browser.page.mouse.move(cont['x'], cont['y'])
                    await asyncio.sleep(0.05)
                    scroll_remaining = cont['scrollable']
                    while scroll_remaining > 0:
                        await self.browser.page.mouse.wheel(0, 500)
                        scroll_remaining -= 500
                        await asyncio.sleep(0.10)
                        url = await self.browser.get_url()
                        if self._check_progress(url, self.current_step):
                            print(f"  Phase 0a: container scroll WORKED!", flush=True)
                            return True
                    # Check for new elements in container after scrolling
                    cont_els = await self.browser.page.evaluate("""() => {
                        const vh = window.innerHeight;
                        const sel = 'button, a, [role="button"], [tabindex], [onclick]';
                        return [...document.querySelectorAll(sel)].filter(el => {
                            if (el.closest('.fixed') || el.disabled) return false;
                            const r = el.getBoundingClientRect();
                            return r.top >= 0 && r.top < vh && r.width > 10 && r.height > 10;
                        }).map(el => {
                            const r = el.getBoundingClientRect();
                            return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                                    text: (el.textContent || '').trim().substring(0, 40)};
                        });
                    }""")
                    for el in cont_els[:10]:
                        if el['text'].lower().strip() in TRAP_WORDS:
                            continue
                        await self._clear_popups()
                        try:
                            await self.browser.page.mouse.click(el['x'], el['y'])
                            await asyncio.sleep(0.08)
                        except Exception:
                            pass
                        url = await self.browser.get_url()
                        if self._check_progress(url, self.current_step):
                            print(f"  Phase 0a: container element '{el['text']}' WORKED!", flush=True)
                            return True

            # After reaching bottom via mouse.wheel, scan for non-standard elements
            bottom_scan = await self.browser.page.evaluate("""() => {
                const vh = window.innerHeight;
                const sel = 'button, a, [role="button"], [tabindex], [onclick]';
                const standard = [...document.querySelectorAll(sel)];
                const pointers = [...document.querySelectorAll('div, span, p, li')].filter(el => {
                    const s = window.getComputedStyle(el);
                    return (s.cursor === 'pointer' || el.hasAttribute('tabindex'))
                        && !el.querySelector('button, a');
                });
                const reactEls = [...document.querySelectorAll('div, span')].filter(el => {
                    const pk = Object.keys(el).find(k => k.startsWith('__reactProps$'));
                    return pk && el[pk] && el[pk].onClick && !el.querySelector('button, a');
                });
                return [...new Set([...standard, ...pointers, ...reactEls])].filter(el => {
                    if (el.closest('.fixed') || el.disabled) return false;
                    const r = el.getBoundingClientRect();
                    return r.top >= 0 && r.top < vh && r.width > 10 && r.height > 10;
                }).map(el => {
                    const r = el.getBoundingClientRect();
                    return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                            text: (el.textContent || '').trim().substring(0, 40), tag: el.tagName};
                });
            }""")
            non_trap_first = sorted(bottom_scan, key=lambda e: e['text'].lower().strip() in TRAP_WORDS)
            for el in non_trap_first[:20]:
                await self._clear_popups()
                try:
                    await self.browser.page.mouse.click(el['x'], el['y'])
                    await asyncio.sleep(0.08)
                except Exception:
                    pass
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Phase 0 bottom: '{el['text']}' ({el['tag']}) WORKED!", flush=True)
                    return True

            # ===== Phase 0b: Keyboard scrolling =====
            # End key fires both keyboard AND scroll events; PageDown fires scroll events
            print(f"  Scroll-to-find: phase 0b - keyboard scrolling...", flush=True)
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.1)
            await self.browser.page.keyboard.press("End")
            await asyncio.sleep(0.5)
            url = await self.browser.get_url()
            if self._check_progress(url, self.current_step):
                print(f"  Phase 0b: End key WORKED!", flush=True)
                return True
            # Check for newly injected elements after End key
            end_els = await self.browser.page.evaluate("""() => {
                const vh = window.innerHeight;
                const sel = 'button, a, [role="button"], [tabindex], [onclick]';
                const pointers = [...document.querySelectorAll('div, span')].filter(el =>
                    window.getComputedStyle(el).cursor === 'pointer' && !el.querySelector('button, a')
                );
                return [...document.querySelectorAll(sel), ...pointers].filter(el => {
                    if (el.closest('.fixed') || el.disabled) return false;
                    const r = el.getBoundingClientRect();
                    return r.top >= 0 && r.top < vh && r.width > 10 && r.height > 10;
                }).map(el => {
                    const r = el.getBoundingClientRect();
                    return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                            text: (el.textContent || '').trim().substring(0, 40), tag: el.tagName};
                });
            }""")
            for el in sorted(end_els, key=lambda e: e['text'].lower().strip() in TRAP_WORDS)[:15]:
                await self._clear_popups()
                try:
                    await self.browser.page.mouse.click(el['x'], el['y'])
                    await asyncio.sleep(0.08)
                except Exception:
                    pass
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Phase 0b: '{el['text']}' after End key WORKED!", flush=True)
                    return True

            # PageDown through entire page
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.1)
            for _ in range(80):
                await self.browser.page.keyboard.press("PageDown")
                await asyncio.sleep(0.06)
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Phase 0b: PageDown WORKED!", flush=True)
                    return True

            # ===== Phase 0c: Synthetic event dispatch =====
            # Dispatch scroll AND wheel events on all targets after programmatic scroll
            print(f"  Scroll-to-find: phase 0c - synthetic events...", flush=True)
            await self.browser.page.evaluate("""() => {
                window.scrollTo(0, document.body.scrollHeight);
                for (const target of [window, document, document.documentElement, document.body]) {
                    target.dispatchEvent(new Event('scroll', {bubbles: true}));
                    target.dispatchEvent(new WheelEvent('wheel', {deltaY: 500, bubbles: true}));
                }
            }""")
            await asyncio.sleep(0.3)
            url = await self.browser.get_url()
            if self._check_progress(url, self.current_step):
                print(f"  Phase 0c: synthetic events WORKED!", flush=True)
                return True
            # Check for elements injected by synthetic events
            post_synth = await self.browser.page.evaluate("""() => {
                const vh = window.innerHeight;
                const sel = 'button, a, [role="button"], [tabindex], [onclick]';
                const pointers = [...document.querySelectorAll('div, span')].filter(el =>
                    window.getComputedStyle(el).cursor === 'pointer' && !el.querySelector('button, a')
                );
                return [...document.querySelectorAll(sel), ...pointers].filter(el => {
                    if (el.closest('.fixed') || el.disabled) return false;
                    const r = el.getBoundingClientRect();
                    return r.top >= 0 && r.top < vh && r.width > 10 && r.height > 10;
                }).map(el => {
                    const r = el.getBoundingClientRect();
                    return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                            text: (el.textContent || '').trim().substring(0, 40), tag: el.tagName};
                });
            }""")
            for el in sorted(post_synth, key=lambda e: e['text'].lower().strip() in TRAP_WORDS)[:15]:
                await self._clear_popups()
                try:
                    await self.browser.page.mouse.click(el['x'], el['y'])
                    await asyncio.sleep(0.08)
                except Exception:
                    pass
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Phase 0c: '{el['text']}' WORKED!", flush=True)
                    return True

            # ===== Existing fallback phases (scrollTo-based) =====
            total_h = await self.browser.page.evaluate("() => document.body.scrollHeight")
            step_px = 800

            # Phase 1: Scroll and click SAFE_WORDS buttons (fast)
            for pos in range(0, total_h + step_px, step_px):
                await self.browser.page.evaluate(f"window.scrollTo(0, {pos})")
                await asyncio.sleep(0.05)

                btn_results = await self.browser.page.evaluate("""() => {
                    const SAFE_WORDS = ['next', 'submit', 'go', '→', 'navigate', 'enter'];
                    const btns = [...document.querySelectorAll('button, a')];
                    const results = [];
                    for (const btn of btns) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.top < -10 || rect.top > window.innerHeight + 10) continue;
                        if (!btn.offsetParent || btn.disabled) continue;
                        if (btn.closest('.fixed')) continue;
                        const t = (btn.textContent || '').trim();
                        const tl = t.toLowerCase();
                        if (tl.length > 40 || tl.length === 0) continue;
                        if (t === '×' || t === 'X' || t === '✕') continue;
                        if (SAFE_WORDS.some(w => tl === w || (tl.includes(w) && tl.length < 15))) {
                            results.push({text: t, idx: btns.indexOf(btn)});
                        }
                    }
                    return results;
                }""")

                for btn in btn_results:
                    await self._clear_popups()
                    await self.browser.page.evaluate(f"(idx) => [...document.querySelectorAll('button, a')][idx]?.click()", btn['idx'])
                    await asyncio.sleep(0.1)
                    url = await self.browser.get_url()
                    if self._check_progress(url, self.current_step):
                        print(f"  Scroll-to-find: button '{btn['text']}' at scroll {pos}px WORKED!", flush=True)
                        return True

            # Phase 2: Find outlier buttons (rare labels among many similar ones)
            outlier_result = await self.browser.page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')].filter(b => {
                    if (!b.offsetParent || b.disabled || b.closest('.fixed')) return false;
                    const t = b.textContent.trim();
                    return t.length > 0 && t.length < 40 && t !== '×' && t !== 'X' && t !== '✕';
                });
                if (btns.length < 5) return null;
                const freq = {};
                btns.forEach(b => {
                    const label = b.textContent.trim().toLowerCase();
                    freq[label] = (freq[label] || 0) + 1;
                });
                const outliers = btns.filter(b => {
                    const label = b.textContent.trim().toLowerCase();
                    return freq[label] <= 2;
                });
                return outliers.map((b, i) => ({
                    text: b.textContent.trim(),
                    idx: [...document.querySelectorAll('button')].indexOf(b)
                }));
            }""")

            if outlier_result:
                print(f"  Scroll-to-find: found {len(outlier_result)} outlier buttons", flush=True)
                for btn in outlier_result:
                    await self._clear_popups()
                    await self.browser.page.evaluate(f"""(idx) => {{
                        const btn = document.querySelectorAll('button')[idx];
                        if (btn) {{ btn.scrollIntoView({{behavior: 'instant', block: 'center'}}); btn.click(); }}
                    }}""", btn['idx'])
                    await asyncio.sleep(0.12)
                    url = await self.browser.get_url()
                    if self._check_progress(url, self.current_step):
                        print(f"  Scroll-to-find: outlier '{btn['text']}' WORKED!", flush=True)
                        return True

            if not deep_scroll:
                await self.browser.page.evaluate("window.scrollTo(0, 0)")
                return False

            # Phase 3: Fast full-page button scan
            # Step A: Thorough scroll with mouse.wheel to trigger rendering + fire events
            total_h = await self.browser.page.evaluate("() => document.body.scrollHeight")
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.05)
            for pos in range(0, total_h + 1000, 1000):
                await self.browser.page.mouse.wheel(0, 1000)
                await asyncio.sleep(0.05)
            # Re-check height (virtualized content may have expanded)
            new_h = await self.browser.page.evaluate("() => document.body.scrollHeight")
            if new_h > total_h + 500:
                for pos in range(total_h, new_h + 1000, 1000):
                    await self.browser.page.mouse.wheel(0, 1000)
                    await asyncio.sleep(0.05)
            # Dispatch synthetic events at bottom for good measure
            await self.browser.page.evaluate("""() => {
                for (const target of [window, document, document.documentElement, document.body]) {
                    target.dispatchEvent(new Event('scroll', {bubbles: true}));
                    target.dispatchEvent(new WheelEvent('wheel', {deltaY: 500, bubbles: true}));
                }
            }""")
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.1)

            # Step B: Get ALL buttons from entire page (they should all be rendered now)
            all_btns = await self.browser.page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button, a')].filter(el => {
                    if (el.disabled || el.closest('.fixed')) return false;
                    const t = (el.textContent || '').trim();
                    return t.length > 0 && t.length < 40 && t !== '×' && t !== 'X' && t !== '✕';
                });
                return btns.map((b, i) => ({text: b.textContent.trim(), idx: i}));
            }""")
            print(f"  Scroll-to-find: phase 3 clicking {len(all_btns)} buttons...", flush=True)

            # Step C: Click each button with popup clearing (in batches of 5 for speed)
            batch_size = 5
            for start in range(0, len(all_btns), batch_size):
                end = min(start + batch_size, len(all_btns))
                await self.browser.page.evaluate(f"""() => {{
                    const start = {start}, end = {end};
                    const clearP = () => {{
                        document.querySelectorAll('.fixed').forEach(el => {{
                            const text = el.textContent || '';
                            if (text.includes('Wrong Button') || text.includes('Try Again') ||
                                text.includes('another way') || text.includes('fake') ||
                                text.includes('won a prize') || text.includes('popup message') ||
                                text.includes('Click the button to dismiss')) {{
                                const btn = el.querySelector('button');
                                if (btn) btn.click();
                                el.style.display = 'none';
                                el.style.pointerEvents = 'none';
                            }}
                        }});
                    }};
                    const allBtns = [...document.querySelectorAll('button, a')].filter(el => {{
                        if (el.disabled || el.closest('.fixed')) return false;
                        const t = (el.textContent || '').trim();
                        return t.length > 0 && t.length < 40 && t !== '×' && t !== 'X' && t !== '✕';
                    }});
                    for (let i = start; i < Math.min(end, allBtns.length); i++) {{
                        clearP();
                        allBtns[i].scrollIntoView({{behavior: 'instant', block: 'center'}});
                        allBtns[i].click();
                    }}
                }}""")
                await asyncio.sleep(0.1)
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Scroll-to-find: phase 3 batch {start}-{end} WORKED!", flush=True)
                    return True

            # Step D: Extract codes from full-page scroll (codes may be hidden in filler sections)
            html_after = await self.browser.get_html()
            scroll_codes = extract_hidden_codes(html_after)
            new_scroll_codes = [c for c in scroll_codes if c not in (codes_to_try or [])]
            if new_scroll_codes:
                print(f"  Scroll-to-find: found new codes during scroll: {new_scroll_codes}", flush=True)
                for code in new_scroll_codes[:3]:
                    if await self._fill_and_submit(code, self.current_step):
                        return True

            # Step E: Try pressing Enter with each code
            if codes_to_try:
                for code in codes_to_try[:3]:
                    await self.browser.page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.05)
                    inp = self.browser.page.locator('input[placeholder*="code" i], input[type="text"]').first
                    try:
                        await inp.click(timeout=1000)
                        await inp.fill(code)
                        await self.browser.page.keyboard.press("Enter")
                        await asyncio.sleep(0.3)
                        url = await self.browser.get_url()
                        if self._check_progress(url, self.current_step):
                            print(f"  Scroll-to-find: Enter with code {code} WORKED!", flush=True)
                            return True
                    except Exception:
                        pass

            # Phase 4: Playwright-native clicks + code accumulation during scroll
            # Extract codes at each scroll position (virtualized content only exists in viewport)
            print(f"  Scroll-to-find: phase 4 - Playwright native clicks...", flush=True)
            total_h = await self.browser.page.evaluate("() => document.body.scrollHeight")
            clicked = 0
            all_labels = []
            accumulated_codes = set()
            phase4_start = time.time()
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.1)
            scroll_pos = 0
            while scroll_pos < total_h + 800:
                if time.time() - phase4_start > 25:
                    print(f"  Scroll-to-find: phase 4 time limit ({clicked} clicks)", flush=True)
                    break
                # Use mouse.wheel for real event firing
                await self.browser.page.mouse.wheel(0, 800)
                scroll_pos += 800
                await asyncio.sleep(0.1)
                # Extract codes from current viewport text (catches virtualized content)
                viewport_codes = await self.browser.page.evaluate("""() => {
                    const text = document.body.innerText || '';
                    const codes = text.match(/\\b[A-Z0-9]{6}\\b/g) || [];
                    return [...new Set(codes)];
                }""")
                accumulated_codes.update(viewport_codes)
                # Get interactive elements visible in current viewport
                visible_btns = await self.browser.page.evaluate("""() => {
                    const sel = 'button, a, [role="button"], [class*="cursor-pointer"], [onclick]';
                    const els = [...document.querySelectorAll(sel)].filter(el => {
                        if (el.closest('.fixed')) return false;
                        if (el.disabled) return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.top < -10 || rect.top > window.innerHeight + 10) return false;
                        if (rect.width < 10 || rect.height < 10) return false;
                        const t = (el.textContent || '').trim();
                        if (t.length === 0 || t.length > 60) return false;
                        if (t === '×' || t === 'X' || t === '✕') return false;
                        return true;
                    });
                    return els.map(el => {
                        const rect = el.getBoundingClientRect();
                        return {
                            text: (el.textContent || '').trim().substring(0, 40),
                            x: Math.round(rect.x + rect.width / 2),
                            y: Math.round(rect.y + rect.height / 2)
                        };
                    });
                }""")
                for btn in visible_btns:
                    all_labels.append(btn['text'])
                    await self._clear_popups()
                    try:
                        await self.browser.page.mouse.click(btn['x'], btn['y'])
                        clicked += 1
                        await asyncio.sleep(0.05)
                    except Exception:
                        pass
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Scroll-to-find: phase 4 WORKED after {clicked} clicks (scroll {scroll_pos}px)!", flush=True)
                    return True
            # Debug info
            from collections import Counter
            label_counts = Counter(all_labels)
            unique_labels = [l for l, c in label_counts.items() if c <= 2]
            print(f"  Phase 4: {clicked} clicks, {len(label_counts)} unique labels. Rare: {unique_labels[:10]}", flush=True)

            # Try ALL accumulated codes from scrolling (some only exist when their section is in viewport)
            LATIN = {'BEATAE','LABORE','DOLORE','VENIAM','NOSTRU','ALIQUA','EXERCI',
                     'TEMPOR','INCIDI','LABORI','MAGNAM','VOLUPT','SAPIEN','FUGIAT',
                     'COMMOD','EXCEPT','OFFICI','MOLLIT','PROIDE','REPUDI','FILLER',
                     'SCROLL','HIDDEN','BUTTON','SUBMIT','OPTION','CHOICE','REVEAL',
                     'PUZZLE','CANVAS','STROKE','SECOND','MEMORY','LOADED','BLOCKS',
                     'CHANGE','DELETE','CREATE','SEARCH','FILTER','NOTICE','STATUS',
                     'RESULT','OUTPUT','INPUTS','BEFORE','LAYOUT','RENDER','EFFECT',
                     'TOGGLE','HANDLE','CUSTOM','STRING','NUMBER','PROMPT','GLOBAL',
                     'MODULE','SHOULD','COOKIE','MOVING','FILLED','PIECES','VERIFY',
                     'DEVICE','SCREEN','MOBILE','TABLET','SELECT','PLEASE','SIMPLE',
                     'NEEDED','EXTEND','RANDOM','ACTIVE','PLAYED','ESCAPE','ALMOST',
                     'INSIDE','SOLVED','CENTER','BOTTOM','SHADOW','CURSOR','ROTATE',
                     'COLORS','IMAGES','CANCEL','RETURN','UPDATE','ALERTS','ERRORS'}
            new_accumulated = [c for c in accumulated_codes
                             if c not in LATIN and not c.isdigit()
                             and c not in (codes_to_try or [])
                             and not re.match(r'^\d+(?:PX|VH|VW|EM|REM|MS|FR)$', c)]
            # Sort: codes with digits first (more likely real), then all-letter codes
            new_accumulated.sort(key=lambda c: (c.isalpha(), c))
            if new_accumulated:
                print(f"  Phase 4 accumulated codes: {new_accumulated}", flush=True)
                for code in new_accumulated[:5]:
                    if await self._fill_and_submit(code, self.current_step):
                        return True

            # Phase 5: Try hidden navigation - forms, links, direct URL manipulation
            # Some scroll challenges have a hidden link or form at the very bottom
            await self.browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.3)
            # Look for any navigation links with href patterns
            nav_found = await self.browser.page.evaluate(f"""() => {{
                // Check for hidden links with step-related hrefs
                const links = [...document.querySelectorAll('a[href]')];
                for (const a of links) {{
                    const href = a.getAttribute('href') || '';
                    if (href.includes('step') || href.includes('/{self.current_step + 1}') ||
                        href.includes('challenge') || href.match(/\\/\\d+$/)) {{
                        a.click();
                        return 'link: ' + href;
                    }}
                }}
                // Check for forms
                const forms = document.querySelectorAll('form');
                for (const form of forms) {{
                    if (form.action && form.action !== window.location.href) {{
                        form.submit();
                        return 'form: ' + form.action;
                    }}
                }}
                return null;
            }}""")
            if nav_found:
                print(f"  Scroll-to-find: phase 5 found {nav_found}", flush=True)
                await asyncio.sleep(0.5)
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    return True

            # Phase 6: Find non-standard React onClick elements (divs, spans, etc.)
            # Only scan non-button/non-link elements since Phase 4 covered those
            print(f"  Scroll-to-find: phase 6 - React onClick scan...", flush=True)
            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.05)
            for scroll_pos in range(0, total_h + 1200, 1200):
                if time.time() - phase4_start > 35:
                    break
                await self.browser.page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.08)
                react_btns = await self.browser.page.evaluate("""() => {
                    const results = [];
                    // Only check div/span/p/li - buttons and links already covered
                    const els = document.querySelectorAll('div, span, p, li, td, section');
                    for (const el of els) {
                        if (el.closest('.fixed')) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.top < -10 || rect.top > window.innerHeight + 10) continue;
                        if (rect.width < 20 || rect.height < 15) continue;
                        const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps$'));
                        if (propsKey && el[propsKey] && el[propsKey].onClick) {
                            const t = (el.textContent || '').trim();
                            if (t === '×' || t === 'X' || t.length > 60) continue;
                            // Skip if this element contains a button/link child (already clicked)
                            if (el.querySelector('button, a')) continue;
                            results.push({
                                x: Math.round(rect.x + rect.width / 2),
                                y: Math.round(rect.y + rect.height / 2)
                            });
                        }
                    }
                    return results;
                }""")
                for btn in react_btns:
                    await self._clear_popups()
                    try:
                        await self.browser.page.mouse.click(btn['x'], btn['y'])
                    except Exception:
                        pass
                url = await self.browser.get_url()
                if self._check_progress(url, self.current_step):
                    print(f"  Scroll-to-find: phase 6 (React onClick) WORKED at scroll {scroll_pos}!", flush=True)
                    return True

            # Phase 7: Use Playwright locators (auto scroll-into-view, handles virtual lists)
            print(f"  Scroll-to-find: phase 7 - Playwright locator clicks...", flush=True)
            try:
                # Try clicking EVERY button on the page using Playwright locators
                all_buttons = self.browser.page.locator('button:not(:has-text("×")):not(:has-text("✕"))')
                count = await all_buttons.count()
                print(f"  Phase 7: {count} buttons found via locator", flush=True)
                phase7_clicked = 0
                for i in range(count):
                    if time.time() - phase4_start > 50:
                        break
                    try:
                        btn = all_buttons.nth(i)
                        # Check if it's inside a .fixed modal (skip those)
                        is_fixed = await btn.evaluate("el => !!el.closest('.fixed')")
                        if is_fixed:
                            continue
                        text = (await btn.text_content() or '').strip()
                        if len(text) > 60 or text in ('×', 'X', '✕'):
                            continue
                        await self._clear_popups()
                        await btn.click(timeout=500, force=True)
                        phase7_clicked += 1
                        url = await self.browser.get_url()
                        if self._check_progress(url, self.current_step):
                            print(f"  Phase 7: button '{text}' WORKED! ({phase7_clicked} clicks)", flush=True)
                            return True
                    except Exception:
                        pass
                print(f"  Phase 7: clicked {phase7_clicked}/{count}, none worked", flush=True)
            except Exception as e:
                print(f"  Phase 7 error: {e}", flush=True)

            # DEBUG: dump page state when all phases fail
            debug_info = await self.browser.page.evaluate("""() => {
                const vh = window.innerHeight;
                const scrollH = document.body.scrollHeight;
                const allBtns = document.querySelectorAll('button').length;
                const allLinks = document.querySelectorAll('a').length;
                const allInputs = document.querySelectorAll('input').length;
                const allForms = document.querySelectorAll('form').length;
                const iframes = document.querySelectorAll('iframe').length;
                const canvases = document.querySelectorAll('canvas').length;
                // Check for scrollable containers
                const scrollable = [...document.querySelectorAll('div, section')].filter(el => {
                    const s = window.getComputedStyle(el);
                    return (s.overflow + s.overflowY).match(/auto|scroll/) && el.scrollHeight > el.clientHeight + 10;
                }).length;
                // Get visible text at bottom
                window.scrollTo(0, scrollH);
                const bottomText = document.body.innerText.substring(document.body.innerText.length - 500);
                // Check data attributes
                const dataEls = [...document.querySelectorAll('[data-code], [data-value], [data-step], [data-nav]')].map(
                    el => ({tag: el.tagName, attrs: [...el.attributes].map(a => a.name + '=' + a.value.substring(0, 30)).join(', ')})
                );
                return {scrollH, allBtns, allLinks, allInputs, allForms, iframes, canvases,
                        scrollable, bottomText: bottomText.substring(0, 300), dataEls: dataEls.slice(0, 5)};
            }""")
            print(f"  ALL PHASES FAILED. Debug: scrollH={debug_info['scrollH']}, btns={debug_info['allBtns']}, "
                  f"links={debug_info['allLinks']}, inputs={debug_info['allInputs']}, forms={debug_info['allForms']}, "
                  f"iframes={debug_info['iframes']}, canvases={debug_info['canvases']}, "
                  f"scrollableContainers={debug_info['scrollable']}", flush=True)
            if debug_info['dataEls']:
                print(f"  Data-attr elements: {debug_info['dataEls']}", flush=True)
            print(f"  Bottom text: {debug_info['bottomText'][:200]}", flush=True)

            await self.browser.page.evaluate("window.scrollTo(0, 0)")
            return False
        except Exception as e:
            print(f"  Scroll-to-find error: {e}", flush=True)
            return False

    async def _try_audio_challenge(self) -> bool:
        """Handle Audio Challenge - force-end speech synthesis in headless Chromium."""
        try:
            # Reset capture state
            await self.browser.page.evaluate("""() => {
                window.__capturedSpeechTexts = window.__capturedSpeechTexts || [];
                window.__capturedSpeechUtterance = window.__capturedSpeechUtterance || null;
                window.__speechDone = false;
            }""")

            # Click Play Audio button (but NOT "Playing...")
            play_result = await self.browser.page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                for (const btn of btns) {
                    const text = (btn.textContent || '').trim().toLowerCase();
                    if (text.includes('play') && !text.includes('playing') && btn.offsetParent && !btn.disabled) {
                        btn.click(); return 'clicked';
                    }
                }
                for (const btn of btns) {
                    const text = (btn.textContent || '').trim().toLowerCase();
                    if (text.includes('playing')) return 'already_playing';
                }
                return 'not_found';
            }""")
            if play_result == 'not_found':
                return False
            print(f"  Audio: {play_result}", flush=True)

            # Wait for speech synthesis to start, then force-end it
            await asyncio.sleep(3.0)

            # Force-end speech and dispatch 'end' event on captured utterance
            await self.browser.page.evaluate("""() => {
                if (window.speechSynthesis) window.speechSynthesis.cancel();
                const utt = window.__capturedSpeechUtterance;
                if (utt) {
                    try { utt.dispatchEvent(new SpeechSynthesisEvent('end', {utterance: utt})); } catch(e) {
                        try { utt.dispatchEvent(new Event('end')); } catch(e2) {}
                    }
                    if (utt.onend) { try { utt.onend(new Event('end')); } catch(e) {} }
                }
                // Force-end Audio elements
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
            }""")
            print(f"  Audio: force-ended speech synthesis", flush=True)
            await asyncio.sleep(1.0)

            # Click Complete/Done button
            for _ in range(6):
                clicked = await self.browser.page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if ((text.includes('complete') || text.includes('done') || text.includes('finish')) &&
                            !text.includes('playing') && btn.offsetParent && !btn.disabled) {
                            btn.click(); return true;
                        }
                    }
                    return false;
                }""")
                if clicked:
                    print(f"  Audio: clicked Complete", flush=True)
                    break
                await asyncio.sleep(0.5)
            else:
                # Last resort: click "Playing..." button (might toggle to Complete)
                await self.browser.page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const btn of btns) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        if (text.includes('playing') && btn.offsetParent) { btn.click(); return; }
                    }
                }""")

            await asyncio.sleep(1.0)
            return True
        except Exception as e:
            print(f"  Audio error: {e}", flush=True)
            return False

    async def _try_canvas_challenge(self) -> bool:
        """Handle Canvas Challenge - draw shapes or strokes on a canvas."""
        try:
            canvas_info = await self.browser.page.evaluate("""() => {
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
            }""")
            if not canvas_info.get('found'):
                return False

            cx, cy, cw, ch = canvas_info['x'], canvas_info['y'], canvas_info['w'], canvas_info['h']
            shape = canvas_info.get('shape', 'strokes')
            print(f"  Canvas: drawing {shape}", flush=True)

            if shape == 'square':
                margin = 0.2
                corners = [(cx+cw*margin, cy+ch*margin), (cx+cw*(1-margin), cy+ch*margin),
                           (cx+cw*(1-margin), cy+ch*(1-margin)), (cx+cw*margin, cy+ch*(1-margin)),
                           (cx+cw*margin, cy+ch*margin)]
                await self.browser.page.mouse.move(corners[0][0], corners[0][1])
                await self.browser.page.mouse.down()
                for corner in corners[1:]:
                    await self.browser.page.mouse.move(corner[0], corner[1], steps=15)
                    await asyncio.sleep(0.05)
                await self.browser.page.mouse.up()
            elif shape == 'circle':
                import math
                center_x, center_y = cx + cw/2, cy + ch/2
                radius = min(cw, ch) * 0.35
                start_x = center_x + radius
                await self.browser.page.mouse.move(start_x, center_y)
                await self.browser.page.mouse.down()
                for i in range(1, 37):
                    angle = (2 * math.pi * i) / 36
                    await self.browser.page.mouse.move(center_x + radius*math.cos(angle),
                                                        center_y + radius*math.sin(angle), steps=3)
                await self.browser.page.mouse.up()
            elif shape == 'triangle':
                margin = 0.2
                corners = [(cx+cw/2, cy+ch*margin), (cx+cw*(1-margin), cy+ch*(1-margin)),
                           (cx+cw*margin, cy+ch*(1-margin)), (cx+cw/2, cy+ch*margin)]
                await self.browser.page.mouse.move(corners[0][0], corners[0][1])
                await self.browser.page.mouse.down()
                for corner in corners[1:]:
                    await self.browser.page.mouse.move(corner[0], corner[1], steps=15)
                    await asyncio.sleep(0.05)
                await self.browser.page.mouse.up()
            else:
                # Default: draw 4 varied strokes
                for i in range(4):
                    sx = cx + cw*0.2 + (i*cw*0.15)
                    sy = cy + ch*0.3 + (i*ch*0.1)
                    ex = cx + cw*0.5 + (i*cw*0.1)
                    ey = cy + ch*0.7 - (i*ch*0.05)
                    await self.browser.page.mouse.move(sx, sy)
                    await self.browser.page.mouse.down()
                    await self.browser.page.mouse.move(ex, ey, steps=10)
                    await self.browser.page.mouse.up()
                    await asyncio.sleep(0.3)

            # Click Complete/Done button
            await asyncio.sleep(0.5)
            await self.browser.page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                for (const btn of btns) {
                    const t = (btn.textContent || '').trim().toLowerCase();
                    if ((t.includes('complete') || t.includes('done') || t.includes('check') ||
                         t.includes('verify') || t.includes('reveal')) &&
                        !t.includes('clear') && btn.offsetParent && !btn.disabled) {
                        btn.click(); return;
                    }
                }
            }""")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"  Canvas error: {e}", flush=True)
            return False

    async def _try_split_parts(self) -> bool:
        """Handle Split Parts Challenge - click scattered Part N elements."""
        try:
            for click_round in range(10):
                result = await self.browser.page.evaluate("""() => {
                    const text = document.body.textContent || '';
                    const foundMatch = text.match(/(\\d+)\\/(\\d+)\\s*found/);
                    const found = foundMatch ? parseInt(foundMatch[1]) : 0;
                    const total = foundMatch ? parseInt(foundMatch[2]) : 4;
                    if (found >= total) return {found, total, clicked: 0, done: true};

                    let clicked = 0;
                    document.querySelectorAll('div').forEach(el => {
                        const style = getComputedStyle(el);
                        const cls = el.className || '';
                        const elText = (el.textContent || '').trim();
                        if (!(style.position === 'absolute' || cls.includes('absolute'))) return;
                        if (!elText.match(/Part\\s*\\d/i)) return;
                        if (el.offsetWidth < 10) return;
                        const bg = style.backgroundColor;
                        if (bg.includes('134') || bg.includes('green') || cls.includes('bg-green')) return;
                        el.scrollIntoView({behavior: 'instant', block: 'center'});
                        el.click();
                        clicked++;
                    });
                    return {found, total, clicked, done: false};
                }""")
                if result.get('done'):
                    print(f"  Split parts: all collected!", flush=True)
                    break
                if result.get('clicked', 0) == 0:
                    await self.browser.page.evaluate("() => window.scrollBy(0, 400)")
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"  Split parts error: {e}", flush=True)
            return False

    async def _try_rotating_code(self) -> bool:
        """Handle Rotating Code Challenge - click Capture N times."""
        try:
            for _ in range(15):
                state = await self.browser.page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')];
                    let done = 0, required = 3;
                    for (const btn of btns) {
                        const t = (btn.textContent || '').trim();
                        const m = t.match(/[Cc]apture.*?(\\d+)\\/(\\d+)/);
                        if (m) { done = parseInt(m[1]); required = parseInt(m[2]); break; }
                    }
                    return {done, required, complete: done >= required};
                }""")
                if state.get('complete'):
                    return True
                clicked = await self.browser.page.evaluate("""() => {
                    for (const btn of document.querySelectorAll('button')) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t.includes('capture') && btn.offsetParent && !btn.disabled) {
                            btn.click(); return true;
                        }
                    }
                    return false;
                }""")
                if not clicked:
                    break
                await asyncio.sleep(1.0)
            return True
        except Exception as e:
            print(f"  Rotating code error: {e}", flush=True)
            return False

    async def _try_multi_tab(self) -> bool:
        """Handle Multi-Tab Challenge - click through all tabs to collect code parts."""
        try:
            for _ in range(3):
                result = await self.browser.page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')];
                    const tabBtns = btns.filter(b => {
                        const t = (b.textContent || '').trim().toLowerCase();
                        return (t.includes('tab') || t.match(/^\\d+$/)) && b.offsetParent;
                    });
                    for (const btn of tabBtns) btn.click();
                    return tabBtns.length;
                }""")
                if result > 0:
                    print(f"  Multi-tab: clicked {result} tabs", flush=True)
                await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"  Multi-tab error: {e}", flush=True)
            return False

    async def _try_sequence_challenge(self) -> bool:
        """Handle Sequence Challenge - perform 4 actions: click, hover, type, scroll."""
        try:
            # Action 1: Click "Click Me" button
            await self.browser.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const t = (btn.textContent || '').trim().toLowerCase();
                    if (t.includes('click me') && btn.offsetParent && !btn.disabled) { btn.click(); return; }
                }
            }""")
            await asyncio.sleep(0.3)

            # Action 2: Hover over the hover area
            hover_info = await self.browser.page.evaluate("""() => {
                const els = [...document.querySelectorAll('div, span, p')];
                let best = null;
                for (const el of els) {
                    const t = (el.textContent || '').trim().toLowerCase();
                    if ((t === 'hover over this area' || t.includes('hover over')) && el.offsetParent) {
                        if (!best || el.textContent.length < best.textContent.length) best = el;
                    }
                }
                if (best) {
                    best.scrollIntoView({behavior: 'instant', block: 'center'});
                    const rect = best.getBoundingClientRect();
                    return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                }
                return null;
            }""")
            if hover_info:
                await self.browser.page.mouse.move(hover_info['x'], hover_info['y'])
                await asyncio.sleep(0.5)
                await self.browser.page.evaluate(f"""() => {{
                    const el = document.elementFromPoint({hover_info['x']}, {hover_info['y']});
                    if (el) {{
                        el.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                        el.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                    }}
                }}""")
                await asyncio.sleep(0.8)

            # Action 3: Type text in non-code input
            await self.browser.page.evaluate("""() => {
                const inputs = [...document.querySelectorAll('input[type="text"], input:not([type]), textarea')];
                const inp = inputs.find(i => {
                    const ph = (i.placeholder || '').toLowerCase();
                    return !ph.includes('code') && i.offsetParent && i.type !== 'number' && i.type !== 'hidden';
                });
                if (inp) {
                    inp.scrollIntoView({behavior: 'instant', block: 'center'});
                    inp.focus();
                    const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    s.call(inp, 'hello world');
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""")
            await asyncio.sleep(0.3)

            # Action 4: Scroll inside scroll box
            scroll_info = await self.browser.page.evaluate("""() => {
                for (const el of document.querySelectorAll('div, textarea')) {
                    const style = getComputedStyle(el);
                    const isScrollable = style.overflow === 'auto' || style.overflow === 'scroll' ||
                        style.overflowY === 'auto' || style.overflowY === 'scroll';
                    if (isScrollable && el.scrollHeight > el.clientHeight + 10 &&
                        el.offsetParent && el.clientHeight < 400 && el.clientHeight > 30) {
                        el.scrollIntoView({behavior: 'instant', block: 'center'});
                        el.scrollTop = el.scrollHeight;
                        const rect = el.getBoundingClientRect();
                        return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                    }
                }
                return null;
            }""")
            if scroll_info:
                await self.browser.page.mouse.move(scroll_info['x'], scroll_info['y'])
                await self.browser.page.mouse.wheel(0, 300)
            await asyncio.sleep(0.3)

            # Click Complete button
            await self.browser.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const t = (btn.textContent || '').trim().toLowerCase();
                    if (t.includes('complete') && btn.offsetParent && !btn.disabled) { btn.click(); return; }
                }
            }""")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"  Sequence error: {e}", flush=True)
            return False

    async def _try_video_challenge(self) -> bool:
        """Handle Video Frames Challenge - navigate to target frame."""
        try:
            state = await self.browser.page.evaluate("""() => {
                const text = document.body.textContent || '';
                const targetMatch = text.match(/(?:frame|Frame)\\s+(\\d+)/g);
                let targetFrame = null;
                if (targetMatch) {
                    for (const m of targetMatch) {
                        const num = parseInt(m.match(/\\d+/)[0]);
                        if (num > 0 && num < 100) { targetFrame = num; break; }
                    }
                }
                const currentMatch = text.match(/Frame\\s+(\\d+)\\/(\\d+)/);
                const currentFrame = currentMatch ? parseInt(currentMatch[1]) : 0;
                return {targetFrame, currentFrame};
            }""")
            target = state.get('targetFrame')
            if target is None:
                return False
            print(f"  Video: navigating to frame {target} (at {state.get('currentFrame')})", flush=True)

            # Perform required seek operations
            for _ in range(5):
                await self.browser.page.evaluate("""() => {
                    for (const btn of document.querySelectorAll('button')) {
                        if (btn.textContent.trim() === '+1' && btn.offsetParent) { btn.click(); return; }
                    }
                }""")
                await asyncio.sleep(0.3)

            # Navigate to target frame using +10/-10 and +1/-1
            for _ in range(20):
                current = await self.browser.page.evaluate("""() => {
                    const m = (document.body.textContent || '').match(/Frame\\s+(\\d+)\\//);
                    return m ? parseInt(m[1]) : 0;
                }""")
                if current == target:
                    break
                diff = target - current
                btn_text = '+10' if diff >= 10 else '-10' if diff <= -10 else '+1' if diff > 0 else '-1'
                await self.browser.page.evaluate(f"""() => {{
                    for (const btn of document.querySelectorAll('button')) {{
                        if (btn.textContent.trim() === '{btn_text}' && btn.offsetParent) {{ btn.click(); return; }}
                    }}
                }}""")
                await asyncio.sleep(0.2)

            # Click Complete/Reveal button
            await asyncio.sleep(0.5)
            await self.browser.page.evaluate("""() => {
                for (const btn of document.querySelectorAll('button')) {
                    const t = (btn.textContent || '').trim().toLowerCase();
                    if ((t.includes('complete') || t.includes('done') || t.includes('reveal')) &&
                        btn.offsetParent && !btn.disabled) { btn.click(); return; }
                }
            }""")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(f"  Video error: {e}", flush=True)
            return False

    # ── Action execution ────────────────────────────────────────────────

    async def _execute_action(self, action) -> str:
        """Execute the agent's suggested action. Returns description string."""
        atype = action.action_type
        target = action.target_selector
        value = action.value

        try:
            if atype == ActionType.CLICK or atype == ActionType.CLICK_REVEAL:
                if target:
                    try:
                        await self.browser.page.click(target, timeout=2000)
                        return f"clicked {target}"
                    except Exception:
                        # Try JS click
                        await self.browser.page.evaluate(f"""() => {{
                            const el = document.querySelector('{target}');
                            if (el) el.click();
                        }}""")
                        return f"js-clicked {target}"
                return "click (no target)"

            elif atype == ActionType.TYPE:
                if target and value:
                    try:
                        await self.browser.page.fill(target, value)
                    except Exception:
                        await self.browser.page.evaluate(f"""(val) => {{
                            const el = document.querySelector('{target}');
                            if (el) {{
                                const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                                s.call(el, val);
                                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                            }}
                        }}""", value)
                    return f"typed '{value}' in {target}"
                return "type (no target/value)"

            elif atype == ActionType.SCROLL:
                await self.browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.3)
                return "scrolled to bottom"

            elif atype == ActionType.SCROLL_UP:
                await self.browser.page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.3)
                return "scrolled to top"

            elif atype == ActionType.HOVER:
                if target:
                    try:
                        loc = self.browser.page.locator(target)
                        if await loc.count() > 0:
                            await loc.first.hover(timeout=2000)
                            await asyncio.sleep(1.5)
                            return f"hovered {target} for 1.5s"
                    except Exception:
                        pass
                    # Fallback: JS dispatch hover events
                    await self.browser.page.evaluate(f"""() => {{
                        const el = document.querySelector('{target}');
                        if (el) {{
                            el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                            const rect = el.getBoundingClientRect();
                            const opts = {{bubbles: true, clientX: rect.x + rect.width/2, clientY: rect.y + rect.height/2}};
                            el.dispatchEvent(new MouseEvent('mouseenter', opts));
                            el.dispatchEvent(new MouseEvent('mouseover', opts));
                            el.dispatchEvent(new MouseEvent('mousemove', opts));
                        }}
                    }}""")
                    await asyncio.sleep(1.5)
                    return f"js-hovered {target} for 1.5s"
                return "hover (no target)"

            elif atype == ActionType.KEYBOARD:
                if value:
                    # Value like "Control+A" or "Shift+K"
                    keys = [k.strip() for k in value.split(",")]
                    await self.browser.page.evaluate("() => document.body.focus()")
                    for key in keys:
                        await self.browser.page.keyboard.press(key.strip())
                        await asyncio.sleep(0.3)
                    return f"pressed keys: {value}"
                return "keyboard (no value)"

            elif atype == ActionType.WAIT:
                await asyncio.sleep(1.0)
                return "waited 1s"

            elif atype == ActionType.EXTRACT_CODE:
                return "extract_code (handled in main loop)"

            elif atype == ActionType.CANVAS_DRAW:
                await self._try_canvas_challenge()
                return "canvas_draw executed"

        except Exception as e:
            return f"action error: {e}"

        return f"unknown action: {atype}"
