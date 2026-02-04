import re
from bs4 import BeautifulSoup, Comment

# Pattern for 6-character alphanumeric codes
CODE_PATTERN = re.compile(r'\b([A-Z0-9]{6})\b')


def extract_hidden_codes(html: str) -> list[str]:
    """Extract potential 6-character codes from HTML."""
    codes = set()
    soup = BeautifulSoup(html, 'html.parser')

    # 1. Check data-* attributes
    for elem in soup.find_all(True):
        for key, value in elem.attrs.items():
            if key.startswith('data-') and isinstance(value, str):
                codes.update(CODE_PATTERN.findall(value.upper()))

    # 2. Check aria-* attributes
    for elem in soup.find_all(True):
        for key, value in elem.attrs.items():
            if key.startswith('aria-') and isinstance(value, str):
                codes.update(CODE_PATTERN.findall(value.upper()))

    # 3. Check hidden elements (display:none, visibility:hidden, hidden attribute)
    for elem in soup.find_all(style=re.compile(r'display:\s*none|visibility:\s*hidden')):
        text = elem.get_text()
        codes.update(CODE_PATTERN.findall(text.upper()))

    for elem in soup.find_all(attrs={'hidden': True}):
        text = elem.get_text()
        codes.update(CODE_PATTERN.findall(text.upper()))

    # 4. Check HTML comments
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        codes.update(CODE_PATTERN.findall(str(comment).upper()))

    # Also search raw HTML for comments (backup)
    comment_pattern = re.compile(r'<!--(.*?)-->', re.DOTALL)
    for match in comment_pattern.findall(html):
        codes.update(CODE_PATTERN.findall(match.upper()))

    # 5. Check meta tags
    for meta in soup.find_all('meta'):
        content = meta.get('content', '')
        if isinstance(content, str):
            codes.update(CODE_PATTERN.findall(content.upper()))

    # 6. Check title attribute
    for elem in soup.find_all(attrs={'title': True}):
        title = elem.get('title', '')
        if isinstance(title, str):
            codes.update(CODE_PATTERN.findall(title.upper()))

    return list(codes)


def find_real_next_button(html: str) -> str | None:
    """Find the selector for the real navigation button among decoys."""
    soup = BeautifulSoup(html, 'html.parser')

    # Look for buttons/links with navigation-related onclick or href
    for elem in soup.find_all(['button', 'a']):
        onclick = elem.get('onclick', '')
        href = elem.get('href', '')

        # Check if it actually navigates
        if 'step' in href.lower() or 'next' in onclick.lower():
            if elem.get('id'):
                return f"#{elem['id']}"
            if elem.get('class'):
                return f".{elem['class'][0]}"

    return None
