"""Strategy loader — discovers and loads strategy modules."""

import importlib
import importlib.util
import os

_STRATEGIES_DIR = os.path.dirname(os.path.abspath(__file__))


def load_strategy(name):
    """Load a strategy by name or absolute file path."""
    if os.path.isfile(name):
        spec = importlib.util.spec_from_file_location(
            "custom_strategy", os.path.abspath(name)
        )
        if spec is None:
            raise ImportError(f"Cannot create module spec from {name!r}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(f"strategies.{name}")

    if hasattr(mod, "Strategy"):
        cls = mod.Strategy
    else:
        from strategies.base import BaseStrategy

        cls = None
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseStrategy)
                and attr is not BaseStrategy
            ):
                cls = attr
                break

    if cls is None:
        raise ValueError(
            f"No Strategy class found in '{name}'. "
            f"Define a top-level `Strategy` class inheriting from BaseStrategy."
        )

    return cls()


def list_strategies():
    """Return sorted list of available strategy names."""
    return sorted(
        f[:-3]
        for f in os.listdir(_STRATEGIES_DIR)
        if f.endswith(".py")
        and f not in ("__init__.py", "base.py")
        and not f.startswith("_")
    )
