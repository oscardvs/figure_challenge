"""Entry point for the agent-based challenge solver using Gemini 3."""
import asyncio
import argparse
import json
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from agent_solver import AgentChallengeSolver
from config import GEMINI_API_KEY, CHALLENGE_URL, MAX_TIME_SECONDS


async def main(headless: bool = False):
    if not GEMINI_API_KEY:
        print("ERROR: Set GEMINI_API_KEY environment variable")
        sys.exit(1)

    print(f"Agent-Based Browser Challenge Solver (Gemini 3)")
    print(f"Target: {CHALLENGE_URL}")
    print(f"Time limit: {MAX_TIME_SECONDS}s")
    print(f"Headless: {headless}")
    print("-" * 50)

    solver = AgentChallengeSolver(GEMINI_API_KEY)

    try:
        results = await asyncio.wait_for(
            solver.run(CHALLENGE_URL, headless=headless),
            timeout=MAX_TIME_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"\nTIMEOUT: Exceeded {MAX_TIME_SECONDS}s limit")
        results = solver.metrics.get_summary()

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
    args = parser.parse_args()
    asyncio.run(main(headless=args.headless))
