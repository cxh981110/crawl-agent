import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Any, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from jsonschema import ValidationError, validate
from langgraph.graph import END, START, StateGraph
from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI

from agent.mcp_client import StdioMCPClient


DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_MCP_SERVER = Path(__file__).resolve().parent.parent / "mcp" / "server.py"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUSINESS_DIR = PROJECT_ROOT / "businesses"
PROMPT_DIR = PROJECT_ROOT / "prompts"
DEFAULT_BUSINESS = "company_profile"
MAX_TURNS = 8


load_dotenv(PROJECT_ROOT / ".env")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _trace_enabled() -> bool:
    return _env_flag("LLM_TRACE", False)


def _trace_content_chars() -> int:
    try:
        return max(0, int(os.getenv("LLM_TRACE_CONTENT_CHARS", "1200")))
    except ValueError:
        return 1200


def _trace(message: str) -> None:
    if _trace_enabled():
        print("[llm-agent] {}".format(message), file=sys.stderr, flush=True)


def _trace_block(title: str, content: Any) -> None:
    if not _trace_enabled():
        return
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    limit = _trace_content_chars()
    if limit and len(text) > limit:
        text = "{}\n... <truncated {} chars>".format(text[:limit], len(text) - limit)
    print("[llm-agent] {}:\n{}".format(title, text), file=sys.stderr, flush=True)


def _llm_extra_body(model_name: str, base_url: Optional[str]) -> Optional[Dict[str, Any]]:
    if _env_flag("LLM_ENABLE_THINKING", False):
        return None
    model_is_qwen = (model_name or "").lower().startswith("qwen")
    base_is_dashscope = "dashscope.aliyuncs.com" in (base_url or "").lower()
    if not model_is_qwen and not base_is_dashscope:
        return None
    return {"enable_thinking": False}


class LlmState(TypedDict, total=False):
    url: str
    business: str
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    output_schema: Dict[str, Any]
    called_tools: bool
    pending_tool_calls: List[Dict[str, str]]
    last_content: str
    final_result: Optional[Dict[str, Any]]
    fallback_result: Optional[Dict[str, Any]]
    step_count: int
    record_mode: str
    fields_json_text: str
    tool_hints: Dict[str, Any]


def _load_business_config(business: str) -> Dict[str, Any]:
    config_path = BUSINESS_DIR / "{}.json".format(business)
    if not config_path.exists():
        raise RuntimeError("Business config not found: {}".format(config_path))
    # Be tolerant to UTF-8 BOM from editors/PowerShell.
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def _load_business_prompt(business: str, business_config: Dict[str, Any]) -> str:
    prompt_file = business_config.get("prompt_file") or "{}.md".format(business)
    prompt_path = PROMPT_DIR / str(prompt_file)
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8-sig").strip()
    return str(business_config.get("prompt", "")).strip()


def _to_openai_tools(mcp_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tools = []
    for tool in mcp_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
        )
    return tools


def _chat_completion_kwargs(model_name: str, base_url: Optional[str], **kwargs: Any) -> Dict[str, Any]:
    extra_body = _llm_extra_body(model_name=model_name, base_url=base_url)
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def _llm_step_node(state: LlmState, config: Dict[str, Any]) -> LlmState:
    if state["step_count"] >= MAX_TURNS:
        debug_trace = _build_debug_trace(state.get("messages", []))
        return {
            "fallback_result": {
                "url": state["url"],
                "strategy": "llm_max_turns",
                "data": {},
                "missing_fields": [],
                "evidence": {
                    "reason": "LLM tool loop exceeded max turns.",
                    "step_count": state["step_count"],
                    "debug_trace": debug_trace,
                },
                "business": state["business"],
            },
            "pending_tool_calls": [],
            "last_content": "",
        }

    step_count = state["step_count"] + 1
    _trace(
        "llm step {} start: messages={}, tools={}, thinking={}".format(
            step_count,
            len(state["messages"]),
            len(state["tools"]),
            "on" if _env_flag("LLM_ENABLE_THINKING", False) else "off",
        )
    )
    started = time.time()
    response = config["client"].chat.completions.create(
        **_chat_completion_kwargs(
            model_name=config["model_name"],
            base_url=config.get("base_url"),
            model=config["model_name"],
            messages=state["messages"],
            tools=state["tools"],
            tool_choice="auto",
            temperature=0,
            timeout=config["request_timeout"],
        )
    )
    elapsed = time.time() - started
    message = response.choices[0].message
    tool_calls = message.tool_calls or []
    _trace("llm step {} done in {:.1f}s: tool_calls={}, content_chars={}".format(
        step_count, elapsed, len(tool_calls), len(message.content or "")
    ))

    #先取出历史数据
    updated_messages = list(state["messages"])
    if tool_calls:
        _trace_block(
            "llm step {} tool_calls".format(step_count),
            [
                {
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                }
                for call in tool_calls
            ],
        )
        updated_messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            }
        )
        return {
            "messages": updated_messages,
            "pending_tool_calls": [
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                }
                for call in tool_calls
            ],
            "called_tools": True,
            "last_content": "",
            "step_count": state["step_count"] + 1,
        }

    content = message.content or "{}"
    _trace_block("llm step {} content".format(step_count), content)
    updated_messages.append({"role": "assistant", "content": content})
    if not state.get("called_tools", False):
        updated_messages.append(
            {
                "role": "user",
                "content": "You must call at least one tool before final answer.",
            }
        )
        return {
            "messages": updated_messages,
            "pending_tool_calls": [],
            "last_content": "",
            "step_count": state["step_count"] + 1,
        }

    return {
        "messages": updated_messages,
        "pending_tool_calls": [],
        "last_content": content,
        "step_count": state["step_count"] + 1,
    }


def _route_after_llm_step(state: LlmState) -> str:
    if state.get("fallback_result") is not None:
        return "done"
    if state.get("final_result") is not None:
        return "done"
    if state.get("pending_tool_calls"):
        return "run_tools"
    if state.get("last_content"):
        return "validate_output"
    return "llm_step"


def _tool_step_node(state: LlmState, config: Dict[str, Any]) -> LlmState:
    messages = list(state["messages"])
    final_result = None
    seen_calls = _seen_tool_calls(messages)
    for tool_call in state.get("pending_tool_calls", []):
        args_text = tool_call.get("arguments") or "{}"
        tool_started = time.time()
        try:
            args = json.loads(args_text)
            tool_name = tool_call["name"]
            args["url"] = _safe_tool_url(target_url=state["url"], requested_url=args.get("url"))
            call_key = _tool_call_key(tool_name, args)
            if call_key in seen_calls:
                _trace("tool skipped as duplicate: {} args={}".format(tool_name, json.dumps(args, ensure_ascii=False)))
                tool_output = (
                    "TOOL_NOTICE: duplicate tool call skipped. "
                    "Use the content already returned by previous tool calls and produce the final JSON. "
                    "If only direct-sale records are present, return those records instead of searching indefinitely."
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_output,
                    }
                )
                continue
            seen_calls.add(call_key)

            # Keep the target URL stable while allowing the LLM to choose the MCP tool.
            if tool_name == "get_html_content":
                tool_hints = state.get("tool_hints", {}) or {}
                for key in ("timeout", "wait_seconds", "section_hint", "auto_paginate", "max_pages", "next_selector"):
                    if key in tool_hints and key not in args:
                        args[key] = tool_hints[key]
            elif tool_name == "parse_pdf_content":
                tool_hints = state.get("tool_hints", {}) or {}
                for key in ("timeout",):
                    if key in tool_hints and key not in args:
                        args[key] = tool_hints[key]

            _trace("tool start: {} args={}".format(tool_name, json.dumps(args, ensure_ascii=False)))
            tool_output = config["mcp_client"].call_tool(tool_call["name"], args)
            _trace(
                "tool done: {} in {:.1f}s output_chars={}".format(
                    tool_name, time.time() - tool_started, len(tool_output or "")
                )
            )
            _trace_block("tool output {}".format(tool_name), tool_output)
            parsed_output = _try_parse_json(tool_output)
            if parsed_output is not None and _has_meaningful_data(parsed_output.get("data")):
                final_result = {
                    "url": parsed_output.get("url", state["url"]),
                    "strategy": tool_name,
                    "data": parsed_output.get("data"),
                    "missing_fields": parsed_output.get("missing_fields", []),
                    "evidence": parsed_output.get("evidence", {}),
                }
        except (TimeoutError, RuntimeError) as exc:
            error_text = str(exc)
            _trace("tool error: {} after {:.1f}s error={}".format(
                tool_call["name"], time.time() - tool_started, error_text
            ))
            if isinstance(exc, TimeoutError) or "timed out" in error_text or "No MCP response" in error_text:
                return {
                    "fallback_result": {
                        "url": state["url"],
                        "strategy": "mcp_tool_failed",
                        "data": [] if state.get("record_mode") == "array" else {},
                        "missing_fields": [],
                        "evidence": {
                            "reason": "MCP tool execution failed and the agent stopped instead of continuing to the next LLM step.",
                            "tool_name": tool_call["name"],
                            "arguments": args_text,
                            "error": error_text,
                        },
                        "business": state["business"],
                    },
                    "pending_tool_calls": [],
                }
            tool_output = (
                "TOOL_ERROR: tool execution failed for {}. "
                "arguments={}, error={}"
                ).format(tool_call["name"], args_text, exc)
        except Exception as exc:
            tool_output = (
                "TOOL_ERROR: tool execution failed for {}. "
                "arguments={}, error={}"
            ).format(tool_call["name"], args_text, exc)
            _trace("tool error: {} after {:.1f}s error={}".format(
                tool_call["name"], time.time() - tool_started, exc
            ))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_output,
            }
        )

    result: LlmState = {"messages": messages, "pending_tool_calls": []}
    if final_result is not None:
        result["final_result"] = final_result
    return result


def _seen_tool_calls(messages: List[Dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    history = list(messages)
    if history and history[-1].get("role") == "assistant" and history[-1].get("tool_calls"):
        history = history[:-1]
    for message in history:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []) or []:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            seen.add(_tool_call_key(name, args))
    return seen


def _tool_call_key(tool_name: str, args: Dict[str, Any]) -> str:
    normalized = {key: args.get(key) for key in sorted(args)}
    return "{}:{}".format(tool_name, json.dumps(normalized, ensure_ascii=True, sort_keys=True))


def _safe_tool_url(target_url: str, requested_url: Any) -> str:
    if not isinstance(requested_url, str) or not requested_url.strip():
        return target_url

    requested = urljoin(target_url, requested_url.strip())
    target_parts = urlparse(target_url)
    requested_parts = urlparse(requested)
    if not requested_parts.scheme or not requested_parts.netloc:
        return target_url

    target_host = (target_parts.hostname or "").lower()
    requested_host = (requested_parts.hostname or "").lower()
    if requested_host == target_host or requested_host.endswith("." + target_host):
        return requested
    return target_url


def _validate_output_node(state: LlmState, config: Dict[str, Any]) -> LlmState:
    content = state.get("last_content") or "{}"
    _trace("validate output start: content_chars={}".format(len(content)))
    try:
        result = _parse_json_content(content)
        validate(instance=result, schema=state["output_schema"])
        _trace("validate output done: schema valid")
        return {"final_result": result}
    except ValidationError as schema_err:
        _trace("validate output schema error: {}".format(schema_err.message))
        messages = list(state["messages"])
        messages.append(
            {
                "role": "user",
                "content": "Output schema validation failed: {}. Return valid JSON only.".format(schema_err.message),
            }
        )
        return {"messages": messages, "last_content": ""}
    except Exception:
        _trace("validate output parse error; using llm_text fallback")
        return {
            "fallback_result": {
                "url": state["url"],
                "strategy": "llm_text",
                "data": {},
                "missing_fields": [],
                "evidence": {"raw": content},
                "business": state["business"],
            }
        }


def _route_after_validate(state: LlmState) -> str:
    if state.get("final_result") is not None:
        return "done"
    if state.get("fallback_result") is not None:
        return "done"
    return "llm_step"


def _build_llm_graph(client: OpenAI, mcp_client: StdioMCPClient, model_name: str, base_url: Optional[str]):
    graph = StateGraph(LlmState)
    runtime_config = {
        "client": client,
        "mcp_client": mcp_client,
        "model_name": model_name,
        "base_url": base_url,
        "request_timeout": float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120")),
    }

    graph.add_node("llm_step", lambda state: _llm_step_node(state, runtime_config))
    graph.add_node("run_tools", lambda state: _tool_step_node(state, runtime_config))
    graph.add_node("validate_output", lambda state: _validate_output_node(state, runtime_config))

    graph.add_edge(START, "llm_step")
    graph.add_conditional_edges(
        "llm_step",
        _route_after_llm_step,
        {
            "done": END,
            "run_tools": "run_tools",
            "validate_output": "validate_output",
            "llm_step": "llm_step",
        },
    )
    graph.add_edge("run_tools", "llm_step")
    graph.add_conditional_edges(
        "validate_output",
        _route_after_validate,
        {"done": END, "llm_step": "llm_step"},
    )
    return graph.compile()


def run_llm_agent(url: str, model: str = None, business: str = DEFAULT_BUSINESS) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required.")

    model_name = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")
    if not base_url and model_name.lower().startswith("qwen"):
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    _trace(
        "run start: business={}, model={}, base_url={}, thinking={}".format(
            business,
            model_name,
            base_url or "<default>",
            "on" if _env_flag("LLM_ENABLE_THINKING", False) else "off",
        )
    )
    _check_llm_connectivity(client=client, model_name=model_name, base_url=base_url)
    business_config = _load_business_config(business)
    business_prompt = _load_business_prompt(business, business_config)
    fields_json_obj = business_config.get("fields_json", {})
    output_schema = business_config.get("output_schema", {})
    record_mode = str(business_config.get("record_mode", "object")).lower()
    tool_hints = business_config.get("tool_hints", {})
    data_schema_type = (((output_schema.get("properties") or {}).get("data") or {}).get("type"))
    fields_json_text = json.dumps(fields_json_obj, ensure_ascii=False)
    output_schema_text = json.dumps(output_schema, ensure_ascii=False, indent=2)
    tool_hints_text = json.dumps(tool_hints, ensure_ascii=False, indent=2)

    mcp_client = StdioMCPClient(DEFAULT_MCP_SERVER)
    _trace("mcp start: {}".format(DEFAULT_MCP_SERVER))
    mcp_client.start()

    try:
        mcp_tools = mcp_client.list_tools()
        _trace_block("mcp tools", mcp_tools)
        if not mcp_tools:
            raise RuntimeError("No tools found from MCP server.")

        tools = _to_openai_tools(mcp_tools)
        if not tools:
            raise RuntimeError("No tools available.")
        system_prompt = (
            "You are a web extraction agent. "
            "You must use available tools to extract structured data for the given URL. "
            "Choose tools yourself based on the URL, page content, business goal, and tool descriptions. "
            "MCP tools are low-level content tools, not business extractors: "
            "use get_html_content for web pages and parse_pdf_content for PDF documents. "
            "When get_html_content returns network_json, inspect those response bodies as page content. "
            "If returned content reveals same-site HTML, JSON, PDF, or JavaScript URLs that are needed for extraction, you may call tools on those same-site URLs. "
            "You are responsible for interpreting returned text/tables and producing the final business JSON. "
            "Return strict JSON that matches the provided output schema exactly."
        )
        if data_schema_type == "array" or record_mode == "array":
            system_prompt += (
                " If output data is an array of records, choose the tool from the actual source shape and tool descriptions. "
                "Ensure each row item strictly matches required fields."
            )
        user_prompt = (
            "Target URL: {}\n"
            "Business: {}\n"
            "Business prompt:\n{}\n\n"
            "record_mode: {}\n"
            "tool_hints:\n{}\n\n"
            "fields_json (target business fields; do not pass it to MCP tools unless a tool explicitly asks for it):\n{}\n\n"
            "Call the appropriate MCP content tool, then extract the final JSON yourself from returned text/tables. "
            "Use tool_hints only as business constants or runtime options, not as a tool-routing directive.\n\n"
            "Do not invent or modify schema fields for tool arguments; always use the provided schema JSON.\n\n"
            "Output JSON schema:\n{}\n"
            "You can call tools multiple times to complete extraction."
        ).format(
            url,
            business,
            business_prompt,
            record_mode,
            tool_hints_text,
            fields_json_text,
            output_schema_text,
        )

        llm_graph = _build_llm_graph(
            client=client,
            mcp_client=mcp_client,
            model_name=model_name,
            base_url=base_url,
        )
        final_state = llm_graph.invoke(
            {
                "url": url,
                "business": business,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "tools": tools,
                "output_schema": output_schema,
                "called_tools": False,
                "pending_tool_calls": [],
                "last_content": "",
                "step_count": 0,
                "record_mode": record_mode,
                "fields_json_text": fields_json_text,
                "tool_hints": tool_hints,
            }
        )

        if final_state.get("final_result") is not None:
            return final_state["final_result"]
        if final_state.get("fallback_result") is not None:
            return final_state["fallback_result"]
        raise RuntimeError("LLM graph ended without result.")
    finally:
        _trace("mcp stop")
        mcp_client.stop()


def _parse_json_content(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return json.loads(match.group(1))

    match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(1))

    raise ValueError("No JSON object found in LLM response.")


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads((text or "").strip())
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _has_meaningful_data(data: Any) -> bool:
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                if any(value not in (None, "", []) for value in row.values()):
                    return True
            elif row not in (None, "", []):
                return True
        return False
    if isinstance(data, dict):
        return any(value not in (None, "", []) for value in data.values())
    return data not in (None, "", [])


def _build_debug_trace(messages: List[Dict[str, Any]], tail: int = 12) -> List[Dict[str, Any]]:
    """
    Keep a compact trace of recent assistant/tool turns for max-turn diagnosis.
    """
    compact: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role not in {"assistant", "tool", "user"}:
            continue
        entry: Dict[str, Any] = {"role": role}
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            for call in msg.get("tool_calls", []):
                fn = ((call.get("function") or {}).get("name")) or ""
                args = ((call.get("function") or {}).get("arguments")) or ""
                calls.append({"name": fn, "arguments": str(args)[:300]})
            entry["tool_calls"] = calls
            entry["content"] = str(msg.get("content", ""))[:300]
        else:
            entry["content"] = str(msg.get("content", ""))[:500]
        compact.append(entry)
    return compact[-tail:]


def _check_llm_connectivity(client: OpenAI, model_name: str, base_url: Optional[str]) -> None:
    """
    Fail fast with explicit diagnostics before entering tool-loop.
    """
    try:
        _trace("llm connectivity check start")
        started = time.time()
        client.chat.completions.create(
            **_chat_completion_kwargs(
                model_name=model_name,
                base_url=base_url,
                model=model_name,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
                timeout=float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120")),
            )
        )
        _trace("llm connectivity check done in {:.1f}s".format(time.time() - started))
    except APIConnectionError as exc:
        raise RuntimeError(
            "LLM network connection failed. model={}, base_url={}. "
            "Please check proxy/firewall/network policy. original_error={}".format(
                model_name, base_url or "<default>", exc
            )
        ) from exc
    except AuthenticationError as exc:
        raise RuntimeError(
            "LLM authentication failed. Please verify OPENAI_API_KEY / OPENAI_BASE_URL. "
            "model={}, base_url={}, original_error={}".format(
                model_name, base_url or "<default>", exc
            )
        ) from exc
    except APIStatusError as exc:
        raise RuntimeError(
            "LLM HTTP error before extraction loop. status_code={}, model={}, base_url={}, error={}".format(
                getattr(exc, "status_code", None), model_name, base_url or "<default>", exc
            )
        ) from exc
