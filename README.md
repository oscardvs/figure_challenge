# Browser Challenge Agent

AI agent that solves 30 browser navigation challenges in under 5 minutes.

## Setup

1. Install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
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

For headless mode:
```bash
python main.py --headless
```

## Results

The agent outputs:
- Console summary with pass/fail, time, tokens, cost
- `results_<timestamp>.json` with detailed metrics

## Architecture

- **Gemini 2.0 Flash** for fast visual analysis
- **DOM parsing** for hidden codes (no API calls needed)
- **Playwright** for browser automation
- **Parallel inputs**: screenshot + HTML analyzed together

## Challenge Types Handled

| Type | Detection | Solution |
|------|-----------|----------|
| Hidden DOM code | `data-*`, `aria-*`, hidden elements | Parse HTML for codes |
| Fake close buttons | Green "Dismiss" buttons | Click red X instead |
| Cookie consent | "Cookie Consent" heading | Click Accept |
| Decoy buttons | Many "Next", "Proceed" buttons | Find real navigation |
| Scroll to find | "Scroll Down" sections | JS `scrollTo(0, bottom)` |
| Moving elements | Elements that dodge cursor | Direct JS `element.click()` |
| Code entry | 6-char textbox | Extract code from DOM, fill |

## Project Structure

```
figure/
├── agent/
│   ├── main.py          # Entry point
│   ├── config.py        # Configuration
│   ├── solver.py        # Main orchestrator
│   ├── browser.py       # Playwright controller
│   ├── vision.py        # Gemini analyzer
│   ├── dom_parser.py    # Hidden code extractor
│   ├── metrics.py       # Performance tracking
│   └── handlers.py      # Challenge-specific handlers
├── requirements.txt
├── .env.example
└── README.md
```
