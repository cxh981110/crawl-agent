import json
import time

from DrissionPage import ChromiumOptions, ChromiumPage


def _parse_actions(actions_json: str) -> list[dict]:
    if not actions_json:
        return []

    actions = json.loads(actions_json)
    if not isinstance(actions, list):
        raise ValueError("actions_json must be a JSON array.")
    return actions


def fetch_page(
    url: str,
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
) -> dict:
    """
    Open a page in Chromium and optionally perform interactive actions.
    """

    actions = _parse_actions(actions_json)
    options = ChromiumOptions().auto_port().headless()
    page = ChromiumPage(addr_or_opts=options)

    try:
        page.get(url=url, timeout=timeout)
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        for action in actions:
            action_type = action.get("type") or action.get("action") or "click"
            if action_type == "wait":
                time.sleep(float(action.get("seconds", 1)))
                continue

            if action_type == "scroll_bottom":
                page.scroll.to_bottom()
                time.sleep(float(action.get("sleep_after", 1)))
                continue

            locator = action.get("locator")
            if not locator:
                raise ValueError(f"Action requires locator: {action}")

            element = page.ele(locator, timeout=action.get("timeout", timeout))
            if element is None:
                raise ValueError(f"Element not found for locator: {locator}")

            if action_type == "click":
                element.click()
            elif action_type == "input":
                element.input(action.get("value", ""))
            else:
                raise ValueError(f"Unsupported action type: {action_type}")

            time.sleep(float(action.get("sleep_after", 1)))

        html = page.html
        payload = {
            "url": page.url or url,
            "title": page.title,
            "html": html,
        }
        return payload
    finally:
        page.quit()
