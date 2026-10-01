"""opptions: free-data research trackers for options and swing trading.

Each tracker module (gex, flow, news, earnings) exposes the same three
functions so the CLI, the packet builder and the agents can use them alike:

    fetch(symbol: str) -> dict        # network: raw data from free sources
    analyze(raw: dict) -> dict        # pure: no network, unit-tested on fixtures
    to_markdown(result: dict) -> str  # human/agent-readable section
"""

__version__ = "0.1.0"

TRACKERS = ("gex", "flow", "news", "earnings")
