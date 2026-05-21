import io
import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests
import urllib3


urllib3.disable_warnings()


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%()")
    query = quote(parts.query, safe="=&%")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {"User-Agent": "Mozilla/5.0"}
    url = _safe_url(url)
    try:
        response = requests.get(url, timeout=timeout, headers=headers)
        response.raise_for_status()
        return response
    except requests.exceptions.SSLError:
        response = requests.get(url, timeout=timeout, headers=headers, verify=False)
        response.raise_for_status()
        return response


def parse_pdf_content(url: str, timeout: int = 20) -> str:
    """
    Download and parse a PDF. This tool does not extract business fields.
    It returns page text and detected tables for the LLM to interpret.
    """
    try:
        import pdfplumber
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pdfplumber is required for PDF parsing. Install dependencies with: pip install -r requirements.txt"
        ) from exc

    response = _http_get(url, timeout=timeout)
    content = response.content
    if not content.startswith(b"%PDF"):
        raise RuntimeError("Downloaded content is not a PDF.")

    pages: list[dict[str, Any]] = []
    all_text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            all_text_parts.append(text)
            pages.append(
                {
                    "page": page_index,
                    "text": text,
                    "tables": tables,
                }
            )

    full_text = "\n".join(all_text_parts)
    result = {
        "url": url,
        "text": full_text[:50000],
        "pages": pages,
        "evidence": {
            "source": "parse_pdf_content",
            "page_count": len(pages),
            "byte_length": len(content),
        },
    }
    return json.dumps(result, ensure_ascii=False, indent=2)
