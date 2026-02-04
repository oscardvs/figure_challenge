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
