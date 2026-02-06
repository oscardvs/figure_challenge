"""Entry point for the agent-based challenge solver using Gemini 3."""
import asyncio
import argparse
import json
import sys
import signal
from datetime import datetime
from dotenv import load_dotenv

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from agent_solver import AgentChallengeSolver
from config import GEMINI_API_KEY, CHALLENGE_URL, MAX_TIME_SECONDS


async def main(headless: bool = False, keep_open: bool = False):
    if not GEMINI_API_KEY:
        print("ERROR: Set GEMINI_API_KEY environment variable")
        sys.exit(1)

    print(f"Agent-Based Browser Challenge Solver (Gemini 3)")
    print(f"Target: {CHALLENGE_URL}")
    print(f"Time limit: {MAX_TIME_SECONDS}s")
    print(f"Headless: {headless}")
    print(f"Keep browser open: {keep_open}")
    print("-" * 50)

    solver = AgentChallengeSolver(GEMINI_API_KEY)
    solver.keep_browser_open = keep_open

    try:
        results = await asyncio.wait_for(
            solver.run(CHALLENGE_URL, headless=headless),
            timeout=MAX_TIME_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"\nTIMEOUT: Exceeded {MAX_TIME_SECONDS}s limit")
        results = solver.metrics.get_summary()
        solver.metrics.print_summary()

        if keep_open:
            print("\n" + "="*60)
            print("  BROWSER LEFT OPEN FOR DEBUGGING")
            print("  Press Enter to close browser and exit...")
            print("="*60 + "\n")
            try:
                await asyncio.get_event_loop().run_in_executor(None, input)
            except (EOFError, KeyboardInterrupt):
                pass
            finally:
                try:
                    await solver.browser.stop()
                except Exception:
                    pass

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = f"agent_results_{timestamp}.json"

    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to: {results_file}")
    return results


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description="Agent-Based Browser Challenge Solver")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--keep-open", action="store_true", help="Keep browser open after timeout for debugging")
    args = parser.parse_args()
    asyncio.run(main(headless=args.headless, keep_open=args.keep_open))