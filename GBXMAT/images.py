"""Image loading & decoding.

GBXMAT owns this layer because Blender's built-in DDS reader is unreliable
across versions and silently mishandles ATI1/BC4 (single-channel) and
ATI2/BC5 (two-channel) — both of which Trackmania uses heavily for
``RoughMetal`` and ``Normal`` slots respectively.

The decode path:
    DDS bytes  ──► Pillow.Image  ──► RGBA buffer  ──► bpy.data.images.new()
                                                       + foreach_set + pack

Non-DDS files (``.png``, ``.tga``, ``.jpg``, ``.webp``) go through
``bpy.data.images.load`` directly — Blender handles those well.

Three pixel-level transforms happen here, before the buffer reaches Blender:
  1. Vertical flip (Pillow is top-down, Blender is bottom-up).
  2. RGBA conversion (so all images present a uniform 4-channel buffer
     to shader builders) plus a ``source_channels`` metadata int stored
     on the Image datablock — needed by ``RoughMetal`` builders to know
     whether the green channel is real or padding.
  3. Optional normal-map green-channel flip (DirectX Y → OpenGL Y).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from typing import Optional

try:                # delayed import; Blender supplies this
    import bpy
except Exception:   # pragma: no cover — non-Blender contexts (tests)
    bpy = None       # type: ignore[assignment]


log = logging.getLogger("gbxmat.images")


# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

_max_texture_size: int = 0  # 0 = unrestricted

# Diagnostic timers (read + reset by callers).
DDS_TIMINGS: dict = {
    "decode": 0.0,    # Pillow open/convert/resize
    "convert": 0.0,   # numpy float buffer build
    "upload": 0.0,    # bpy.data.images.new + foreach_set + pack
    "calls": 0,       # DDS files actually decoded
    "pixels": 0,
    "cache_hits": 0,  # bpy.data.images datablock reused
    "native": 0,      # non-DDS loaded via bpy.data.images.load
}


def set_max_texture_size(value: int) -> None:
    """Clamp the longest dimension of any subsequently-decoded texture.

    ``0`` (the default) keeps the image at its original resolution. Any
    positive value triggers a Pillow ``BILINEAR`` downscale for DDS
    images that exceed it. Non-DDS images are not downscaled here —
    callers can use ``image.scale()`` after the fact if needed.
    """
    global _max_texture_size
    _max_texture_size = max(0, int(value))


def get_max_texture_size() -> int:
    return _max_texture_size


# ``(path) -> bool`` existence check; the addon plugs in a check that can
# fetch missing game files (GBXBPY.library.ensure).
_file_check = os.path.isfile


def set_file_check(check) -> None:
    global _file_check
    _file_check = check or os.path.isfile


# ``(path) -> path`` remap applied before a texture is loaded or a material
# is named; the map importer plugs in a texture mod's replacements.
_path_override = None


def set_path_override(override) -> None:
    global _path_override
    _path_override = override


def map_path(path: str) -> str:
    """Return the texture file actually used for ``path``."""
    if _path_override is None or not path:
        return path
    try:
        return _path_override(path) or path
    except Exception:  # noqa: BLE001 - a broken override must not break materials
        log.exception("texture path override failed for %s", path)
        return path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_image(path: str, role: str, *, flip_normal_green: bool = True):
    """Load an image from disk and return the resulting ``bpy.types.Image``.

    Returns ``None`` on any failure (file missing, decode failure,
    unsupported format). Builders should fall back to a magenta error
    texture in that case.

    Identical ``(path, role)`` calls return the cached datablock — uses
    Blender's own ``bpy.data.images`` keyed by name as the cache.
    """
    if bpy is None:
        return None
    if not path:
        return None

    path = map_path(path)
    name = _image_name(path, role)

    # Reuse an existing Image datablock if one with this name already
    # exists. We trust the name signature; rebuilding identical pixel
    # data on every placement would be wasteful.
    existing = bpy.data.images.get(name)
    if existing is not None:
        DDS_TIMINGS["cache_hits"] += 1
        return existing

    if not _file_check(path):
        log.debug("texture missing: %s", path)
        return None

    ext = os.path.splitext(path)[1].lower()
    if ext == ".dds":
        return _load_dds(path, name, role, flip_normal_green=flip_normal_green)
    DDS_TIMINGS["native"] += 1
    return _load_via_blender(path, name, role)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _image_name(path: str, role: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    # Same-named files in different folders (every skin pack ships a
    # ``PZ1.dds``) must not share a datablock, so tag the folder.
    folder = os.path.normcase(os.path.dirname(os.path.abspath(path)))
    tag = hashlib.blake2s(folder.encode("utf-8", "replace"), digest_size=3).hexdigest()
    # Role suffix prevents two materials that disagree about role
    # (e.g. one wants the green-flipped normal, another doesn't) from
    # accidentally sharing a datablock.
    return f"{base}~{tag}__{role}" if role else f"{base}~{tag}"


def _load_via_blender(path: str, name: str, role: str):
    """``.png/.jpg/.tga/.webp`` etc. — Blender handles these natively."""
    img = bpy.data.images.load(path, check_existing=True)
    img.name = name
    _set_colorspace_for_role(img, role)
    if (
        _max_texture_size > 0
        and getattr(img, "source", "") != "MOVIE"  # scale() would only touch one frame
        and img.size[0] > 0
        and img.size[1] > 0
        and max(img.size[0], img.size[1]) > _max_texture_size
    ):
        ratio = _max_texture_size / max(img.size[0], img.size[1])
        new_w = max(1, round(img.size[0] * ratio))
        new_h = max(1, round(img.size[1] * ratio))
        try:
            img.scale(new_w, new_h)
        except Exception as e:  # noqa: BLE001
            log.debug("non-DDS scale failed for %s: %s", path, e)
    return img


# DX10 sRGB block formats -> the same layout without the sRGB flag. Some
# Pillow versions only decode the UNORM ids; colour space is set per role.
_DXGI_SRGB_TO_UNORM = {29: 28, 72: 71, 75: 74, 78: 77, 99: 98}


def _open_dds(pil_image, path: str):
    """``PIL.Image.open`` for a DDS, retrying sRGB DX10 files as UNORM."""
    try:
        img = pil_image.open(path)
        img.load()
        return img
    except (NotImplementedError, OSError, ValueError) as exc:
        with open(path, "rb") as handle:
            data = bytearray(handle.read())
        if len(data) < 148 or data[84:88] != b"DX10":
            raise
        fmt = int.from_bytes(data[128:132], "little")
        if fmt not in _DXGI_SRGB_TO_UNORM:
            raise
        log.debug("decoding sRGB DXGI format %d of %s as UNORM (%s)", fmt, path, exc)
        data[128:132] = _DXGI_SRGB_TO_UNORM[fmt].to_bytes(4, "little")
        img = pil_image.open(io.BytesIO(bytes(data)))
        img.load()
        return img


def _load_dds(path: str, name: str, role: str, *, flip_normal_green: bool):
    """DDS via Pillow → packed Blender Image datablock."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        log.error(
            "Pillow not installed — cannot decode DDS texture %s. "
            "Install via the addon's dependency installer.",
            path,
        )
        return None

    try:
        import numpy as np
    except ImportError:
        np = None  # fall back to slow path

    import time as _time
    _t0 = _time.perf_counter()
    try:
        img = _open_dds(PILImage, path)
        # Track original channel count *before* RGBA conversion so
        # builders know whether the G/B channels are meaningful
        # (BC4/ATI1 → "L", BC5/ATI2 → "RGB" with B unused).
        _mode_channels = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4, "P": 1}
        source_channels = _mode_channels.get(img.mode, len(img.getbands()))

        img = img.convert("RGBA")

        if _max_texture_size > 0 and max(img.width, img.height) > _max_texture_size:
            ratio = _max_texture_size / max(img.width, img.height)
            new_w = max(1, round(img.width * ratio))
            new_h = max(1, round(img.height * ratio))
            img = img.resize((new_w, new_h), PILImage.BILINEAR)

        width, height = img.width, img.height
        DDS_TIMINGS["decode"] += _time.perf_counter() - _t0

        _t1 = _time.perf_counter()
        if np is not None:
            arr = np.frombuffer(img.tobytes(), dtype=np.uint8)
            arr = arr.reshape((height, width, 4))
            arr = arr[::-1, :, :]
            if flip_normal_green and role == "Normal":
                arr = arr.copy()
                arr[:, :, 1] = 255 - arr[:, :, 1]
            floats = (arr.astype(np.float32) * (1.0 / 255.0)).ravel()
        else:
            img = img.transpose(PILImage.FLIP_TOP_BOTTOM)
            if flip_normal_green and role == "Normal":
                r, g, b, a = img.split()
                from PIL import ImageChops
                g = ImageChops.invert(g)
                img = PILImage.merge("RGBA", (r, g, b, a))
            import array
            pixel_bytes = img.tobytes()
            floats = array.array("f", (b / 255.0 for b in pixel_bytes))
        DDS_TIMINGS["convert"] += _time.perf_counter() - _t1
    except Exception as exc:  # noqa: BLE001 — Pillow throws assorted errors
        log.warning("DDS decode failed for %s: %s", path, exc)
        return None

    _t2 = _time.perf_counter()
    bimg = bpy.data.images.new(name, width=width, height=height, alpha=True)
    bimg.pixels.foreach_set(floats)
    bimg.pack()
    DDS_TIMINGS["upload"] += _time.perf_counter() - _t2
    DDS_TIMINGS["calls"] += 1
    DDS_TIMINGS["pixels"] += width * height
    bimg["source_channels"] = source_channels
    _set_colorspace_for_role(bimg, role)
    return bimg


def _set_colorspace_for_role(img, role: str) -> None:
    """Set ``image.colorspace_settings.name`` per role.

    sRGB for color-bearing slots; Non-Color for data textures.
    """
    try:
        cs = img.colorspace_settings
    except AttributeError:
        return
    # HueMask RGB is sampled through an sRGB view in-game (alpha stays linear).
    color_roles = {"Diffuse", "Emissive", "HueMask"}
    if role in color_roles:
        cs.name = "sRGB"
    else:
        cs.name = "Non-Color"


__all__ = ["load_image", "set_max_texture_size", "get_max_texture_size"]
