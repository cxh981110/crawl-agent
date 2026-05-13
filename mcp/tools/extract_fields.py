import json

from agent.config import build_schema

from .build_context import build_context_payload
from .extract_from_context import extract_from_context_payload
from .page_pipeline import fetch_and_clean_page


def extract_fields(
    url: str,
    fields_json: str,
    token_budget: int = 3000,
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
) -> str:
    """
    Extract structured values from a page by field labels.
    Fixed internal pipeline: fetch html -> clean html -> build context -> extract fields.
    """

    raw_spec = json.loads(fields_json)
    schema = build_schema(raw_spec)

    page_payload = fetch_and_clean_page(
        url=url,
        wait_seconds=wait_seconds,
        actions_json=actions_json,
        timeout=timeout,
    )

    context = build_context_payload(
        html=page_payload.get("cleaned_html", ""),
        schema=schema,
        token_budget=token_budget,
    )

    data, evidence = extract_from_context_payload(
        context=context,
        schema=schema,
    )
    missing_fields = [key for key, value in data.items() if value in (None, "", [])]

    response = {
        "url": page_payload.get("url", url),
        "title": page_payload.get("title"),
        "data": data,
        "missing_fields": missing_fields,
        "evidence": evidence,
        "context_meta": (context.get("meta") or {}),
    }
    return json.dumps(response, ensure_ascii=False, indent=2)
