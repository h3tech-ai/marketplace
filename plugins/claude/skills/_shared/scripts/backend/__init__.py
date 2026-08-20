"""Agent backend configuration for plugin-claude (Claude-only).

Usage:
    from backend import BackendConfig
    config = BackendConfig.load(project_dir)
    backend = config.get_backend("software-engineer")  # -> "claude"
"""

from .backend_config import BackendConfig, BackendConfigError

__all__ = ["BackendConfig", "BackendConfigError"]
