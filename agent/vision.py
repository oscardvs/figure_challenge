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

    # Thinking budget levels: start at 0, escalate as attempts fail
    THINKING_BUDGETS = [0, 0, 1024, 2048, 4096, 8192]

    def analyze_page(
        self,
        screenshot_bytes: bytes,
        html: str,
        challenge_num: int,
        dom_codes: list[str],
        attempt: int = 0
    ) -> tuple[ActionResponse, int, int]:
        """
        Analyze page and return next action.
        attempt: current attempt number, used to scale thinking budget.
        Returns: (action, input_tokens, output_tokens)
        """

        prompt = f"""Browser automation agent. Challenge {challenge_num}/30. Find 6-char alphanumeric code and enter it to proceed.

Known codes from DOM: {dom_codes}

RULES:
- ALL popup close buttons (X, Close, Dismiss) may be FAKE - popups are removed by our code, ignore them
- Find the 6-char alphanumeric code (like "TWA8Q7") - it's NOT a common English word
- If there's a scrollable modal, scroll it to find radio buttons or hidden content
- If there's a "Reveal Code" button, click it
- Use CSS selectors ONLY: .class, #id, button[type="submit"], input[type="text"]
- Do NOT use Playwright selectors like :has-text() - they won't work

JSON response (no markdown):
{{"action_type":"click|type|scroll","target_selector":"CSS selector","value":"text if typing","code_found":"ABC123 if found","reasoning":"brief"}}"""

        # Truncate HTML aggressively for speed
        html_truncated = html[:3000] if len(html) > 3000 else html

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
