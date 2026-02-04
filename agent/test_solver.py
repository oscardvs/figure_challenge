import pytest
from unittest.mock import AsyncMock, Mock, patch


def test_solver_init():
    from solver import ChallengeSolver
    solver = ChallengeSolver(api_key="test")
    assert solver.max_attempts_per_challenge == 10
    assert solver.current_challenge == 0


def test_solver_has_components():
    from solver import ChallengeSolver
    solver = ChallengeSolver(api_key="test")
    assert hasattr(solver, 'browser')
    assert hasattr(solver, 'vision')
    assert hasattr(solver, 'metrics')


def test_solver_has_run_method():
    from solver import ChallengeSolver
    solver = ChallengeSolver(api_key="test")
    assert hasattr(solver, 'run')
    assert callable(solver.run)
