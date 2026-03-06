import asyncio

from ofac_sanctions_agent.parser import has_captcha


class _FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    async def content(self) -> str:
        return self._html


def test_has_captcha_detects_keywords() -> None:
    page = _FakePage("<html><body>please complete the CAPTCHA challenge</body></html>")
    result = asyncio.run(has_captcha(page))  # type: ignore[arg-type]
    assert result is True


def test_has_captcha_false_when_clean() -> None:
    page = _FakePage("<html><body>normal search results page</body></html>")
    result = asyncio.run(has_captcha(page))  # type: ignore[arg-type]
    assert result is False

