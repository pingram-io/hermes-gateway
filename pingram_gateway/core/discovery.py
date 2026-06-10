"""Ensure Hermes plugin platforms are registered before tool/session hooks run."""


def ensure_plugins_discovered() -> None:
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:
        pass
