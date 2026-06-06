"""Star Citizen mission tracker package.

Tails the game's Game.log, models accepted missions, and serves a dashboard that
groups cargo by route. See the top-level tracker.py for the CLI entry point.
"""

__all__ = ["config", "patterns", "model", "state", "overrides", "ships", "catalogs", "snapshot", "tailer", "server"]
