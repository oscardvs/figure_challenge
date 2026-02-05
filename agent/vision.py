import base64
import json
from enum import Enum
from typing import Optional
from pydantic import BaseModel
from google import genai
from google.genai import types


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    WAIT = "wait"
    CLOSE_POPUP = "close_popup"
    EXTRACT_CODE = "extract_code"
    NAVIGATE = "navigate"
    HOVER = "hover"


class ActionResponse(BaseModel):
    action_type: ActionType
    target_selector: Optional[str] = None
    value: Optional[str] = None  # For typing text
    reasoning: str
    code_found: Optional[str] = None
    confidence: float = 0.0


class VisionAnalyzer:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        # Use Gemini 3 Flash Preview - fast and intelligent
        self.model_name = "gemini-3-flash-preview"

    # Thinking budget levels: start higher, escalate as attempts fail
    THINKING_BUDGETS = [2048, 4096, 4096, 8192, 8192, 8192, 8192, 8192, 8192, 8192]

    def analyze_page(
        self,
        screenshot_bytes: bytes,
        html: str,
        challenge_num: int,
        dom_codes: list[str],
        attempt: int = 0,
        failed_codes: list[str] | None = None
    ) -> tuple[ActionResponse, int, int]:
        """
        Analyze page and return next action.
        attempt: current attempt number, used to scale thinking budget.
        failed_codes: codes already tried and failed on this step.
        Returns: (action, input_tokens, output_tokens)
        """

        failed_info = ""
        if failed_codes:
            failed_info = f"\nPreviously tried codes that FAILED (do NOT suggest these): {failed_codes}"

        prompt = f"""You are a browser automation agent solving challenge {challenge_num}/30.
Each challenge hides a 6-character alphanumeric code (like "TWA8Q7", "P4HWBQ"). Find it and enter it to proceed.

Known codes extracted from DOM: {dom_codes}{failed_info}

CHALLENGE TYPES you may encounter:
1. HIDDEN CODE: Code in HTML comments, data-* attributes, hidden elements, Base64 strings, or aria-* attributes
2. SCROLL REVEAL: Must scroll down/up to reveal the code or a button
3. HOVER REVEAL: Must hover over a specific element for 1+ second to reveal code
4. CLICK REVEAL: Must click "Reveal Code" or similar buttons
5. TIMER/COUNTDOWN: Wait for countdown to finish, then code appears
6. RADIO MODAL: Modal with radio options - select "correct" option and click "Submit & Continue"
7. KEYBOARD SEQUENCE: Press key combos (Ctrl+A, Shift+K) to reveal code
8. DRAG AND DROP: Drag pieces into slots to assemble the code
9. CANVAS DRAWING: Draw shapes on a canvas element
10. TIMING CAPTURE: Click "Capture" while a timing window is active
11. AUDIO: Play audio, listen for spelled-out code, click Complete
12. SPLIT PARTS: Code parts scattered across page - click each to collect
13. ROTATING CODE: Click "Capture" multiple times as code rotates
14. MULTI-TAB: Click through tabs to collect code parts
15. SEQUENCE: Perform 4 actions (click, hover, type, scroll) in sequence
16. MATH PUZZLE: Solve math expression, type answer, click Solve
17. VIDEO FRAMES: Seek to a specific frame number to find the code
18. FAKE POPUPS: Popups with fake close buttons - find the real dismiss button
19. TRAP BUTTONS: Many similar buttons but only one is real

RULES:
- The code is ALWAYS exactly 6 characters, uppercase letters A-Z and digits 0-9 only
- It is NOT a common English word (not BUTTON, SCROLL, HIDDEN, etc.)
- Look carefully at the screenshot for any visible 6-char alphanumeric code
- Popup close buttons (X, Close, Dismiss) are often FAKE - our code handles popups already
- Use CSS selectors ONLY: .class, #id, button[type="submit"], input[type="text"]
- Do NOT use Playwright selectors like :has-text()
- If you see the code in the screenshot but not in the DOM codes list, report it in code_found
- If a code from DOM was already tried and failed, look for a DIFFERENT code (maybe hidden in the page)

JSON response (no markdown):
{{"action_type":"click|type|scroll|hover|wait|extract_code","target_selector":"CSS selector","value":"text to type","code_found":"ABC123 or null","reasoning":"what you see and what action to take"}}"""

        # Truncate HTML for context (5000 chars provides good coverage)
        html_truncated = html[:5000] if len(html) > 5000 else html

        # Scale thinking budget based on attempt number
        budget_idx = min(attempt, len(self.THINKING_BUDGETS) - 1)
        thinking_budget = self.THINKING_BUDGETS[budget_idx]
        print(f"    [vision] sending: screenshot + {len(html_truncated)} chars HTML, model={self.model_name}, thinking={thinking_budget}", flush=True)

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(
                            data=screenshot_bytes,
                            mime_type="image/png"
                        ),
                        types.Part.from_text(text=f"HTML (truncated):\n{html_truncated}\n\n{prompt}")
                    ]
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
            )
        )

        # Extract token counts
        tokens_in = response.usage_metadata.prompt_token_count
        tokens_out = response.usage_metadata.candidates_token_count

        print(f"    [vision] received: {tokens_in} in / {tokens_out} out tokens", flush=True)

        # Parse response
        try:
            text = response.text.strip()
            print(f"    [vision] raw response: {text[:300]}", flush=True)
            # Remove markdown code blocks if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]
                text = text.strip()
            # Extract first valid JSON object (handle trailing garbage from Gemini)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Find the first complete JSON object by matching braces
                depth = 0
                end_idx = 0
                for i, ch in enumerate(text):
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end_idx = i + 1
                            break
                if end_idx > 0:
                    data = json.loads(text[:end_idx])
                else:
                    raise
            action = ActionResponse(**data)
            print(f"    [vision] parsed: action={action.action_type}, code={action.code_found}, target={action.target_selector}", flush=True)
        except Exception as e:
            print(f"    [vision] PARSE ERROR: {e}", flush=True)
            # Fallback action
            action = ActionResponse(
                action_type=ActionType.WAIT,
                reasoning=f"Failed to parse response: {e}",
                confidence=0.0
            )

        return action, tokens_in, tokens_out
