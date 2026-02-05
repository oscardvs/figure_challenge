# Browser Challenge Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an AI agent that solves 30 browser navigation challenges in under 5 minutes using Gemini 3 Flash for speed.

**Architecture:** Dual-input system (DOM + screenshot) with Gemini Flash for fast visual analysis and direct DOM parsing for hidden codes. Playwright for browser automation. Parallel processing where possible.

**Tech Stack:** Python 3.11+, Playwright, Google Gemini 3 Flash API, asyncio for parallelism

---

## Constraints & Strategy

- **Time budget:** 300 seconds / 30 challenges = **10 seconds per challenge max**
- **Model choice:** Gemini 3 Flash (`gemini-3-flash-preview`) - fastest, $0.50/1M input tokens
- **Thinking level:** `minimal` to minimize latency
- **Structured output:** JSON schema for reliable action parsing

## Challenge Types Identified

| Type | Detection | Solution |
|------|-----------|----------|
| Hidden DOM code | `data-*`, `aria-*`, hidden elements | Parse HTML for codes |
| Fake close buttons | Green "Dismiss" buttons | Click red X instead |
| Cookie consent | "Cookie Consent" heading | Click Accept |
| Decoy buttons | Many "Next", "Proceed" buttons | Find one with real navigation |
| Scroll to find | "Scroll Down" sections | JS `scrollTo(0, bottom)` |
| Moving elements | Elements that dodge cursor | Direct JS `element.click()` |
| Code entry | 6-char textbox | Extract code from DOM, fill |
| Delayed content | Content appears after timeout | Wait for selector |
| Modals/popups | Overlapping dialogs | Close in z-index order |

---

## Task 1: Project Setup

**Files:**
- Create: `figure/agent/main.py`
- Create: `figure/agent/config.py`
- Create: `figure/requirements.txt`
- Create: `figure/README.md`

**Step 1: Create project structure**

```bash
mkdir -p /home/odesha/figure/agent
```

**Step 2: Create requirements.txt**

```txt
google-genai>=1.0.0
playwright>=1.40.0
python-dotenv>=1.0.0
pydantic>=2.0.0
```

**Step 3: Create config.py**

```python
import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3-flash-preview"
THINKING_LEVEL = "minimal"
CHALLENGE_URL = "https://serene-frangipane-7fd25b.netlify.app/"
MAX_TIME_SECONDS = 300
```

**Step 4: Install dependencies**

```bash
pip install -r requirements.txt
playwright install chromium
```

**Step 5: Commit**

```bash
git add figure/
git commit -m "feat: initialize browser challenge agent project structure"
```

---

## Task 2: Metrics Tracker

**Files:**
- Create: `figure/agent/metrics.py`
- Test: `figure/agent/test_metrics.py`

**Step 1: Write failing test**

```python
# figure/agent/test_metrics.py
import pytest
from metrics import MetricsTracker

def test_metrics_tracker_records_challenge():
    tracker = MetricsTracker()
    tracker.start_challenge(1)
    tracker.end_challenge(1, success=True, tokens_in=100, tokens_out=50)

    summary = tracker.get_summary()
    assert summary["total_challenges"] == 1
    assert summary["successful"] == 1
    assert summary["total_tokens"] == 150
```

**Step 2: Run test to verify failure**

```bash
cd /home/odesha/figure/agent && python -m pytest test_metrics.py -v
```
Expected: FAIL with "No module named 'metrics'"

**Step 3: Implement MetricsTracker**

```python
# figure/agent/metrics.py
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ChallengeMetric:
    challenge_num: int
    start_time: float
    end_time: Optional[float] = None
    success: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None

@dataclass
class MetricsTracker:
    start_time: float = field(default_factory=time.time)
    challenges: dict[int, ChallengeMetric] = field(default_factory=dict)

    def start_challenge(self, num: int) -> None:
        self.challenges[num] = ChallengeMetric(
            challenge_num=num,
            start_time=time.time()
        )

    def end_challenge(
        self,
        num: int,
        success: bool,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: Optional[str] = None
    ) -> None:
        if num in self.challenges:
            self.challenges[num].end_time = time.time()
            self.challenges[num].success = success
            self.challenges[num].tokens_in = tokens_in
            self.challenges[num].tokens_out = tokens_out
            self.challenges[num].error = error

    def get_summary(self) -> dict:
        total_tokens_in = sum(c.tokens_in for c in self.challenges.values())
        total_tokens_out = sum(c.tokens_out for c in self.challenges.values())
        successful = sum(1 for c in self.challenges.values() if c.success)

        # Gemini Flash pricing: $0.50/1M input, $3/1M output
        cost = (total_tokens_in * 0.50 / 1_000_000) + (total_tokens_out * 3.0 / 1_000_000)

        return {
            "total_challenges": len(self.challenges),
            "successful": successful,
            "failed": len(self.challenges) - successful,
            "total_time_seconds": time.time() - self.start_time,
            "total_tokens": total_tokens_in + total_tokens_out,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "estimated_cost_usd": round(cost, 4),
            "per_challenge": [
                {
                    "num": c.challenge_num,
                    "time_seconds": round((c.end_time or time.time()) - c.start_time, 2),
                    "success": c.success,
                    "tokens": c.tokens_in + c.tokens_out,
                    "error": c.error
                }
                for c in sorted(self.challenges.values(), key=lambda x: x.challenge_num)
            ]
        }

    def print_summary(self) -> None:
        s = self.get_summary()
        print(f"\n{'='*50}")
        print(f"BROWSER CHALLENGE AGENT - RESULTS")
        print(f"{'='*50}")
        print(f"Challenges: {s['successful']}/{s['total_challenges']} passed")
        print(f"Total time: {s['total_time_seconds']:.1f}s")
        print(f"Tokens used: {s['total_tokens']:,} (in: {s['tokens_in']:,}, out: {s['tokens_out']:,})")
        print(f"Estimated cost: ${s['estimated_cost_usd']:.4f}")
        print(f"{'='*50}\n")
```

**Step 4: Run test to verify pass**

```bash
cd /home/odesha/figure/agent && python -m pytest test_metrics.py -v
```
Expected: PASS

**Step 5: Commit**

```bash
git add figure/agent/metrics.py figure/agent/test_metrics.py
git commit -m "feat: add metrics tracker for time, tokens, cost"
```

---

## Task 3: DOM Parser for Hidden Codes

**Files:**
- Create: `figure/agent/dom_parser.py`
- Test: `figure/agent/test_dom_parser.py`

**Step 1: Write failing test**

```python
# figure/agent/test_dom_parser.py
import pytest
from dom_parser import extract_hidden_codes

def test_extract_code_from_data_attribute():
    html = '<div data-code="ABC123">Content</div>'
    codes = extract_hidden_codes(html)
    assert "ABC123" in codes

def test_extract_code_from_aria_label():
    html = '<button aria-label="Secret code: XYZ789">Click</button>'
    codes = extract_hidden_codes(html)
    assert "XYZ789" in codes

def test_extract_code_from_hidden_element():
    html = '<span style="display:none">Code: DEF456</span>'
    codes = extract_hidden_codes(html)
    assert "DEF456" in codes

def test_extract_code_from_comment():
    html = '<!-- The code is: GHI012 -->'
    codes = extract_hidden_codes(html)
    assert "GHI012" in codes
```

**Step 2: Run test to verify failure**

```bash
cd /home/odesha/figure/agent && python -m pytest test_dom_parser.py -v
```

**Step 3: Implement DOM parser**

```python
# figure/agent/dom_parser.py
import re
from bs4 import BeautifulSoup

# Pattern for 6-character alphanumeric codes
CODE_PATTERN = re.compile(r'\b([A-Z0-9]{6})\b')

def extract_hidden_codes(html: str) -> list[str]:
    """Extract potential 6-character codes from HTML."""
    codes = set()
    soup = BeautifulSoup(html, 'html.parser')

    # 1. Check data-* attributes
    for elem in soup.find_all(attrs=lambda x: x and any(k.startswith('data-') for k in x.keys())):
        for key, value in elem.attrs.items():
            if key.startswith('data-') and isinstance(value, str):
                codes.update(CODE_PATTERN.findall(value.upper()))

    # 2. Check aria-* attributes
    for elem in soup.find_all(attrs=lambda x: x and any(k.startswith('aria-') for k in x.keys())):
        for key, value in elem.attrs.items():
            if key.startswith('aria-') and isinstance(value, str):
                codes.update(CODE_PATTERN.findall(value.upper()))

    # 3. Check hidden elements (display:none, visibility:hidden, hidden attribute)
    for elem in soup.find_all(style=re.compile(r'display:\s*none|visibility:\s*hidden')):
        text = elem.get_text()
        codes.update(CODE_PATTERN.findall(text.upper()))

    for elem in soup.find_all(attrs={'hidden': True}):
        text = elem.get_text()
        codes.update(CODE_PATTERN.findall(text.upper()))

    # 4. Check HTML comments
    comments = soup.find_all(string=lambda t: isinstance(t, str) and '<!--' in str(t.parent) if t.parent else False)
    for comment in soup.find_all(string=lambda text: isinstance(text, str)):
        if hasattr(comment, 'output_ready'):  # It's a comment
            codes.update(CODE_PATTERN.findall(str(comment).upper()))

    # Also search raw HTML for comments
    comment_pattern = re.compile(r'<!--(.*?)-->', re.DOTALL)
    for match in comment_pattern.findall(html):
        codes.update(CODE_PATTERN.findall(match.upper()))

    # 5. Check meta tags
    for meta in soup.find_all('meta'):
        content = meta.get('content', '')
        if isinstance(content, str):
            codes.update(CODE_PATTERN.findall(content.upper()))

    # 6. Check title attribute
    for elem in soup.find_all(attrs={'title': True}):
        title = elem.get('title', '')
        if isinstance(title, str):
            codes.update(CODE_PATTERN.findall(title.upper()))

    return list(codes)


def find_real_next_button(html: str) -> str | None:
    """Find the selector for the real navigation button among decoys."""
    soup = BeautifulSoup(html, 'html.parser')

    # Look for buttons/links with navigation-related onclick or href
    for elem in soup.find_all(['button', 'a']):
        onclick = elem.get('onclick', '')
        href = elem.get('href', '')

        # Check if it actually navigates
        if 'step' in href.lower() or 'next' in onclick.lower():
            if elem.get('id'):
                return f"#{elem['id']}"
            if elem.get('class'):
                return f".{elem['class'][0]}"

    return None
```

**Step 4: Run test**

```bash
cd /home/odesha/figure/agent && python -m pytest test_dom_parser.py -v
```

**Step 5: Add beautifulsoup4 to requirements and commit**

```bash
echo "beautifulsoup4>=4.12.0" >> /home/odesha/figure/requirements.txt
git add figure/agent/dom_parser.py figure/agent/test_dom_parser.py figure/requirements.txt
git commit -m "feat: add DOM parser for extracting hidden codes"
```

---

## Task 4: Gemini Vision Analyzer

**Files:**
- Create: `figure/agent/vision.py`
- Test: `figure/agent/test_vision.py`

**Step 1: Write failing test**

```python
# figure/agent/test_vision.py
import pytest
from unittest.mock import Mock, patch
from vision import VisionAnalyzer, ActionResponse

def test_action_response_schema():
    """Test that ActionResponse has required fields."""
    action = ActionResponse(
        action_type="click",
        target_selector="#submit-btn",
        reasoning="Found submit button",
        code_found=None
    )
    assert action.action_type == "click"
    assert action.target_selector == "#submit-btn"

def test_vision_analyzer_init():
    """Test VisionAnalyzer initializes with correct model."""
    with patch('vision.genai') as mock_genai:
        analyzer = VisionAnalyzer(api_key="test-key")
        mock_genai.configure.assert_called_once_with(api_key="test-key")
```

**Step 2: Run test to verify failure**

```bash
cd /home/odesha/figure/agent && python -m pytest test_vision.py -v
```

**Step 3: Implement VisionAnalyzer**

```python
# figure/agent/vision.py
import base64
from enum import Enum
from typing import Optional
from pydantic import BaseModel
import google.generativeai as genai

class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    WAIT = "wait"
    CLOSE_POPUP = "close_popup"
    EXTRACT_CODE = "extract_code"
    NAVIGATE = "navigate"

class ActionResponse(BaseModel):
    action_type: ActionType
    target_selector: Optional[str] = None
    value: Optional[str] = None  # For typing text
    reasoning: str
    code_found: Optional[str] = None
    confidence: float = 0.0

class VisionAnalyzer:
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(
            model_name="gemini-3-flash-preview",
            generation_config={
                "temperature": 0.1,
                "response_mime_type": "application/json",
            }
        )

    def analyze_page(
        self,
        screenshot_bytes: bytes,
        html: str,
        challenge_num: int,
        dom_codes: list[str]
    ) -> tuple[ActionResponse, int, int]:
        """
        Analyze page and return next action.
        Returns: (action, input_tokens, output_tokens)
        """

        # Encode screenshot
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')

        prompt = f"""You are a browser automation agent solving challenge {challenge_num}/30.

GOAL: Navigate to the next challenge step as fast as possible.

CONTEXT:
- Codes found in DOM: {dom_codes}
- This challenge may have: fake buttons, hidden codes, popups to close, forms to fill

CRITICAL RULES:
1. If there's a code entry field and we have a code, TYPE the code
2. Close popups by clicking RED X buttons, NOT green "Dismiss" buttons (they're fake)
3. Click "Accept" on cookie consent
4. Ignore decoy buttons - find the REAL navigation
5. If code is hidden, check DOM attributes (data-*, aria-*)

RESPOND WITH JSON:
{{
    "action_type": "click|type|scroll|wait|close_popup|extract_code|navigate",
    "target_selector": "CSS selector or description",
    "value": "text to type if action_type is 'type'",
    "reasoning": "brief explanation",
    "code_found": "6-char code if found",
    "confidence": 0.0-1.0
}}

Analyze the screenshot and HTML, then return the SINGLE best next action."""

        # Truncate HTML to avoid token limits
        html_truncated = html[:15000] if len(html) > 15000 else html

        response = self.model.generate_content(
            [
                {"mime_type": "image/png", "data": screenshot_b64},
                f"HTML (truncated):\n{html_truncated}\n\n{prompt}"
            ],
            generation_config={
                "thinking_config": {"thinking_level": "minimal"}
            }
        )

        # Extract token counts
        usage = response.usage_metadata
        tokens_in = usage.prompt_token_count
        tokens_out = usage.candidates_token_count

        # Parse response
        import json
        try:
            data = json.loads(response.text)
            action = ActionResponse(**data)
        except Exception as e:
            # Fallback action
            action = ActionResponse(
                action_type=ActionType.WAIT,
                reasoning=f"Failed to parse response: {e}",
                confidence=0.0
            )

        return action, tokens_in, tokens_out
```

**Step 4: Run test**

```bash
cd /home/odesha/figure/agent && python -m pytest test_vision.py -v
```

**Step 5: Commit**

```bash
git add figure/agent/vision.py figure/agent/test_vision.py
git commit -m "feat: add Gemini vision analyzer for page understanding"
```

---

## Task 5: Browser Controller

**Files:**
- Create: `figure/agent/browser.py`
- Test: `figure/agent/test_browser.py`

**Step 1: Write failing test**

```python
# figure/agent/test_browser.py
import pytest
from unittest.mock import AsyncMock, Mock, patch

@pytest.mark.asyncio
async def test_browser_controller_init():
    with patch('browser.async_playwright') as mock_pw:
        mock_pw.return_value.__aenter__ = AsyncMock()
        mock_pw.return_value.__aexit__ = AsyncMock()

        from browser import BrowserController
        controller = BrowserController()
        assert controller is not None
```

**Step 2: Run test**

```bash
cd /home/odesha/figure/agent && python -m pytest test_browser.py -v
```

**Step 3: Implement BrowserController**

```python
# figure/agent/browser.py
import asyncio
from playwright.async_api import async_playwright, Page, Browser

class BrowserController:
    def __init__(self):
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.playwright = None

    async def start(self, url: str) -> None:
        """Launch browser and navigate to URL."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=False)
        self.page = await self.browser.new_page()
        await self.page.set_viewport_size({"width": 1280, "height": 800})
        await self.page.goto(url)

    async def stop(self) -> None:
        """Close browser."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def screenshot(self) -> bytes:
        """Take screenshot of current page."""
        return await self.page.screenshot(type="png")

    async def get_html(self) -> str:
        """Get page HTML."""
        return await self.page.content()

    async def get_url(self) -> str:
        """Get current URL."""
        return self.page.url

    async def click(self, selector: str) -> bool:
        """Click element by selector. Returns success."""
        try:
            await self.page.click(selector, timeout=2000)
            return True
        except Exception:
            return False

    async def click_by_text(self, text: str) -> bool:
        """Click element containing text."""
        try:
            await self.page.click(f"text={text}", timeout=2000)
            return True
        except Exception:
            return False

    async def type_text(self, selector: str, text: str) -> bool:
        """Type text into input field."""
        try:
            await self.page.fill(selector, text)
            return True
        except Exception:
            return False

    async def scroll_to_bottom(self) -> None:
        """Scroll to page bottom."""
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    async def close_popup_by_x(self) -> bool:
        """Try to close popup by clicking X button."""
        # Try various X button selectors
        selectors = [
            "button:has(img[alt*='close'])",
            "[aria-label*='close']",
            ".close-button",
            "button:has-text('×')",
        ]
        for sel in selectors:
            try:
                await self.page.click(sel, timeout=500)
                return True
            except Exception:
                continue
        return False

    async def wait_for_navigation(self, timeout: int = 5000) -> bool:
        """Wait for navigation to complete."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
            return True
        except Exception:
            return False

    async def execute_js(self, script: str) -> any:
        """Execute JavaScript on page."""
        return await self.page.evaluate(script)
```

**Step 4: Run test**

```bash
cd /home/odesha/figure/agent && python -m pytest test_browser.py -v
```

**Step 5: Commit**

```bash
git add figure/agent/browser.py figure/agent/test_browser.py
git commit -m "feat: add Playwright browser controller"
```

---

## Task 6: Challenge Solver Orchestrator

**Files:**
- Create: `figure/agent/solver.py`
- Test: `figure/agent/test_solver.py`

**Step 1: Write failing test**

```python
# figure/agent/test_solver.py
import pytest
from unittest.mock import AsyncMock, Mock, patch
from solver import ChallengeSolver

def test_solver_init():
    solver = ChallengeSolver(api_key="test")
    assert solver.max_attempts_per_challenge == 10
```

**Step 2: Run test**

```bash
cd /home/odesha/figure/agent && python -m pytest test_solver.py -v
```

**Step 3: Implement ChallengeSolver**

```python
# figure/agent/solver.py
import asyncio
import re
from browser import BrowserController
from vision import VisionAnalyzer, ActionType
from dom_parser import extract_hidden_codes
from metrics import MetricsTracker

class ChallengeSolver:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.browser = BrowserController()
        self.vision = VisionAnalyzer(api_key)
        self.metrics = MetricsTracker()
        self.max_attempts_per_challenge = 10
        self.current_challenge = 0

    async def run(self, start_url: str) -> dict:
        """Run through all 30 challenges."""
        await self.browser.start(start_url)

        try:
            # Click START button
            await self.browser.click_by_text("START")
            await asyncio.sleep(0.5)

            for challenge_num in range(1, 31):
                self.current_challenge = challenge_num
                self.metrics.start_challenge(challenge_num)

                success = await self._solve_challenge(challenge_num)

                if not success:
                    self.metrics.end_challenge(
                        challenge_num,
                        success=False,
                        error="Failed to solve within max attempts"
                    )

        finally:
            await self.browser.stop()
            self.metrics.print_summary()

        return self.metrics.get_summary()

    async def _solve_challenge(self, challenge_num: int) -> bool:
        """Solve a single challenge."""
        total_tokens_in = 0
        total_tokens_out = 0

        for attempt in range(self.max_attempts_per_challenge):
            # Get current state
            screenshot = await self.browser.screenshot()
            html = await self.browser.get_html()
            url = await self.browser.get_url()

            # Check if we've moved to next challenge
            if f"step{challenge_num + 1}" in url or (challenge_num == 30 and "complete" in url.lower()):
                self.metrics.end_challenge(
                    challenge_num,
                    success=True,
                    tokens_in=total_tokens_in,
                    tokens_out=total_tokens_out
                )
                return True

            # Extract codes from DOM first (fast, no API call)
            dom_codes = extract_hidden_codes(html)

            # If we have a code and there's an input field, try filling it directly
            if dom_codes:
                filled = await self._try_fill_code(dom_codes)
                if filled:
                    await asyncio.sleep(0.3)
                    continue

            # Use vision to determine action
            action, tokens_in, tokens_out = self.vision.analyze_page(
                screenshot, html, challenge_num, dom_codes
            )
            total_tokens_in += tokens_in
            total_tokens_out += tokens_out

            # Execute action
            await self._execute_action(action)

            # Brief wait for page to update
            await asyncio.sleep(0.3)

        return False

    async def _try_fill_code(self, codes: list[str]) -> bool:
        """Try to fill code into input field."""
        for code in codes:
            try:
                # Try common input selectors
                selectors = [
                    "input[type='text']",
                    "input[placeholder*='code']",
                    "input[placeholder*='Code']",
                ]
                for sel in selectors:
                    if await self.browser.type_text(sel, code):
                        # Try to submit
                        await self.browser.click("button:has-text('Submit')")
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

        elif action.action_type == ActionType.SCROLL:
            await self.browser.scroll_to_bottom()

        elif action.action_type == ActionType.CLOSE_POPUP:
            await self.browser.close_popup_by_x()

        elif action.action_type == ActionType.WAIT:
            await asyncio.sleep(0.5)

        elif action.action_type == ActionType.EXTRACT_CODE:
            # Code extraction handled in main loop
            pass
```

**Step 4: Run test**

```bash
cd /home/odesha/figure/agent && python -m pytest test_solver.py -v
```

**Step 5: Commit**

```bash
git add figure/agent/solver.py figure/agent/test_solver.py
git commit -m "feat: add challenge solver orchestrator"
```

---

## Task 7: Main Entry Point

**Files:**
- Create: `figure/agent/main.py`

**Step 1: Implement main.py**

```python
# figure/agent/main.py
import asyncio
import json
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

from solver import ChallengeSolver
from config import GEMINI_API_KEY, CHALLENGE_URL, MAX_TIME_SECONDS

async def main():
    if not GEMINI_API_KEY:
        print("ERROR: Set GEMINI_API_KEY environment variable")
        sys.exit(1)

    print(f"Starting Browser Challenge Agent")
    print(f"Target: {CHALLENGE_URL}")
    print(f"Time limit: {MAX_TIME_SECONDS}s")
    print("-" * 50)

    solver = ChallengeSolver(GEMINI_API_KEY)

    try:
        results = await asyncio.wait_for(
            solver.run(CHALLENGE_URL),
            timeout=MAX_TIME_SECONDS
        )
    except asyncio.TimeoutError:
        print(f"\nTIMEOUT: Exceeded {MAX_TIME_SECONDS}s limit")
        results = solver.metrics.get_summary()

    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = f"results_{timestamp}.json"

    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to: {results_file}")

    return results

if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
```

**Step 2: Create .env.example**

```bash
echo "GEMINI_API_KEY=your-api-key-here" > /home/odesha/figure/.env.example
```

**Step 3: Update README.md**

```markdown
# Browser Challenge Agent

AI agent that solves 30 browser navigation challenges in under 5 minutes.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
playwright install chromium
```

2. Set API key:
```bash
cp .env.example .env
# Edit .env and add your Gemini API key
```

3. Run:
```bash
cd agent
python main.py
```

## Results

The agent outputs:
- Console summary with pass/fail, time, tokens, cost
- `results_<timestamp>.json` with detailed metrics

## Architecture

- **Gemini 3 Flash** for fast visual analysis (minimal thinking)
- **DOM parsing** for hidden codes (no API calls)
- **Playwright** for browser automation
- **Parallel inputs**: screenshot + HTML analyzed together
```

**Step 4: Commit**

```bash
git add figure/agent/main.py figure/.env.example figure/README.md
git commit -m "feat: add main entry point and documentation"
```

---

## Task 8: Speed Optimizations

**Files:**
- Modify: `figure/agent/solver.py`
- Modify: `figure/agent/vision.py`

**Step 1: Add parallel DOM+Vision analysis**

In `solver.py`, modify `_solve_challenge` to run DOM extraction and screenshot capture in parallel:

```python
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
        if f"step{challenge_num + 1}" in url or (challenge_num == 30 and "complete" in url.lower()):
            self.metrics.end_challenge(
                challenge_num,
                success=True,
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out
            )
            return True

        # Fast path: DOM code extraction (no API call)
        dom_codes = extract_hidden_codes(html)

        if dom_codes:
            filled = await self._try_fill_code(dom_codes)
            if filled:
                await asyncio.sleep(0.2)
                continue

        # Vision analysis only if needed
        action, tokens_in, tokens_out = self.vision.analyze_page(
            screenshot, html, challenge_num, dom_codes
        )
        total_tokens_in += tokens_in
        total_tokens_out += tokens_out

        await self._execute_action(action)
        await asyncio.sleep(0.2)

    return False
```

**Step 2: Commit**

```bash
git add figure/agent/solver.py
git commit -m "perf: add parallel screenshot/HTML capture"
```

---

## Task 9: Challenge-Specific Handlers

**Files:**
- Create: `figure/agent/handlers.py`

**Step 1: Implement specialized handlers**

```python
# figure/agent/handlers.py
"""Specialized handlers for known challenge patterns."""

import re

async def handle_cookie_consent(browser) -> bool:
    """Handle cookie consent popups."""
    try:
        await browser.click("button:has-text('Accept')")
        return True
    except Exception:
        return False

async def handle_fake_popup(browser) -> bool:
    """Close popups with fake dismiss buttons using X."""
    # The red X is the real close, green Dismiss is fake
    selectors = [
        "button:has(img)",  # X button with image
        "[class*='close']",
        "button[aria-label*='close']",
    ]
    for sel in selectors:
        try:
            await browser.click(sel)
            return True
        except Exception:
            continue
    return False

async def handle_scroll_challenge(browser) -> bool:
    """Scroll to bottom to find navigation."""
    await browser.scroll_to_bottom()
    return True

async def handle_moving_element(browser, selector: str) -> bool:
    """Click moving element using JS (bypasses movement)."""
    try:
        await browser.execute_js(f"""
            document.querySelector('{selector}')?.click()
        """)
        return True
    except Exception:
        return False

def detect_challenge_type(html: str) -> str:
    """Detect challenge type from HTML patterns."""
    if "Cookie Consent" in html:
        return "cookie"
    if "close button is fake" in html.lower():
        return "fake_popup"
    if "Scroll Down to Find" in html:
        return "scroll"
    if "Hidden DOM Challenge" in html:
        return "hidden_code"
    if "Moving" in html:
        return "moving"
    return "unknown"
```

**Step 2: Commit**

```bash
git add figure/agent/handlers.py
git commit -m "feat: add challenge-specific handlers"
```

---

## Task 10: Final Integration & Testing

**Files:**
- Modify: `figure/agent/solver.py`
- Create: `figure/run.sh`

**Step 1: Integrate handlers into solver**

Add to `solver.py`:

```python
from handlers import (
    handle_cookie_consent,
    handle_fake_popup,
    handle_scroll_challenge,
    handle_moving_element,
    detect_challenge_type
)

# In _solve_challenge, before vision analysis:
challenge_type = detect_challenge_type(html)

# Fast path for known patterns
if challenge_type == "cookie":
    await handle_cookie_consent(self.browser)
    continue
elif challenge_type == "fake_popup":
    await handle_fake_popup(self.browser)
    continue
elif challenge_type == "scroll":
    await handle_scroll_challenge(self.browser)
    continue
```

**Step 2: Create run script**

```bash
#!/bin/bash
# figure/run.sh
cd "$(dirname "$0")/agent"
python main.py
```

**Step 3: Make executable and test**

```bash
chmod +x /home/odesha/figure/run.sh
```

**Step 4: Create zip package script**

```bash
#!/bin/bash
# figure/package.sh
cd "$(dirname "$0")"
zip -r browser-challenge-agent.zip \
    agent/ \
    requirements.txt \
    README.md \
    .env.example \
    run.sh \
    -x "*.pyc" -x "__pycache__/*" -x ".env"

echo "Created: browser-challenge-agent.zip"
```

**Step 5: Final commit**

```bash
git add figure/
git commit -m "feat: complete browser challenge agent v1"
```

---

## Summary

| Task | Component | Purpose |
|------|-----------|---------|
| 1 | Project setup | Structure, deps, config |
| 2 | MetricsTracker | Time, tokens, cost tracking |
| 3 | DOM Parser | Extract hidden codes fast |
| 4 | VisionAnalyzer | Gemini Flash page analysis |
| 5 | BrowserController | Playwright automation |
| 6 | ChallengeSolver | Main orchestration loop |
| 7 | Main entry | CLI interface |
| 8 | Speed opts | Parallel capture |
| 9 | Handlers | Challenge-specific logic |
| 10 | Integration | Final assembly |

**Expected Performance:**
- ~10 sec/challenge average
- ~$0.05-0.10 total token cost
- 30/30 challenges in <5 min
