"""Root tools package and namespace bridge for agent tools."""
from pathlib import Path
import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)
_root = Path(__file__).resolve().parent.parent

for _p in [
    _root / "agent-template" / "tools",
    _root / "agents" / "pm" / "tools",
]:
    if _p.exists() and str(_p) not in __path__:
        __path__.append(str(_p))
