import os
import sys
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor

from mcp.server.fastmcp import FastMCP

from .driver import ChatGPTDriver, ChatGPTDriverError, NeedLoginError


mcp = FastMCP(
    "chatgpt",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)
executor = ThreadPoolExecutor(max_workers=1)


def _ask_with_protocol_safe_logs(prompt, mode="auto", timeout_ms=None):
    with redirect_stdout(sys.stderr):
        driver = ChatGPTDriver(timeout_ms=timeout_ms) if timeout_ms else ChatGPTDriver()
        return driver.ask(prompt, mode=mode)


def _status_with_protocol_safe_logs():
    with redirect_stdout(sys.stderr):
        return ChatGPTDriver().status()


def _create_account_with_protocol_safe_logs():
    with redirect_stdout(sys.stderr):
        return ChatGPTDriver().create_account_once()


def chatgpt_ask(prompt: str) -> str:
    """Ask ChatGPT through the local authenticated web session.

    This tool does not use an API key. It launches the configured browser
    profile, loads `.chatgpt-cookies.json` when present, sends the prompt in
    ChatGPT's web UI, and returns the latest assistant message text.
    """
    try:
        return executor.submit(_ask_with_protocol_safe_logs, prompt).result()
    except NeedLoginError as exc:
        return f"LOGIN_REQUIRED: {exc}"
    except ChatGPTDriverError as exc:
        return f"CHATGPT_WEB_ERROR: {exc}"


def chatgpt_status() -> dict:
    """Return the detected ChatGPT web session mode without sending a prompt."""
    try:
        return executor.submit(_status_with_protocol_safe_logs).result()
    except ChatGPTDriverError as exc:
        return {"error": str(exc)}


def chatgpt_search(prompt: str) -> str:
    """Ask ChatGPT with the web search tool selected in the web UI."""
    try:
        return executor.submit(_ask_with_protocol_safe_logs, prompt, "search", 180000).result()
    except NeedLoginError as exc:
        return f"LOGIN_REQUIRED: {exc}"
    except ChatGPTDriverError as exc:
        return f"CHATGPT_WEB_ERROR: {exc}"


def chatgpt_reason(prompt: str) -> str:
    """Ask ChatGPT with reasoning mode selected in the web UI."""
    try:
        return executor.submit(_ask_with_protocol_safe_logs, prompt, "reason", 240000).result()
    except NeedLoginError as exc:
        return f"LOGIN_REQUIRED: {exc}"
    except ChatGPTDriverError as exc:
        return f"CHATGPT_WEB_ERROR: {exc}"


def chatgpt_deep_research(prompt: str) -> str:
    """Ask ChatGPT with deep research selected in the web UI.

    This can be much slower than normal chat because ChatGPT may run a multi-step
    research workflow before returning an answer.
    """
    try:
        return executor.submit(
            _ask_with_protocol_safe_logs,
            prompt,
            "deep_research",
            120000,
        ).result()
    except NeedLoginError as exc:
        return f"LOGIN_REQUIRED: {exc}"
    except ChatGPTDriverError as exc:
        return f"CHATGPT_WEB_ERROR: {exc}"


def chatgpt_create_account() -> dict:
    """Create one persistent ChatGPT account when no authenticated session exists.

    This is a one-time bootstrap helper. It does not rotate accounts and does
    not create another account when ChatGPT later reports a message limit.
    """
    try:
        return executor.submit(_create_account_with_protocol_safe_logs).result()
    except ChatGPTDriverError as exc:
        return {"error": str(exc)}


def main():
    mcp.tool()(chatgpt_ask)
    mcp.tool()(chatgpt_search)
    mcp.tool()(chatgpt_reason)
    mcp.tool()(chatgpt_deep_research)
    mcp.tool()(chatgpt_status)
    mcp.tool()(chatgpt_create_account)

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        sys.exit("ERROR: chatgpt MCP only supports stdio transport.")

    mcp.run()


if __name__ == "__main__":
    main()
