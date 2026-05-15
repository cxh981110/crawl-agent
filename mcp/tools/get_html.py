import json
import re
import time
from typing import Any, Optional

from DrissionPage import ChromiumOptions, ChromiumPage


def _parse_actions(actions_json: str) -> list[dict]:
    if not actions_json:
        return []

    actions = json.loads(actions_json)
    if not isinstance(actions, list):
        raise ValueError("actions_json must be a JSON array.")
    return actions


def _page_signature(page: ChromiumPage) -> str:
    html = page.html or ""
    return "{}::{}".format(page.url or "", hash(html))


def _is_disabled_pager(ele: Any) -> bool:
    try:
        cls = (ele.attr("class") or "").lower()
        aria_disabled = (ele.attr("aria-disabled") or "").lower()
        disabled_attr = (ele.attr("disabled") or "").lower()
        if "disabled" in cls or aria_disabled == "true" or disabled_attr in {"disabled", "true"}:
            return True
    except Exception:
        pass
    return False


def _is_visible(ele: Any) -> bool:
    try:
        visible = ele.run_js("return !!(this.offsetWidth || this.offsetHeight || this.getClientRects().length);")
        return bool(visible)
    except Exception:
        return True


def _safe_ele(scope: Any, locator: str, timeout: float = 1.0) -> Optional[Any]:
    try:
        return scope.ele(locator, timeout=timeout)
    except Exception:
        return None


def _safe_eles(scope: Any, locator: str, timeout: float = 1.0) -> list[Any]:
    try:
        items = scope.eles(locator, timeout=timeout)
        return [item for item in (items or []) if item is not None]
    except Exception:
        return []


def _build_chromium_options(use_new_headless: bool) -> ChromiumOptions:
    options = ChromiumOptions().auto_port().headless()
    options.set_argument("--disable-gpu")
    options.set_argument("--no-first-run")
    options.set_argument("--disable-dev-shm-usage")
    if use_new_headless:
        options.set_argument("--headless=new")
    return options


def _create_chromium_page() -> ChromiumPage:
    last_error: Optional[Exception] = None
    for use_new_headless in (True, False):
        try:
            return ChromiumPage(addr_or_opts=_build_chromium_options(use_new_headless=use_new_headless))
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to create Chromium page.")


def _try_activate_tab(page: ChromiumPage, section_hint: str, wait_seconds: float) -> None:
    candidates = []
    if section_hint:
        candidates.append(section_hint)
    candidates.extend(["\u4ee3\u9500\u673a\u6784", "\u9500\u552e\u673a\u6784", "\u673a\u6784"])

    for keyword in candidates:
        if not keyword:
            continue
        try:
            clicked = page.run_js(
                """
                const keyword = arguments[0];
                const nodes = Array.from(document.querySelectorAll('span,a,button,li,div'));
                const target = nodes.find(node => (node.textContent || '').trim().includes(keyword));
                if (!target) return false;
                target.click();
                return true;
                """,
                keyword,
            )
        except Exception:
            clicked = False
        if clicked:
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            return


def _pager_suffix_from_next_selector(next_selector: str) -> str:
    match = re.search(r"#next_page(\d*)\b", next_selector or "")
    if match:
        return match.group(1)
    return ""


def _candidate_score(ele: Any, section_hint: str) -> int:
    try:
        return int(
            ele.run_js(
                """
                const keyword = arguments[0] || '';
                let score = 0;
                const visible = !!(this.offsetWidth || this.offsetHeight || this.getClientRects().length);
                if (visible) score += 100;
                const textNeedles = [keyword, '\u673a\u6784\u540d\u79f0', '\u9500\u552e\u673a\u6784', '\u4ee3\u9500\u673a\u6784']
                  .filter(Boolean);
                let node = this;
                for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
                  const text = node.textContent || '';
                  if (textNeedles.some(item => text.includes(item))) score += Math.max(45 - depth * 4, 5);
                  if (node.querySelector && node.querySelector('table')) score += Math.max(24 - depth * 3, 3);
                  if ((node.className || '').toString().includes('page')) score += 8;
                }
                return score;
                """,
                section_hint or "",
            )
        )
    except Exception:
        return 0


def _candidate_identity(ele: Any) -> str:
    try:
        return "{}::{}".format(
            ele.attr("id") or ele.attr("outerHTML")[:120],
            ele.run_js("return Array.prototype.indexOf.call(document.querySelectorAll('a,button,li,span'), this);"),
        )
    except Exception:
        return str(id(ele))


def _find_numeric_page_button(page: ChromiumPage, target_page_num: int, section_hint: str) -> Optional[Any]:
    target_text = str(target_page_num)
    locators = [
        f"xpath://a[normalize-space(text())='{target_text}']",
        f"xpath://button[normalize-space(text())='{target_text}']",
        f"xpath://li[normalize-space(text())='{target_text}']",
        f"xpath://span[normalize-space(text())='{target_text}']",
    ]
    candidates = []
    for locator in locators:
        for ele in _safe_eles(page, locator, timeout=0.8):
            if _is_disabled_pager(ele):
                continue
            candidates.append((_candidate_score(ele, section_hint), ele))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else None


def _find_next_buttons(page: ChromiumPage, section_hint: str = "", next_selector: str = "") -> list[Any]:
    if next_selector:
        locators = [next_selector]
    else:
        locators = [
            "css:a[id^='next_page']",
            "css:a.next",
            "css:a.nextpage",
            "css:a[rel='next']",
            "xpath://a[contains(normalize-space(.), '\u4e0b\u4e00\u9875')]",
            "xpath://a[contains(normalize-space(.), '\u4e0b\u9875')]",
            "xpath://a[contains(normalize-space(.), 'Next')]",
            "xpath://a[normalize-space(.)='>']",
            "xpath://button[contains(normalize-space(.), '\u4e0b\u4e00\u9875')]",
            "xpath://button[contains(normalize-space(.), 'Next')]",
            "xpath://button[normalize-space(.)='>']",
        ]

    scored: list[tuple[int, Any]] = []
    seen = set()
    for locator in locators:
        for ele in _safe_eles(page, locator, timeout=0.8):
            identity = _candidate_identity(ele)
            if identity in seen:
                continue
            seen.add(identity)
            if _is_disabled_pager(ele):
                continue
            scored.append((_candidate_score(ele, section_hint), ele))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [ele for _, ele in scored]


def _parse_pager_state_for_button(ele: Any) -> tuple[Optional[int], Optional[int]]:
    try:
        state = ele.run_js(
            """
            let node = this;
            for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
              const text = node.textContent || '';
              const totalMatch = text.match(/共\\s*(\\d+)\\s*页/);
              const active = node.querySelector && node.querySelector('a.active,.active,[aria-current="page"],.current,.on');
              let current = null;
              if (active) {
                const activeText = (active.textContent || '').trim();
                if (/^\\d+$/.test(activeText)) current = parseInt(activeText, 10);
              }
              let total = totalMatch ? parseInt(totalMatch[1], 10) : null;
              if (total === null && node.querySelectorAll) {
                const nums = Array.from(node.querySelectorAll('a,button,span,li'))
                  .map(item => (item.textContent || '').trim())
                  .filter(item => /^\\d+$/.test(item))
                  .map(item => parseInt(item, 10));
                if (nums.length) total = Math.max(...nums);
              }
              if (current !== null || total !== null) return {current, total};
            }
            return {current: null, total: null};
            """
        )
        if isinstance(state, dict):
            return state.get("current"), state.get("total")
    except Exception:
        pass
    return None, None


def _parse_selector_bound_pager_state(page: ChromiumPage, next_selector: str) -> tuple[Optional[int], Optional[int]]:
    suffix = _pager_suffix_from_next_selector(next_selector)
    current = None
    total = None
    if suffix or next_selector:
        current_ele = _safe_ele(page, "css:#page_num{}".format(suffix), timeout=0.8)
        total_ele = _safe_ele(page, "css:#total_page{}".format(suffix), timeout=0.8)
        if current_ele is not None:
            text = (current_ele.text or "").strip()
            numbers = re.findall(r"\d+", text)
            if len(numbers) == 1 and not (len(numbers[0]) > 1 and numbers[0] == text):
                current = int(numbers[0])
        if total_ele is not None:
            match = re.search(r"\u5171\s*(\d+)\s*\u9875", (total_ele.text or "").strip())
            if match:
                total = int(match.group(1))
    return current, total


def _click_pager_element(ele: Any) -> bool:
    try:
        ele.click()
        return True
    except Exception:
        pass
    try:
        ele.run_js("this.click();")
        return True
    except Exception:
        return False


def _paginate_and_collect(
    page: ChromiumPage,
    seed_url: str,
    wait_seconds: float,
    max_pages: int,
    section_hint: str = "",
    next_selector: str = "",
) -> list[dict[str, str]]:
    pages: list[dict[str, str]] = []
    seen_signatures = set()
    stagnant_rounds = 0

    def collect_current() -> None:
        sig = _page_signature(page)
        if sig in seen_signatures:
            return
        seen_signatures.add(sig)
        pages.append({"url": page.url or seed_url, "title": page.title, "html": page.html or ""})

    collect_current()

    while len(pages) < max_pages:
        before_sig = _page_signature(page)
        next_buttons = _find_next_buttons(page, section_hint=section_hint, next_selector=next_selector)
        if not next_buttons:
            break

        current, total = _parse_selector_bound_pager_state(page, next_selector) if next_selector else (None, None)
        if total is None or current is None:
            button_current, button_total = _parse_pager_state_for_button(next_buttons[0])
            current = current if current is not None else button_current
            total = total if total is not None else button_total

        if total is not None and len(pages) >= total:
            break
        if current is not None and total is not None and current >= total:
            break

        clicked = False
        if current is not None:
            target_btn = _find_numeric_page_button(page, current + 1, section_hint=section_hint)
            if target_btn is not None:
                clicked = _click_pager_element(target_btn)

        if not clicked:
            for next_btn in next_buttons:
                if _click_pager_element(next_btn):
                    clicked = True
                    break

        if not clicked:
            break

        if wait_seconds > 0:
            time.sleep(wait_seconds)

        after_sig = _page_signature(page)
        if after_sig == before_sig:
            stagnant_rounds += 1
            if stagnant_rounds >= 2:
                break
            continue

        stagnant_rounds = 0
        prev_len = len(pages)
        collect_current()
        if len(pages) == prev_len:
            stagnant_rounds += 1
            if stagnant_rounds >= 2:
                break

    return pages


def fetch_page(
    url: str,
    wait_seconds: float = 2.0,
    actions_json: str = "[]",
    timeout: int = 15,
    auto_paginate: bool = True,
    max_pages: int = 20,
    section_hint: str = "",
    next_selector: str = "",
) -> dict:
    """
    Open a page in Chromium and optionally collect paginated HTML snapshots.
    Pagination is inferred by ranking next-page controls near the target section.
    """

    actions = _parse_actions(actions_json)
    page = _create_chromium_page()

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

            element = _safe_ele(page, locator, timeout=action.get("timeout", timeout))
            if element is None:
                raise ValueError(f"Element not found for locator: {locator}")
            if action_type == "click":
                element.click()
            elif action_type == "input":
                element.input(action.get("value", ""))
            else:
                raise ValueError(f"Unsupported action type: {action_type}")
            time.sleep(float(action.get("sleep_after", 1)))

        _try_activate_tab(page=page, section_hint=section_hint, wait_seconds=wait_seconds)

        if auto_paginate:
            pages = _paginate_and_collect(
                page=page,
                seed_url=url,
                wait_seconds=wait_seconds,
                max_pages=max_pages,
                section_hint=section_hint,
                next_selector=next_selector,
            )
        else:
            pages = [{"url": page.url or url, "title": page.title, "html": page.html or ""}]

        if len(pages) == 1:
            html = pages[0]["html"]
        else:
            fragments = []
            for idx, item in enumerate(pages, start=1):
                fragments.append(
                    "<section data-page-index='{}' data-page-url='{}'>{}</section>".format(
                        idx, item.get("url", ""), item.get("html", "")
                    )
                )
            html = "<html><body>{}</body></html>".format("".join(fragments))

        try:
            resource_urls = page.run_js(
                """
                return performance.getEntriesByType('resource')
                  .map(item => item.name)
                  .filter(Boolean);
                """
            )
        except Exception:
            resource_urls = []

        return {
            "url": page.url or url,
            "title": page.title,
            "html": html,
            "page_count": len(pages),
            "pages": [{"url": item["url"], "title": item["title"]} for item in pages],
            "resource_urls": resource_urls if isinstance(resource_urls, list) else [],
        }
    finally:
        page.quit()
