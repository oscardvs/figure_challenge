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
        self.model_name = "gemini-2.0-flash"

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

RESPOND WITH JSON ONLY (no markdown):
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
                        types.Part.from_text(f"HTML (truncated):\n{html_truncated}\n\n{prompt}")
                    ]
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            )
        )

        # Extract token counts
        tokens_in = response.usage_metadata.prompt_token_count
        tokens_out = response.usage_metadata.candidates_token_count

        # Parse response
        try:
            text = response.text.strip()
            # Remove markdown code blocks if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]
            data = json.loads(text)
            action = ActionResponse(**data)
        except Exception as e:
            # Fallback action
            action = ActionResponse(
                action_type=ActionType.WAIT,
                reasoning=f"Failed to parse response: {e}",
                confidence=0.0
            )

        return action, tokens_in, tokens_out
