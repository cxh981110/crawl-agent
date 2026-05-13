import json
import os
import re
from pathlib import Path
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
DEFAULT_BUSINESS = "company_profile"
MAX_TURNS = 8


load_dotenv(PROJECT_ROOT / ".env")


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
    source_hint: str
    fields_json_text: str
    tool_hints: Dict[str, Any]


def _load_business_config(business: str) -> Dict[str, Any]:
    config_path = BUSINESS_DIR / "{}.json".format(business)
    if not config_path.exists():
        raise RuntimeError("Business config not found: {}".format(config_path))
    # Be tolerant to UTF-8 BOM from editors/PowerShell.
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


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

    response = config["client"].chat.completions.create(
        model=config["model_name"],
        messages=state["messages"],
        tools=state["tools"],
        tool_choice="auto",
        temperature=0,
    )
    message = response.choices[0].message
    tool_calls = message.tool_calls or []

    #先取出历史数据
    updated_messages = list(state["messages"])
    if tool_calls:
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
    for tool_call in state.get("pending_tool_calls", []):
        args_text = tool_call.get("arguments") or "{}"
        try:
            args = json.loads(args_text)
            tool_name = tool_call["name"]

            # Enforce stable arguments to prevent endless LLM tool-arg drift.
            if tool_name == "extract_fields":
                args["fields_json"] = state.get("fields_json_text", args.get("fields_json", "{}"))
            elif tool_name in {"extract_table_rows", "extract_list_rows"}:
                args["row_schema_json"] = state.get("fields_json_text", args.get("row_schema_json", "{}"))
                tool_hints = state.get("tool_hints", {}) or {}
                if tool_name == "extract_table_rows" and "table_hint" in tool_hints and "table_hint" not in args:
                    args["table_hint"] = tool_hints["table_hint"]
                if tool_name == "extract_list_rows":
                    for key in ("list_selector", "item_selector", "date_regex", "max_items"):
                        if key in tool_hints and key not in args:
                            args[key] = tool_hints[key]

            tool_output = config["mcp_client"].call_tool(tool_call["name"], args)
            parsed_output = _try_parse_json(tool_output)
            if parsed_output is not None and _has_meaningful_data(parsed_output.get("data")):
                final_result = {
                    "url": parsed_output.get("url", state["url"]),
                    "strategy": tool_name,
                    "data": parsed_output.get("data"),
                    "missing_fields": parsed_output.get("missing_fields", []),
                    "evidence": parsed_output.get("evidence", {}),
                }
        except Exception as exc:
            tool_output = (
                "TOOL_ERROR: invalid tool arguments for {}. "
                "arguments={}, error={}"
            ).format(tool_call["name"], args_text, exc)
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


def _validate_output_node(state: LlmState, config: Dict[str, Any]) -> LlmState:
    content = state.get("last_content") or "{}"
    try:
        result = _parse_json_content(content)
        validate(instance=result, schema=state["output_schema"])
        return {"final_result": result}
    except ValidationError as schema_err:
        messages = list(state["messages"])
        messages.append(
            {
                "role": "user",
                "content": "Output schema validation failed: {}. Return valid JSON only.".format(schema_err.message),
            }
        )
        return {"messages": messages, "last_content": ""}
    except Exception:
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


def _build_llm_graph(client: OpenAI, mcp_client: StdioMCPClient, model_name: str):
    graph = StateGraph(LlmState)
    runtime_config = {
        "client": client,
        "mcp_client": mcp_client,
        "model_name": model_name,
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
    _check_llm_connectivity(client=client, model_name=model_name, base_url=base_url)
    business_config = _load_business_config(business)
    business_prompt = business_config.get("prompt", "")
    fields_json_obj = business_config.get("fields_json", {})
    output_schema = business_config.get("output_schema", {})
    record_mode = str(business_config.get("record_mode", "object")).lower()
    source_hint = str(business_config.get("source_hint", "auto")).lower()
    tool_hints = business_config.get("tool_hints", {})
    data_schema_type = (((output_schema.get("properties") or {}).get("data") or {}).get("type"))
    fields_json_text = json.dumps(fields_json_obj, ensure_ascii=False)
    output_schema_text = json.dumps(output_schema, ensure_ascii=False, indent=2)
    tool_hints_text = json.dumps(tool_hints, ensure_ascii=False, indent=2)

    mcp_client = StdioMCPClient(DEFAULT_MCP_SERVER)
    mcp_client.start()

    try:
        mcp_tools = mcp_client.list_tools()
        if not mcp_tools:
            raise RuntimeError("No tools found from MCP server.")

        # Expose only relevant tools for the current business mode/hint.
        allowed_tool_names = set()
        if (data_schema_type == "array" or record_mode == "array") and source_hint == "table":
            allowed_tool_names = {"extract_table_rows"}
        elif (data_schema_type == "array" or record_mode == "array") and source_hint == "list":
            allowed_tool_names = {"extract_list_rows"}
        elif data_schema_type == "array" or record_mode == "array":
            allowed_tool_names = {"extract_table_rows", "extract_list_rows"}
        else:
            allowed_tool_names = {"extract_fields"}

        filtered_mcp_tools = [tool for tool in mcp_tools if tool.get("name") in allowed_tool_names]
        tools = _to_openai_tools(filtered_mcp_tools)
        if not tools:
            raise RuntimeError("No allowed tools for current business mode/hint.")
        system_prompt = (
            "You are a web extraction agent. "
            "You must use available tools to extract structured data for the given URL. "
            "Prefer API/network-derived data before HTML parsing. "
            "Page fetch and HTML cleaning are fixed internal steps in extraction tools. "
            "Use extract_fields for object extraction, and use extract_table_rows/extract_list_rows for array extraction. "
            "Return strict JSON that matches the provided output schema exactly."
        )
        if data_schema_type == "array" or record_mode == "array":
            system_prompt += (
                " If output data is an array of records, choose extraction tool by source_hint: "
                "table -> extract_table_rows, list -> extract_list_rows, auto -> try list/table and pick better coverage. "
                "Ensure each row item strictly matches required fields."
            )
        user_prompt = (
            "Target URL: {}\n"
            "Business: {}\n"
            "Business prompt:\n{}\n\n"
            "record_mode: {}\n"
            "source_hint: {}\n"
            "tool_hints:\n{}\n\n"
            "fields_json (pass this string to extract_fields fields_json argument):\n{}\n\n"
            "If extracting array rows, pass this same JSON string to extract_table_rows/extract_list_rows as row_schema_json.\n"
            "When source_hint=list and tool_hints has selectors, pass them to extract_list_rows.\n"
            "When source_hint=table and tool_hints has table_hint, pass it to extract_table_rows.\n\n"
            "Do not invent or modify schema fields for tool arguments; always use the provided schema JSON.\n\n"
            "Output JSON schema:\n{}\n"
            "You can call tools multiple times to complete extraction."
        ).format(
            url,
            business,
            business_prompt,
            record_mode,
            source_hint,
            tool_hints_text,
            fields_json_text,
            output_schema_text,
        )

        llm_graph = _build_llm_graph(client=client, mcp_client=mcp_client, model_name=model_name)
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
                "source_hint": source_hint,
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
        client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
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
