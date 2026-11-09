"""Launch-time choices the engine reads once, when it is imported.

A launcher calls configure() *before* importing strategy.engine:

    from strategy import runtime
    runtime.configure(variant="v28", argv=sys.argv[1:])
    from strategy import engine
    engine.main()
"""

from __future__ import annotations

VARIANT: str | None = None
CLI_ARGV: list[str] = []


def configure(variant: str, argv: list[str] | None = None) -> None:
    global VARIANT, CLI_ARGV
    VARIANT = variant
    CLI_ARGV = list(argv or [])


def require_variant() -> str:
    if not VARIANT:
        raise RuntimeError(
            "strategy.engine needs a variant: call strategy.runtime.configure(variant=...) "
            "before importing it, or start a launcher such as scripts/v28_strategy_main.py"
        )
    return VARIANT
