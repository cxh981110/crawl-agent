import argparse
import json
import sys

from agent.llm_agent import run_llm_agent



def main() -> int:
    # if len(sys.argv) < 2:
    #     print('Usage: python -m agent.llm_cli "<url>" [--business <name>] [--model <name>]', file=sys.stderr)
    #     return 1

    url = "https://www.cindasc.com/osoa/views/xdyw/zcgl/djh/cpxq/index.html?product_id=4&product_code=970022&product_type=4"
    business = "fund_snapshot"
    parser = argparse.ArgumentParser(description="Run LLM + MCP extraction agent.")
    parser.add_argument("url", nargs="?", default=url, help="Target page URL")
    parser.add_argument("--business", default=business, help="Business config name in businesses/*.json")
    parser.add_argument("--model", default=None, help="Optional model name override")
    args = parser.parse_args()

    try:
        result = run_llm_agent(url=args.url, model=args.model, business=args.business)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print("LLM agent failed: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
