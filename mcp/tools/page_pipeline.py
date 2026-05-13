from typing import Any

from bs4 import BeautifulSoup, Comment

from .get_html import fetch_page


def clean_html_keep_structure(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")

    # Remove obvious noise, keep content structure for table/list extraction.
    for tag in soup(["script", "style", "svg", "path", "footer", "header", "nav", "noscript"]):
        tag.decompose()

    # Keep only essential attributes for links; strip others to reduce noise.
    for tag in soup.find_all(True):
        if tag.name == "a":
            href = tag.get("href")
            title = tag.get("title")
            attrs = {}
            if href:
                attrs["href"] = href
            if title:
                attrs["title"] = title
            tag.attrs = attrs
        else:
            tag.attrs = {}

    # Remove comments.
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    # Keep HTML structure; do not convert to markdown/plain text here.
    return soup.decode()


def fetch_and_clean_page(
    url: str,
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
) -> dict[str, Any]:
    page_payload = fetch_page(
        url=url,
        wait_seconds=wait_seconds,
        actions_json=actions_json,
        timeout=timeout,
    )
    cleaned_html = clean_html_keep_structure(page_payload.get("html", ""))
    payload = dict(page_payload)
    payload["cleaned_html"] = cleaned_html
    return payload
