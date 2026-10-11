"""Convert ToonSquid ``.tsproj`` projects into Adobe Animate ``.fla`` files."""

from .converter import convert
from .tsproj import load_project

__all__ = ["convert", "load_project"]
__version__ = "0.3.0"
