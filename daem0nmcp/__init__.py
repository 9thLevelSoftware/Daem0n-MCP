"""
Daem0nMCP Core Package
"""

try:
    from importlib.metadata import version as _get_version

    __version__ = _get_version("daem0nmcp")
except Exception:
    # Fallback for development or if package not installed
    __version__ = "6.6.6"
