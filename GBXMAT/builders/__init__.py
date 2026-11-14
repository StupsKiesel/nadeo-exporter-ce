"""Bundled shader-family builders.

Each module exposes a single ``build(spec) -> bpy.types.Material``
function and is auto-registered by :func:`gbxmat.registry._bootstrap_builtin_families`.

Add a new family:
  1. Create ``gbxmat/builders/<your_family>.py`` with a ``build`` function.
  2. Register it in :func:`gbxmat.registry._bootstrap_builtin_families`,
     or call :func:`gbxmat.register_family` from downstream code.
"""
