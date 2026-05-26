# Global MCP Tools Test Script

This directory contains a reusable smoke test that connects to the global MCP proxy and exercises **every tool** exposed by all upstream MCP servers. It is intentionally conservative: it initializes the proxy, calls `tools/list`, and then calls each tool once so you can verify connectivity, session handling, and argument plumbing from a single script. The script uses the official `mcp` Python client with a Streamable HTTP transport so every call happens over a proper MCP session (initialize → tools/list → tools/call).

## Usage

1. Install dependencies via Poetry (run this from the `global/mcp/scripts` folder):

   ```bash
   cd global/mcp/scripts
   poetry install
   ```

2. Run the script using Poetry (still inside `global/mcp/scripts`):

   ```bash
   poetry run python test_all_mcp_tools.py
   ```

   The script reads the Cursor MCP config (`~/.cursor/mcp.json` by default) to find the proxy URL and the `X-API-Key` header. It supports two overrides:

   - `MCP_PROXY_URL`: explicitly set the proxy URL.
   - `MCP_API_KEY`: override the API key header value (header name is always `X-API-Key`).

   If the script cannot locate the config or headers, it exits with an error.

3. The script prints one line per tool (`<tool name>: ok` or `<tool name>: <error message>`), followed by a summary of how many tools were called and how many returned errors. It exits with code `0` unless there were transport/session failures (e.g., unable to initialize or send a request).

## Extending tests for new tools

- Update the `SAFE_TOOL_ARGS` dictionary inside `test_all_mcp_tools.py` whenever a new tool requires specific arguments, so each invocation is meaningful rather than just a validation error.
- Because the script reads the proxy’s `tools/list`, it already calls every tool that the proxy advertises. If a new tool is added to an upstream server, add a matching entry to `SAFE_TOOL_ARGS` before running the script to keep the test coverage comprehensive.

Running this script is the canonical “global MCP smoke test,” so new MCP servers should integrate into it as part of their documentation and quality checks.
