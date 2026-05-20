import json
import re
import time
from math import ceil
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from typing import Any, Optional

import requests
from DrissionPage import ChromiumOptions, ChromiumPage

try:
    from playwright.sync_api import Page, sync_playwright
except Exception:  # pragma: no cover - optional dependency fallback
    Page = Any
    sync_playwright = None


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
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    try:
        options.set_browser_path(chrome_path)
    except Exception:
        pass
    options.set_user_agent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
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


def _playwright_click_text(page: Page, text: str, wait_seconds: float) -> bool:
    if not text:
        return False
    try:
        clicked = page.evaluate(
            """
            (keyword) => {
              const nodes = Array.from(document.querySelectorAll('a,button,li,span,div'))
                .filter(node => {
                  const style = window.getComputedStyle(node);
                  return style && style.visibility !== 'hidden' && style.display !== 'none';
                })
                .map(node => ({node, text: (node.textContent || '').trim()}))
                .filter(item => item.text && item.text.includes(keyword))
                .sort((a, b) => {
                  const clickable = (item) => {
                    const tag = item.node.tagName.toLowerCase();
                    return (item.node.getAttribute('onclick') || item.node.getAttribute('href') || ['a','button','li'].includes(tag)) ? 1 : 0;
                  };
                  const clickableDelta = clickable(b) - clickable(a);
                  if (clickableDelta) return clickableDelta;
                  const exactDelta = (b.text === keyword ? 1 : 0) - (a.text === keyword ? 1 : 0);
                  if (exactDelta) return exactDelta;
                  return a.text.length - b.text.length;
                });
              const target = nodes.length ? nodes[0].node : null;
              if (!target) return false;
              target.click();
              return true;
            }
            """,
            text,
        )
    except Exception:
        clicked = False
    if clicked and wait_seconds > 0:
        page.wait_for_timeout(int(wait_seconds * 1000))
    return bool(clicked)


def _playwright_try_activate_tab(page: Page, section_hint: str, wait_seconds: float) -> None:
    candidates = []
    if section_hint:
        candidates.append(section_hint)
    candidates.extend(["\u4ee3\u9500\u673a\u6784", "\u9500\u552e\u673a\u6784", "\u673a\u6784"])
    for keyword in candidates:
        if _playwright_click_text(page, keyword, wait_seconds):
            return


def _playwright_run_actions(page: Page, actions: list[dict], timeout: int) -> None:
    for action in actions:
        action_type = action.get("type") or action.get("action") or "click"
        if action_type == "wait":
            page.wait_for_timeout(int(float(action.get("seconds", 1)) * 1000))
            continue
        if action_type == "scroll_bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(int(float(action.get("sleep_after", 1)) * 1000))
            continue

        locator = action.get("locator")
        if not locator:
            raise ValueError(f"Action requires locator: {action}")
        target = page.locator(locator).first
        target.wait_for(timeout=int(float(action.get("timeout", timeout)) * 1000))
        if action_type == "click":
            target.click()
        elif action_type == "input":
            target.fill(str(action.get("value", "")))
        else:
            raise ValueError(f"Unsupported action type: {action_type}")
        page.wait_for_timeout(int(float(action.get("sleep_after", 1)) * 1000))


def _playwright_page_signature(page: Page) -> str:
    try:
        html = page.content()
        return "{}::{}".format(page.url or "", hash(html))
    except Exception:
        return str(time.time())


def _playwright_collect(page: Page, seed_url: str) -> dict[str, str]:
    main_html = page.content()
    fragments = ["<section data-main-url='{}'>{}</section>".format(page.url or seed_url, main_html)]
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_html = frame.content()
            fragments.append("<section data-frame-url='{}'>{}</section>".format(frame.url or "", frame_html))
        except Exception:
            continue
    html = "<html><body>{}</body></html>".format("".join(fragments))
    return {"url": page.url or seed_url, "title": page.title(), "html": html}


def _playwright_click_next(page: Page, next_selector: str, wait_seconds: float) -> bool:
    if next_selector:
        try:
            locator = page.locator(next_selector).first
            if locator.count() > 0 and locator.is_enabled():
                locator.click()
                page.wait_for_timeout(int(wait_seconds * 1000))
                return True
        except Exception:
            return False

    return bool(
        page.evaluate(
            """
            () => {
              const needles = ['下一页', '下页', 'Next', '>'];
              const nodes = Array.from(document.querySelectorAll('a,button,li,span'));
              const target = nodes.find(node => {
                const text = (node.textContent || '').trim();
                const cls = (node.className || '').toString().toLowerCase();
                const disabled = cls.includes('disabled') || node.getAttribute('aria-disabled') === 'true';
                return !disabled && needles.includes(text);
              });
              if (!target) return false;
              target.click();
              return true;
            }
            """
        )
    )


def _extract_total_and_rows(parsed: Any) -> tuple[Optional[int], int]:
    total = None
    row_count = 0

    def visit(value: Any) -> None:
        nonlocal total, row_count
        if isinstance(value, list):
            row_count = max(row_count, len(value))
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, nested in value.items():
            if key in ("total", "totalCount", "total_count", "recordCount"):
                if isinstance(nested, int):
                    total = nested if total is None else max(total, nested)
                elif isinstance(nested, str) and nested.isdigit():
                    parsed_total = int(nested)
                    total = parsed_total if total is None else max(total, parsed_total)
            elif key == "count" and isinstance(nested, int) and nested > row_count:
                total = nested if total is None else max(total, nested)
            visit(nested)

    visit(parsed)
    return total, row_count


def _replace_query_param(url: str, key: str, value: int) -> str:
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query[key] = [str(value)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def _expand_paginated_json(items: list[dict[str, Any]], timeout: int) -> list[dict[str, Any]]:
    expanded = list(items)
    seen_urls = {item.get("url", "") for item in expanded}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    for item in list(items):
        source_url = item.get("url", "")
        query = parse_qs(urlparse(source_url).query)
        if "pageNum" not in query or "pageSize" not in query:
            continue
        try:
            page_num = int(query.get("pageNum", ["1"])[0])
            page_size = int(query.get("pageSize", ["0"])[0])
            parsed = json.loads(item.get("text", ""))
        except Exception:
            continue
        if page_size <= 0:
            continue
        total, row_count = _extract_total_and_rows(parsed)
        if total is None or total <= row_count:
            continue
        max_page = min(ceil(total / page_size), page_num + 3)
        for next_page in range(page_num + 1, max_page + 1):
            next_url = _replace_query_param(source_url, "pageNum", next_page)
            if next_url in seen_urls:
                continue
            try:
                response = requests.get(next_url, headers=headers, timeout=timeout, verify=False)
                response.raise_for_status()
                text = response.text or ""
            except Exception:
                continue
            seen_urls.add(next_url)
            expanded.append({"url": next_url, "status": response.status_code, "text": text})
    return expanded


def _expand_paginated_json_with_page(page: Page, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded = list(items)
    seen_urls = {item.get("url", "") for item in expanded}
    for item in list(items):
        source_url = item.get("url", "")
        query = parse_qs(urlparse(source_url).query)
        if "pageNum" not in query or "pageSize" not in query:
            continue
        try:
            page_num = int(query.get("pageNum", ["1"])[0])
            page_size = int(query.get("pageSize", ["0"])[0])
            parsed = json.loads(item.get("text", ""))
        except Exception:
            continue
        if page_size <= 0:
            continue
        total, row_count = _extract_total_and_rows(parsed)
        if total is None or total <= row_count:
            continue
        max_page = min(ceil(total / page_size), page_num + 3)
        for next_page in range(page_num + 1, max_page + 1):
            next_url = _replace_query_param(source_url, "pageNum", next_page)
            if next_url in seen_urls:
                continue
            try:
                payload = page.evaluate(
                    """
                    async (url) => {
                      const response = await fetch(url, {credentials: 'include'});
                      return {status: response.status, text: await response.text()};
                    }
                    """,
                    next_url,
                )
            except Exception:
                continue
            seen_urls.add(next_url)
            expanded.append(
                {
                    "url": next_url,
                    "status": payload.get("status") if isinstance(payload, dict) else None,
                    "text": payload.get("text", "") if isinstance(payload, dict) else "",
                }
            )
    return expanded


def _trim_network_json(items: list[dict[str, Any]], max_items: int = 24, max_text: int = 20000) -> list[dict[str, Any]]:
    trimmed = []
    def score(item: dict[str, Any]) -> int:
        haystack = "{} {}".format(item.get("url", ""), item.get("text", "")[:1000]).lower()
        needles = ("agency", "sale", "sales", "fundagency", "销售", "代销", "直销", "机构")
        return sum(1 for needle in needles if needle in haystack)

    ranked = sorted(enumerate(items), key=lambda pair: (score(pair[1]), pair[0]), reverse=True)
    selected_indexes = sorted(index for index, _ in ranked[:max_items])
    for index in selected_indexes:
        item = items[index]
        text = item.get("text", "")
        trimmed.append(
            {
                "url": item.get("url", ""),
                "status": item.get("status"),
                "text": text[:max_text] if isinstance(text, str) else text,
            }
        )
    return trimmed


def _fetch_page_playwright(
    url: str,
    wait_seconds: float,
    actions_json: str,
    timeout: int,
    auto_paginate: bool,
    max_pages: int,
    section_hint: str,
    next_selector: str,
) -> dict:
    if sync_playwright is None:
        raise RuntimeError("playwright is not installed")

    actions = _parse_actions(actions_json)
    network_json: list[dict[str, Any]] = []
    resource_urls: list[str] = []
    timeout_ms = int(timeout * 1000)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                )
            )
            page = context.new_page()

            def on_response(response: Any) -> None:
                try:
                    response_url = response.url
                    resource_urls.append(response_url)
                    content_type = (response.headers.get("content-type") or "").lower()
                    if "json" not in content_type:
                        return
                    text = response.text()
                    network_json.append({"url": response_url, "status": response.status, "text": text})
                except Exception:
                    return

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
            except Exception:
                pass
            if wait_seconds > 0:
                page.wait_for_timeout(int(wait_seconds * 1000))

            _playwright_run_actions(page, actions, timeout=timeout)
            _playwright_try_activate_tab(page=page, section_hint=section_hint, wait_seconds=wait_seconds)

            pages: list[dict[str, str]] = []
            seen_signatures = set()

            def collect_current() -> None:
                sig = _playwright_page_signature(page)
                if sig in seen_signatures:
                    return
                seen_signatures.add(sig)
                pages.append(_playwright_collect(page, url))

            collect_current()
            if auto_paginate:
                while len(pages) < max_pages:
                    before_sig = _playwright_page_signature(page)
                    if not _playwright_click_next(page, next_selector=next_selector, wait_seconds=wait_seconds):
                        break
                    if _playwright_page_signature(page) == before_sig:
                        break
                    collect_current()

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

            return {
                "url": page.url or url,
                "title": page.title(),
                "html": html,
                "page_count": len(pages),
                "pages": [{"url": item["url"], "title": item["title"]} for item in pages],
                "resource_urls": resource_urls,
                "network_json": _trim_network_json(
                    _expand_paginated_json(_expand_paginated_json_with_page(page, network_json), timeout=timeout)
                ),
                "browser_engine": "playwright",
            }
        finally:
            browser.close()


def _fetch_page_requests(url: str, timeout: int) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=timeout, verify=False)
    response.raise_for_status()
    if not response.encoding:
        response.encoding = response.apparent_encoding
    html = (response.text or "").lstrip("\ufeff")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    return {
        "url": response.url or url,
        "title": title,
        "html": html,
        "page_count": 1,
        "pages": [{"url": response.url or url, "title": title}],
        "resource_urls": [],
        "network_json": [],
        "browser_engine": "requests",
    }


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

    try:
        return _fetch_page_playwright(
            url=url,
            wait_seconds=wait_seconds,
            actions_json=actions_json,
            timeout=timeout,
            auto_paginate=auto_paginate,
            max_pages=max_pages,
            section_hint=section_hint,
            next_selector=next_selector,
        )
    except Exception as playwright_error:
        last_error = playwright_error

    try:
        payload = _fetch_page_requests(url=url, timeout=timeout)
        payload["playwright_error"] = str(last_error)
        return payload
    except Exception as requests_error:
        last_error = RuntimeError(
            "playwright_error={}; requests_error={}".format(last_error, requests_error)
        )

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
            "network_json": [],
            "browser_engine": "drissionpage",
            "playwright_error": str(last_error),
        }
    finally:
        page.quit()
