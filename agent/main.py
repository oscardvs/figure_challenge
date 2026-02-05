import asyncio
import argparse
import json
import sys
from datetime import datetime
from dotenv import load_dotenv

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from solver import ChallengeSolver
from config import GEMINI_API_KEY, CHALLENGE_URL, MAX_TIME_SECONDS


async def main(headless: bool = False):
    if not GEMINI_API_KEY:
        print("ERROR: Set GEMINI_API_KEY environment variable", flush=True)
        print("  export GEMINI_API_KEY=your-api-key", flush=True)
        print("  or create a .env file with GEMINI_API_KEY=your-api-key", flush=True)
        sys.exit(1)

    print(f"Starting Browser Challenge Agent", flush=True)
    print(f"Target: {CHALLENGE_URL}", flush=True)
    print(f"Time limit: {MAX_TIME_SECONDS}s", flush=True)
    print(f"Headless: {headless}", flush=True)
    print("-" * 50, flush=True)

    solver = ChallengeSolver(GEMINI_API_KEY)

    try:
        results = await asyncio.wait_for(
            solver.run(CHALLENGE_URL, headless=headless),
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

    parser = argparse.ArgumentParser(description="Browser Challenge Agent")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode"
    )
    args = parser.parse_args()

    asyncio.run(main(headless=args.headless))
