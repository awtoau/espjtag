"""`python -m espjtag.mcp` — run the espjtag MCP server (stdio transport).

This is the entry point an MCP client (Claude Code, the planned VS Code
extension #15) launches. The actual tool definitions live in
espjtag.mcp_server; this module just forwards to its main() so the run command
is the short, memorable `python -m espjtag.mcp`. The mcp SDK is imported lazily
inside build_server(), so importing espjtag itself never requires it."""

from .mcp_server import main

if __name__ == "__main__":
    main()
