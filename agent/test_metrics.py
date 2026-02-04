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


def test_metrics_tracker_multiple_challenges():
    tracker = MetricsTracker()
    tracker.start_challenge(1)
    tracker.end_challenge(1, success=True, tokens_in=100, tokens_out=50)
    tracker.start_challenge(2)
    tracker.end_challenge(2, success=False, tokens_in=200, tokens_out=100, error="timeout")

    summary = tracker.get_summary()
    assert summary["total_challenges"] == 2
    assert summary["successful"] == 1
    assert summary["failed"] == 1
    assert summary["total_tokens"] == 450
