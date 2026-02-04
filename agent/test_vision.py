import pytest
from unittest.mock import Mock, patch, MagicMock
from vision import VisionAnalyzer, ActionResponse, ActionType


def test_action_response_schema():
    """Test that ActionResponse has required fields."""
    action = ActionResponse(
        action_type=ActionType.CLICK,
        target_selector="#submit-btn",
        reasoning="Found submit button",
        code_found=None
    )
    assert action.action_type == ActionType.CLICK
    assert action.target_selector == "#submit-btn"


def test_action_response_with_value():
    """Test ActionResponse with value for typing."""
    action = ActionResponse(
        action_type=ActionType.TYPE,
        target_selector="input[type='text']",
        value="ABC123",
        reasoning="Filling code",
        code_found="ABC123"
    )
    assert action.action_type == ActionType.TYPE
    assert action.value == "ABC123"


def test_action_types_enum():
    """Test all action types exist."""
    assert ActionType.CLICK == "click"
    assert ActionType.TYPE == "type"
    assert ActionType.SCROLL == "scroll"
    assert ActionType.WAIT == "wait"
    assert ActionType.CLOSE_POPUP == "close_popup"
