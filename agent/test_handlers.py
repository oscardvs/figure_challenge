import pytest
from handlers import detect_challenge_type, get_handler_for_type


def test_detect_cookie_consent():
    html = '<div>Cookie Consent</div><button>Accept</button>'
    assert detect_challenge_type(html) == "cookie"


def test_detect_fake_popup():
    html = '<div class="popup"><button>Dismiss</button><button class="close">X</button></div>'
    assert detect_challenge_type(html) == "fake_popup"


def test_detect_scroll():
    html = '<div>Scroll Down to find the button</div>'
    assert detect_challenge_type(html) == "scroll"


def test_detect_hidden_code():
    html = '<div data-code="ABC123" style="display:hidden">Hidden code challenge</div>'
    assert detect_challenge_type(html) == "hidden_code"


def test_detect_decoy():
    html = '<button>Next</button>' * 5
    assert detect_challenge_type(html) == "decoy"


def test_detect_unknown():
    html = '<div>Hello World</div>'
    assert detect_challenge_type(html) == "unknown"


def test_get_handler_for_cookie():
    handler = get_handler_for_type("cookie")
    assert handler is not None
    assert callable(handler)


def test_get_handler_for_unknown():
    handler = get_handler_for_type("unknown")
    assert handler is None
