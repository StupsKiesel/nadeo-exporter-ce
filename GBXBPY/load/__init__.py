"""Per-class-id loaders that turn parsed NICE data into :mod:`GBXBPY.model` objects.

Each loader registers itself with :mod:`GBXBPY.registry` so dispatch is by
GBX class id rather than long ``if/elif`` chains.

Importing this package triggers all loader registrations as a side effect.
"""

# Importing each loader module fires its @register decorator.
from . import material  # noqa: F401
from . import mesh  # noqa: F401
from . import solid  # noqa: F401
from . import scene  # noqa: F401
from . import item  # noqa: F401
from . import veget_tree  # noqa: F401
from . import prefab  # noqa: F401
from . import block  # noqa: F401
from . import clip  # noqa: F401
from . import map  # noqa: F401
from . import funcshader  # noqa: F401
from . import decoration  # noqa: F401
from . import gameskin  # noqa: F401
from . import zone  # noqa: F401
from . import particle  # noqa: F401
from . import fx  # noqa: F401
from . import kinematic  # noqa: F401
from . import placement  # noqa: F401
from . import replay  # noqa: F401

__all__: list[str] = []
