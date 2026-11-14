"""Post-load normalization passes.

This is where parser quirks are flattened into stable domain data:
inheritance walks, variant resolution, palette mapping, placement
normalization, etc. The Blender addon must never need to do this work.
"""
