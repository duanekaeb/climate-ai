"""Climate AI's Claude analyst: the agent service and the MCP server.

Claude reads the house's analytics through the API, signs off or holds model-queued changes
inside pre-approved ranges, proposes changes and experiments, and writes reports. It is never
in the control path and has no tool that reaches a thermostat. Runs bill the owner's Claude
subscription (``CLAUDE_CODE_OAUTH_TOKEN``), never an API key.
"""

__version__ = "0.1.0"
