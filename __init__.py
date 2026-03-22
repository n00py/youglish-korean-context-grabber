from __future__ import annotations


def _bootstrap() -> None:
    try:
        from .ui.actions import install_hooks
    except Exception:
        return

    install_hooks(__name__)


_bootstrap()
