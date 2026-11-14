"""Raw file I/O: opening .Gbx files and invoking the NICE parser.

This subpackage is intentionally thin. It does not interpret class ids or
build domain objects — that is the job of :mod:`GBXBPY.load`.
"""

from .loader import read_gbx, read_gbx_bytes

__all__ = ["read_gbx", "read_gbx_bytes"]
