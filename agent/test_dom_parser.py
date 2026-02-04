import pytest
from dom_parser import extract_hidden_codes


def test_extract_code_from_data_attribute():
    html = '<div data-code="ABC123">Content</div>'
    codes = extract_hidden_codes(html)
    assert "ABC123" in codes


def test_extract_code_from_aria_label():
    html = '<button aria-label="Secret code: XYZ789">Click</button>'
    codes = extract_hidden_codes(html)
    assert "XYZ789" in codes


def test_extract_code_from_hidden_element():
    html = '<span style="display:none">Code: DEF456</span>'
    codes = extract_hidden_codes(html)
    assert "DEF456" in codes


def test_extract_code_from_comment():
    html = '<!-- The code is: GHI012 -->'
    codes = extract_hidden_codes(html)
    assert "GHI012" in codes


def test_extract_code_from_title_attribute():
    html = '<a title="Enter code JKL345 to proceed">Link</a>'
    codes = extract_hidden_codes(html)
    assert "JKL345" in codes


def test_no_false_positives():
    html = '<div>Hello World</div>'
    codes = extract_hidden_codes(html)
    assert len(codes) == 0
