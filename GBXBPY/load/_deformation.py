"""Deformation-layer evaluator (path-along-curve).

The CPlugCrystal Deformation layer bends/stretches a base Geometry mesh along
a piecewise spline defined by a series of GbxIso4 control frames. This module
takes the parsed Deformation struct (see ``NICE/src/gbx_structs.py``) and a
list of base-mesh vertices in raw GBX coords, and returns the deformed
positions (still in raw GBX coords).

Reverse-engineering notes (see also memory: deformation-layer):

* Knot 0 of the spline = ``start_iso``. Knots 1..n = ``controls[0..n-1]``.
  Verified end-to-end on ``Base_def_3control``: gbx delta from start to last
  control = (0, 8, 28) m, matching NinjaRipper-extracted deformed mesh
  centroid delta exactly.
* The deformation parameter axis is the source mesh's local +Z (the
  engine's "length" axis). The world direction this maps to is encoded
  in ``start_iso``'s rotation column 2 — authors can pick any axis via
  the in-editor "deformation axis" control (e.g. half-banked-curve
  variants deform along world X or Y instead of Z). Callers should
  pre-transform world-space verts into the source-local frame using
  :func:`world_to_source_local` before calling :func:`deform_vertices`.
* Each base vertex's gbx-Z coordinate is mapped linearly onto the knot range
  ``[0, n_knots - 1]``; its gbx-XY components are the cross-section that
  rides along the spline, rotated by the local frame.
* GbxIso4 layout is row-major / column-vector
  (``v_new[i] = sum_j R[i][j] * v[j] + T[i]``), confirmed from the
  ``Base_def_pitch`` test: rotation around X-axis writes YY/YZ/ZY/ZZ.

Interpolation:

* Translation is a cubic Hermite spline. The tangent at each knot points
  along that knot's local +Z axis, with magnitude equal to the chord
  vector ``(T_{i+1} - T_i)`` projected onto that direction. Validated
  against the ``Deformed_by_hand`` reference: mid-ring centers reproduce
  within 0.05 m for the 1-segment case (was 0.3 m off with linear lerp).
* The cross-section frame is obtained by slerping the knot rotations and
  then applying a minimum-rotation correction so its local +Z aligns with
  the actual curve tangent (so cross-sections stay perpendicular to the
  spine and tilt smoothly through bends).

Open items:

* The parsed ``bounds`` floats are currently ignored; current chord-projection
  magnitude works for the validated cases.
* The parsed mask identifies selected groups in the bound source layer.
  Binding one deformation to multiple source layers is not implemented.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, List, Sequence, Tuple

log = logging.getLogger("gbxbpy.load.deformation")


# ---------------------------------------------------------------------------
# Legacy raw-blob decoder
# ---------------------------------------------------------------------------
# Current NICE parses counted masks and spline frames directly. These helpers
# remain for older raw containers with ``_unsupported_v3plus`` payloads.
# The value historically called modifier_version was actually a mask count.
# The helpers below scan legacy bytes for GbxIso4 frames and synthesize the minimum set
# of fields the deformation evaluator needs (``start_iso``, ``controls``,
# ``n_controls``, ``mask_bits``, ``linked_layer_id``).
#
# Reverse-engineered v6 header layout (offsets relative to start of
# ``_unsupported_v3plus.bytes_until_facade``, sample = DeformationLayer.Item.Gbx):
#   0..3   : mask_bits (Int32, observed = 0)
#   4..51  : 6 × (GbxLookbackString-ref, Int32) = 48 bytes — purpose unknown
#            (may be group/region link table; sample has refs to slot 2 with
#            ints 1..5,0).
#   52..63 : 3 floats (16, 5, 32) — possibly source bbox extents.
#   64..75 : 3 floats (16, 5, 64) — possibly target bbox extents.
#   76..123: GbxIso4 ``start_iso`` (12 floats: 9-rot col-major + 3-trans).
#   124..127 : n_controls (Int32).
#   128..   : ``n_controls`` × GbxIso4 control points.
#   trailer : large packed section (~per-vertex weights/groups, not needed
#             for the spline geometry).
#
# Detection strategy: scan for the FIRST orthonormal 12-float block and
# treat that as ``start_iso``. The Int32 immediately after is ``n_controls``.
# Subsequent 48-byte chunks are validated as orthonormal; the run terminates
# at the first non-orthonormal block. This is robust to small header layout
# changes between v3/v4/v5/v6.


class _SynthesizedIso:
    """Stand-in for a parsed GbxIso4 Container, matching the attribute
    names the deformation evaluator expects."""

    __slots__ = ("XX", "XY", "XZ", "YX", "YY", "YZ", "ZX", "ZY", "ZZ",
                 "TX", "TY", "TZ")

    def __init__(self, floats):
        # GbxIso4 in NICE is stored row-major in the file
        # (XX, XY, XZ, YX, YY, YZ, ZX, ZY, ZZ, TX, TY, TZ) — see
        # NICE/src/gbx_structs.py::GbxIso4. Match that layout exactly.
        (self.XX, self.XY, self.XZ,
         self.YX, self.YY, self.YZ,
         self.ZX, self.ZY, self.ZZ,
         self.TX, self.TY, self.TZ) = floats


def _is_orthonormal_3x3_colmajor(m9, eps=1e-2) -> bool:
    """Return True if 9 floats form a (column-major) orthonormal 3x3."""
    c1 = (m9[0], m9[1], m9[2])
    c2 = (m9[3], m9[4], m9[5])
    c3 = (m9[6], m9[7], m9[8])

    def _dot(a, b):
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    n1 = _dot(c1, c1)
    n2 = _dot(c2, c2)
    n3 = _dot(c3, c3)
    if abs(n1 - 1.0) > eps or abs(n2 - 1.0) > eps or abs(n3 - 1.0) > eps:
        return False
    if (abs(_dot(c1, c2)) > eps or abs(_dot(c2, c3)) > eps
            or abs(_dot(c1, c3)) > eps):
        return False
    return True


def decode_v6_deformation(blob: bytes):
    """Decode a v3+ Deformation ``_unsupported_v3plus`` blob into the
    fields the evaluator needs. Returns a dict with keys ``start_iso``,
    ``controls``, ``n_controls``, ``mask_bits``, ``linked_layer_id`` —
    or ``None`` if no plausible spline frame was found."""
    import struct as _struct

    if not blob or len(blob) < 76 + 48 + 4 + 48:
        return None

    # Scan for the first orthonormal 12-float block (= start_iso).
    start_off = -1
    for off in range(0, min(len(blob) - 48, 4096), 4):
        floats = _struct.unpack_from("<12f", blob, off)
        if not _is_orthonormal_3x3_colmajor(floats[:9]):
            continue
        # Reject blocks where the translation is wildly out of map range
        # (avoids matching random byte runs that happen to look orthonormal).
        if any(abs(t) > 4096.0 for t in floats[9:12]):
            continue
        start_off = off
        break
    if start_off < 0:
        return None

    start_iso = _SynthesizedIso(_struct.unpack_from("<12f", blob, start_off))

    # n_controls follows start_iso.
    n_off = start_off + 48
    if n_off + 4 > len(blob):
        return None
    n_controls = _struct.unpack_from("<I", blob, n_off)[0]
    # Sanity: must fit in remaining blob and not exceed a reasonable cap.
    if n_controls == 0 or n_controls > 256:
        return None
    needed = 48 * n_controls
    if n_off + 4 + needed > len(blob):
        return None

    controls = []
    coff = n_off + 4
    for i in range(n_controls):
        floats = _struct.unpack_from("<12f", blob, coff + i * 48)
        if not _is_orthonormal_3x3_colmajor(floats[:9]):
            # Run broke earlier than n_controls says — trust the run length.
            break
        if any(abs(t) > 4096.0 for t in floats[9:12]):
            break
        controls.append(_SynthesizedIso(floats))
    if not controls:
        return None

    # mask_bits is at offset 0 in every observed v6 sample.
    mask_bits = _struct.unpack_from("<I", blob, 0)[0]

    return {
        "start_iso": start_iso,
        "controls": controls,
        "n_controls": len(controls),
        "mask_bits": mask_bits,
        "linked_layer_id": None,
    }


def _patch_v6_deformation_content(content) -> bool:
    """If ``content`` is an unparsed v3+ Deformation Container, decode the
    raw blob and attach synthesized ``start_iso``/``controls`` fields so
    the rest of the pipeline treats it like a v1/v2 layer.

    Also handles the ``_unsupported_v2plus_fallback`` blob produced by
    the schema's v2 fallback branch (e.g. RoadTech_2x4-R.Item.Gbx whose
    v2 layout differs from the simple v1/v2 schema).

    Returns True when patching succeeded (or was already done), False when
    decoding failed and the layer should be skipped.
    """
    if hasattr(content, "controls") and hasattr(content, "start_iso"):
        return True
    blob = None
    blob_kind = None
    for field_name in ("_unsupported_v3plus", "_unsupported_v2plus_fallback"):
        blob_field = getattr(content, field_name, None)
        if blob_field is None:
            continue
        candidate = getattr(blob_field, "bytes_until_facade", None)
        if candidate is not None:
            blob = candidate
            blob_kind = field_name
            break
    if blob is None:
        return False
    decoded = decode_v6_deformation(blob)
    if decoded is None:
        log.warning(
            "Deformation blob decode failed (kind=%s, modifier_version=%s, "
            "blob=%d bytes); deformation will be skipped",
            blob_kind, getattr(content, "modifier_version", "?"), len(blob),
        )
        return False
    # The v2 fallback blob's offset-0 int is a version/format field, not the
    # group-skip mask used by v6 layers. Reading it as mask_bits would mark
    # groups as "passthrough" and leave parts of the source mesh in their
    # un-deformed world-space coordinates (visible as stuck flat geometry
    # next to the curved deformed portion). Force mask=0 so every face in
    # the mesh gets deformed for v2 fallbacks.
    if blob_kind == "_unsupported_v2plus_fallback":
        decoded["mask_bits"] = 0
    # Attach synthesized fields. Containers are dict-like and also expose
    # attribute access — assigning by attribute mirrors the parsed layout.
    for k, v in decoded.items():
        try:
            setattr(content, k, v)
        except Exception:
            try:
                content[k] = v  # type: ignore[index]
            except Exception:
                return False
    log.info(
        "Deformation decoded (%s): %d controls, start T=(%.2f, %.2f, %.2f)",
        blob_kind, decoded["n_controls"],
        decoded["start_iso"].TX, decoded["start_iso"].TY, decoded["start_iso"].TZ,
    )
    return True


# ---------------------------------------------------------------------------
#  Math helpers (no numpy — avoid pulling deps just for this)
# ---------------------------------------------------------------------------

Vec3T = Tuple[float, float, float]
QuatT = Tuple[float, float, float, float]  # (x, y, z, w)
Mat3T = Tuple[Vec3T, Vec3T, Vec3T]  # rows


def _iso4_rt(iso) -> Tuple[Mat3T, Vec3T]:
    """Extract rotation 3x3 (rows) and translation Vec3 from a parsed Iso4."""
    rot: Mat3T = (
        (float(iso.XX), float(iso.XY), float(iso.XZ)),
        (float(iso.YX), float(iso.YY), float(iso.YZ)),
        (float(iso.ZX), float(iso.ZY), float(iso.ZZ)),
    )
    t: Vec3T = (float(iso.TX), float(iso.TY), float(iso.TZ))
    return rot, t


def _mat3_to_quat(R: Mat3T) -> QuatT:
    """Convert a 3x3 rotation matrix (row-major, column-vector convention) to
    a quaternion ``(x, y, z, w)``. Uses Shepperd's method for numerical
    stability across all rotation angles."""
    m00, m01, m02 = R[0]
    m10, m11, m12 = R[1]
    m20, m21, m22 = R[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (m21 - m12) / s
        qy = (m02 - m20) / s
        qz = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        qw = (m21 - m12) / s
        qx = 0.25 * s
        qy = (m01 + m10) / s
        qz = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        qw = (m02 - m20) / s
        qx = (m01 + m10) / s
        qy = 0.25 * s
        qz = (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        qw = (m10 - m01) / s
        qx = (m02 + m20) / s
        qy = (m12 + m21) / s
        qz = 0.25 * s
    return (qx, qy, qz, qw)


def _quat_to_mat3(q: QuatT) -> Mat3T:
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz),     2.0 * (xz + wy)),
        (2.0 * (xy + wz),       1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)),
        (2.0 * (xz - wy),       2.0 * (yz + wx),     1.0 - 2.0 * (xx + yy)),
    )


def _slerp(qa: QuatT, qb: QuatT, t: float) -> QuatT:
    ax, ay, az, aw = qa
    bx, by, bz, bw = qb
    d = ax * bx + ay * by + az * bz + aw * bw
    if d < 0.0:
        bx, by, bz, bw, d = -bx, -by, -bz, -bw, -d
    if d > 0.9995:
        # Near-parallel — linearly interpolate and renormalize
        rx = ax + t * (bx - ax)
        ry = ay + t * (by - ay)
        rz = az + t * (bz - az)
        rw = aw + t * (bw - aw)
    else:
        theta_0 = math.acos(max(-1.0, min(1.0, d)))
        sin_theta_0 = math.sin(theta_0)
        theta = theta_0 * t
        s1 = math.sin(theta) / sin_theta_0
        s0 = math.cos(theta) - d * s1
        rx = s0 * ax + s1 * bx
        ry = s0 * ay + s1 * by
        rz = s0 * az + s1 * bz
        rw = s0 * aw + s1 * bw
    n = math.sqrt(rx * rx + ry * ry + rz * rz + rw * rw)
    if n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return (rx / n, ry / n, rz / n, rw / n)


def _mat3_mul_vec3(R: Mat3T, v: Vec3T) -> Vec3T:
    return (
        R[0][0] * v[0] + R[0][1] * v[1] + R[0][2] * v[2],
        R[1][0] * v[0] + R[1][1] * v[1] + R[1][2] * v[2],
        R[2][0] * v[0] + R[2][1] * v[1] + R[2][2] * v[2],
    )


def _vec_dot(a: Vec3T, b: Vec3T) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_cross(a: Vec3T, b: Vec3T) -> Vec3T:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vec_norm(v: Vec3T) -> Vec3T:
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if n < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def _quat_mul(qa: QuatT, qb: QuatT) -> QuatT:
    ax, ay, az, aw = qa
    bx, by, bz, bw = qb
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _min_rotation_quat(from_v: Vec3T, to_v: Vec3T) -> QuatT:
    """Shortest-arc quaternion rotating ``from_v`` to ``to_v`` (both unit)."""
    d = _vec_dot(from_v, to_v)
    if d > 0.999999:
        return (0.0, 0.0, 0.0, 1.0)
    if d < -0.999999:
        # 180° rotation — pick any perpendicular axis.
        if abs(from_v[0]) < 0.9:
            perp = _vec_norm(_vec_cross(from_v, (1.0, 0.0, 0.0)))
        else:
            perp = _vec_norm(_vec_cross(from_v, (0.0, 1.0, 0.0)))
        return (perp[0], perp[1], perp[2], 0.0)
    axis = _vec_cross(from_v, to_v)
    qx, qy, qz = axis
    qw = 1.0 + d
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    return (qx / n, qy / n, qz / n, qw / n)


# ---------------------------------------------------------------------------
#  Spline frame at parameter s (Hermite cubic + curve-tangent-following frame)
# ---------------------------------------------------------------------------
#
# Reverse-engineered from the ``Deformed_by_hand`` reference mesh: the engine
# interpolates translation with a cubic Hermite spline whose tangent at each
# knot points along that knot's local +Z axis. The tangent magnitude equals
# the chord vector (T_{i+1}-T_i) projected onto that local +Z. The local
# cross-section frame is then rotated so its +Z axis aligns with the actual
# curve tangent at the sample point (minimum-twist correction applied to a
# slerp-blended knot rotation).
#
# Validated against ``Deformed_by_hand`` (2 knots, identity rotations,
# n_controls=1): mid-ring centers reproduce within 0.05 m and cross-section
# tilt direction matches to within 0.5°.

def _build_knots(deform) -> Tuple[List[Mat3T], List[Vec3T]]:
    """Return (rotations[], translations[]) for the spline's n+1 knots."""
    rots: List[Mat3T] = []
    trans: List[Vec3T] = []
    R0, T0 = _iso4_rt(deform.start_iso)
    rots.append(R0)
    trans.append(T0)
    for ctrl in deform.controls:
        Rc, Tc = _iso4_rt(ctrl)
        rots.append(Rc)
        trans.append(Tc)
    return rots, trans


def _build_segment_tangents(
    knot_R: List[Mat3T], knot_T: List[Vec3T]
) -> Tuple[List[Vec3T], List[Vec3T]]:
    """For each segment i (between knot i and i+1), compute the Hermite
    leaving tangent at knot i (``tan_out[i]``) and arriving tangent at
    knot i+1 (``tan_in[i]``).

    Direction is the knot's local +Z (column 2 of its rotation matrix).

    Magnitude uses the **circular-arc Hermite** formula
    ``|t| = 4 * R * tan(theta/4)`` rewritten in terms of the straight chord
    length L and the angle theta between the two knot tangents:

        |t| = 2 * L * tan(theta/4) / sin(theta/2)

    This makes a single segment between two perpendicular knots reproduce
    a perfect quarter circle (verified visually on
    ``HalfBankedCurf4x4Dirt``: the projection-only formula made the curve
    bow inward; the circular-arc formula matches the in-game shape).

    Limits:
        * theta -> 0  =>  |t| -> L  (linear case = chord magnitude)
        * theta = pi/2 =>  |t| ~= 1.171 L
        * theta -> pi  =>  |t| -> infinity (degenerate U-turn; clamped to a
          large multiple of L for safety)
    """
    n_seg = len(knot_T) - 1
    tan_out: List[Vec3T] = []
    tan_in: List[Vec3T] = []
    for i in range(n_seg):
        T0 = knot_T[i]
        T1 = knot_T[i + 1]
        chord: Vec3T = (T1[0] - T0[0], T1[1] - T0[1], T1[2] - T0[2])
        L = math.sqrt(chord[0] * chord[0] + chord[1] * chord[1] + chord[2] * chord[2])
        # Local +Z axis (deformation tangent direction) of each knot.
        z0: Vec3T = (knot_R[i][0][2], knot_R[i][1][2], knot_R[i][2][2])
        z1: Vec3T = (knot_R[i + 1][0][2], knot_R[i + 1][1][2], knot_R[i + 1][2][2])

        if L < 1e-9:
            tan_out.append((0.0, 0.0, 0.0))
            tan_in.append((0.0, 0.0, 0.0))
            continue

        # Angle between the two tangent directions.
        cos_theta = max(-1.0, min(1.0, _vec_dot(z0, z1)))
        theta = math.acos(cos_theta)

        if theta < 1e-4:
            # Degenerate-straight case: classic chord-projection collapses
            # to L, and the circular formula's limit is L too.
            mag = L
        else:
            sin_half = math.sin(theta * 0.5)
            tan_quarter = math.tan(theta * 0.25)
            mag = 2.0 * L * tan_quarter / max(sin_half, 1e-6)
            # Clamp pathological U-turns so the curve doesn't explode.
            mag = min(mag, 8.0 * L)

        # Sign: tangent should point along chord direction. If the knot
        # tangent is anti-parallel to the chord, the curve would loop back
        # on itself; preserve the original projection's sign so we don't
        # invert authored "go-backwards" curves.
        sign0 = 1.0 if _vec_dot(chord, z0) >= 0.0 else -1.0
        sign1 = 1.0 if _vec_dot(chord, z1) >= 0.0 else -1.0

        m0 = mag * sign0
        m1 = mag * sign1

        tan_out.append((z0[0] * m0, z0[1] * m0, z0[2] * m0))
        tan_in.append((z1[0] * m1, z1[1] * m1, z1[2] * m1))
    return tan_out, tan_in


def _hermite(
    u: float, P0: Vec3T, T0: Vec3T, P1: Vec3T, T1: Vec3T
) -> Vec3T:
    u2 = u * u
    u3 = u2 * u
    h00 = 2.0 * u3 - 3.0 * u2 + 1.0
    h10 = u3 - 2.0 * u2 + u
    h01 = -2.0 * u3 + 3.0 * u2
    h11 = u3 - u2
    return (
        h00 * P0[0] + h10 * T0[0] + h01 * P1[0] + h11 * T1[0],
        h00 * P0[1] + h10 * T0[1] + h01 * P1[1] + h11 * T1[1],
        h00 * P0[2] + h10 * T0[2] + h01 * P1[2] + h11 * T1[2],
    )


def _hermite_deriv(
    u: float, P0: Vec3T, T0: Vec3T, P1: Vec3T, T1: Vec3T
) -> Vec3T:
    u2 = u * u
    d00 = 6.0 * u2 - 6.0 * u
    d10 = 3.0 * u2 - 4.0 * u + 1.0
    d01 = -6.0 * u2 + 6.0 * u
    d11 = 3.0 * u2 - 2.0 * u
    return (
        d00 * P0[0] + d10 * T0[0] + d01 * P1[0] + d11 * T1[0],
        d00 * P0[1] + d10 * T0[1] + d01 * P1[1] + d11 * T1[1],
        d00 * P0[2] + d10 * T0[2] + d01 * P1[2] + d11 * T1[2],
    )


def _build_arclength_lut(
    knot_R: List[Mat3T],
    knot_T: List[Vec3T],
    tan_out: List[Vec3T],
    tan_in: List[Vec3T],
    samples_per_segment: int = 64,
) -> Tuple[List[float], List[float], float]:
    """Sample the spline and return (s_values, arclen_values, total_length).

    Returns one parametric ``s`` per sample plus the cumulative arc length up
    to that sample. Use :func:`_arclen_to_s` to convert an arc-length value
    back into ``s``.
    """
    n_segments = len(knot_T) - 1
    s_vals: List[float] = []
    a_vals: List[float] = []
    if n_segments <= 0:
        return [0.0], [0.0], 0.0

    s_vals.append(0.0)
    a_vals.append(0.0)
    prev_pos = knot_T[0]
    cum = 0.0
    for seg in range(n_segments):
        for j in range(1, samples_per_segment + 1):
            u = j / samples_per_segment
            s = seg + u
            pos = _hermite(u, knot_T[seg], tan_out[seg],
                           knot_T[seg + 1], tan_in[seg])
            dx = pos[0] - prev_pos[0]
            dy = pos[1] - prev_pos[1]
            dz = pos[2] - prev_pos[2]
            cum += math.sqrt(dx * dx + dy * dy + dz * dz)
            s_vals.append(s)
            a_vals.append(cum)
            prev_pos = pos
    return s_vals, a_vals, cum


def _arclen_to_s(target: float, s_vals: List[float], a_vals: List[float]) -> float:
    """Binary-search the LUT and linearly interpolate ``s`` for arc length ``target``."""
    if not a_vals:
        return 0.0
    if target <= a_vals[0]:
        return s_vals[0]
    if target >= a_vals[-1]:
        return s_vals[-1]
    lo, hi = 0, len(a_vals) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a_vals[mid] <= target:
            lo = mid
        else:
            hi = mid
    span = a_vals[hi] - a_vals[lo]
    if span < 1e-9:
        return s_vals[lo]
    t = (target - a_vals[lo]) / span
    return s_vals[lo] + t * (s_vals[hi] - s_vals[lo])


def _frame_at(
    s: float,
    knot_R: List[Mat3T],
    knot_T: List[Vec3T],
    tan_out: List[Vec3T],
    tan_in: List[Vec3T],
) -> Tuple[Mat3T, Vec3T]:
    n = len(knot_T) - 1  # number of segments
    if n <= 0:
        return knot_R[0], knot_T[0]
    if s <= 0.0:
        return knot_R[0], knot_T[0]
    if s >= n:
        return knot_R[n], knot_T[n]
    i = int(math.floor(s))
    if i >= n:
        i = n - 1
    u = s - i

    P0 = knot_T[i]
    P1 = knot_T[i + 1]
    T0 = tan_out[i]
    T1 = tan_in[i]
    pos = _hermite(u, P0, T0, P1, T1)
    deriv = _hermite_deriv(u, P0, T0, P1, T1)
    z_curve = _vec_norm(deriv)
    if z_curve == (0.0, 0.0, 0.0):
        # Degenerate segment — fall back to slerp+lerp.
        qa = _mat3_to_quat(knot_R[i])
        qb = _mat3_to_quat(knot_R[i + 1])
        return _quat_to_mat3(_slerp(qa, qb, u)), pos

    # Slerp the knot rotations to get a baseline frame, then apply a
    # minimum-rotation twist so its local +Z aligns with the curve tangent.
    qa = _mat3_to_quat(knot_R[i])
    qb = _mat3_to_quat(knot_R[i + 1])
    qm = _slerp(qa, qb, u)
    Rs = _quat_to_mat3(qm)
    Rs_z: Vec3T = (Rs[0][2], Rs[1][2], Rs[2][2])
    qcorr = _min_rotation_quat(Rs_z, z_curve)
    qfinal = _quat_mul(qcorr, qm)
    return _quat_to_mat3(qfinal), pos


# ---------------------------------------------------------------------------
#  Public entry point
# ---------------------------------------------------------------------------

def deform_vertices(
    verts_gbx: Sequence[Vec3T],
    deform,
    src_z_extent: float | None = None,
) -> List[Vec3T]:
    """Apply a Deformation layer to a list of base-mesh vertices.

    Parameters
    ----------
    verts_gbx
        Base mesh positions in raw GBX coordinates ``(x, y, z)``.
    deform
        Parsed ``GbxCrystal_Deformation`` Container (has ``start_iso``,
        ``controls``, ``n_controls`` etc.).
    src_z_extent
        Z extent of the *source* mesh (before tiling). When provided,
        each tile of size ``src_z_extent`` is mapped to exactly that
        many units of curve arc length, except the last tile which
        absorbs the remainder. When ``None``, the input ``verts_gbx``
        are mapped uniformly across the full arc length.
    """
    knot_R, knot_T = _build_knots(deform)
    n_segments = len(knot_T) - 1
    if n_segments <= 0 or not verts_gbx:
        return [tuple(v) for v in verts_gbx]
    tan_out, tan_in = _build_segment_tangents(knot_R, knot_T)

    # Source-mesh extent along the deformation parameter axis (GBX Z).
    z_vals = [v[2] for v in verts_gbx]
    z_min = min(z_vals)
    z_max = max(z_vals)
    z_span = z_max - z_min
    if z_span < 1e-9:
        # Degenerate (all vertices at same Z) — collapse onto knot 0.
        log.warning("Deformation: source mesh has zero Z extent; passthrough")
        return [tuple(v) for v in verts_gbx]

    s_lut, a_lut, total_arclen = _build_arclength_lut(
        knot_R, knot_T, tan_out, tan_in
    )

    # Determine effective tile count.
    if src_z_extent is None or src_z_extent <= 0:
        tile_size = z_span
        n_tiles_eff = 1
    else:
        tile_size = float(src_z_extent)
        n_tiles_eff = max(1, int(round(z_span / tile_size)))

    # Source-local axial coordinate runs from z_min (curve start, s=0) to
    # z_max (end of last tile). Verts past either end extrapolate along
    # the corresponding endpoint tangent. NOTE: the verts coming in are
    # already in the source-local frame when the caller went through
    # world_to_source_local — for HalfBanked-style items z_min then
    # naturally lands at 0 (curve start plane). The hand-authored
    # ``Deformed_by_hand`` reference test bypasses world_to_source_local
    # and feeds verts with z_min=-2, so the explicit -z_min shift is
    # required for both code paths.
    arclen_per_tile = (total_arclen / n_tiles_eff) if n_tiles_eff > 0 else 0.0
    arclen_per_z = (arclen_per_tile / tile_size) if tile_size > 0 else 0.0

    # Pre-compute extrapolation frames at the curve's two endpoints.
    R_start, T_start = _frame_at(0.0, knot_R, knot_T, tan_out, tan_in)
    R_end, T_end = _frame_at(float(n_segments), knot_R, knot_T, tan_out, tan_in)
    start_dir: Vec3T = (R_start[0][2], R_start[1][2], R_start[2][2])
    end_dir: Vec3T = (R_end[0][2], R_end[1][2], R_end[2][2])

    log.warning(
        "Deformation layer detected — generated mesh accuracy is degraded "
        "(curve parameterization is approximate; non-linear curves may "
        "differ slightly from the in-game shape)."
    )

    out: List[Vec3T] = []
    for vx, vy, vz in verts_gbx:
        # vz is the source-local axial coordinate; the source mesh's
        # axial range [z_min, z_max] is mapped to the curve's arc-length
        # range [0, total_arclen]. So z_min -> curve start, z_max -> end.
        target_arclen = (vz - z_min) * arclen_per_z

        if target_arclen < 0.0:
            # Extrapolate back along the start tangent.
            R, T = R_start, (
                T_start[0] + start_dir[0] * target_arclen,
                T_start[1] + start_dir[1] * target_arclen,
                T_start[2] + start_dir[2] * target_arclen,
            )
        elif target_arclen > total_arclen:
            overrun = target_arclen - total_arclen
            R, T = R_end, (
                T_end[0] + end_dir[0] * overrun,
                T_end[1] + end_dir[1] * overrun,
                T_end[2] + end_dir[2] * overrun,
            )
        else:
            s = _arclen_to_s(target_arclen, s_lut, a_lut)
            R, T = _frame_at(s, knot_R, knot_T, tan_out, tan_in)

        # Cross-section vector: keep XY, drop Z (the parameter is consumed by s).
        local: Vec3T = (vx, vy, 0.0)
        rot = _mat3_mul_vec3(R, local)
        out.append((rot[0] + T[0], rot[1] + T[1], rot[2] + T[2]))

    log.debug(
        "Deformation applied: %d verts, %d knots, z_range=[%.3f, %.3f]",
        len(verts_gbx), len(knot_T), z_min, z_max,
    )
    return out


def find_deformation_layer(layers_chunk):
    """Return the first parsed ``GbxCrystal_Deformation`` content found in a
    ``CPlugCrystal`` layers chunk, or ``None``.

    Note: this is layer-ordering-agnostic. Most callers should use
    :func:`find_deformation_layer_for_index` instead, which respects the
    layer-stack rule that a Deformation only affects Geometry layers
    stacked beneath it (i.e. at a *lower* index in the layers array).
    """
    for layer in getattr(layers_chunk, "layers", None) or []:
        if getattr(layer, "type", "") != "Deformation":
            continue
        content = getattr(layer, "content", None)
        if content is None:
            continue
        if not (hasattr(content, "controls") and hasattr(content, "start_iso")):
            _patch_v6_deformation_content(content)
        if hasattr(content, "controls") and hasattr(content, "start_iso"):
            return content
    return None


def find_deformation_layer_for_index(layers_chunk, geom_layer_index: int):
    """Return the Deformation layer that should modify the Geometry layer at
    ``geom_layer_index``, or ``None``.

    Layer-binding rules (matches the in-editor behavior, verified against
    ``HalfBankedCurf4x4Dirt`` and the controlled ``layeredItems`` test set):

    1. **Stack hierarchy**: a Deformation only affects Geometry layers
       *beneath* it in the layer stack. In the parsed layers array,
       "beneath" maps to lower indices — a Deformation at array index D
       is a candidate for Geometry layers at index < D.
    2. **Single-target binding**: each v1 Deformation binds to exactly one
       Geometry layer — the *lowest-indexed* (oldest / first-created)
       Geometry layer beneath it. So a Deformation at index D modifies
       only the Geometry layer at the lowest index < D, leaving any other
       intermediate Geometry layers (e.g. ``HalfBankedCurf``'s "Clips"
       layer between BASE and Verformung) untouched.
    3. **Closest Deformation wins**: if multiple Deformations sit above
       the candidate Geometry layer, the closest one (lowest index > D's
       target) is the one that owns it.

    ``linked_layer_id`` identifies the first mask entry's source layer.
    Multiple source-layer targets remain unsupported.
    """
    layers = list(getattr(layers_chunk, "layers", None) or [])
    n = len(layers)

    def _is_geometry(idx: int) -> bool:
        return idx < n and getattr(layers[idx], "type", "") == "Geometry"

    def _is_deformation(layer) -> bool:
        if getattr(layer, "type", "") != "Deformation":
            return False
        content = getattr(layer, "content", None)
        if content is None:
            return False
        # v3+ layers come out of NICE without ``controls``/``start_iso``
        # because the schema absorbs the body as raw bytes. Try to decode
        # the blob in-place so the layer behaves like v1/v2.
        if not (hasattr(content, "controls") and hasattr(content, "start_iso")):
            if not _patch_v6_deformation_content(content):
                return False
        return hasattr(content, "controls") and hasattr(content, "start_iso")

    # Walk Deformation layers above geom_layer_index, closest first.
    for d_idx in range(geom_layer_index + 1, n):
        d_layer = layers[d_idx]
        if not _is_deformation(d_layer):
            continue
        # Find this Deformation's target = lowest-indexed Geometry below D.
        target_idx = next(
            (i for i in range(d_idx) if _is_geometry(i)), None
        )
        if target_idx is None or target_idx != geom_layer_index:
            # This Deformation belongs to a different Geometry layer.
            return None
        # v2: explicit binding overrides the implicit "lowest-index" rule
        # — but only when the referenced id actually resolves to one of
        # the file's Geometry layers. Authoring sessions sometimes leave
        # behind a stale ``linked_layer_id`` (e.g. ``Deformed_by_layer2``
        # references ``'Layer5'`` while the only Geometry layer is
        # ``'Layer0'`` — the original Layer5 was deleted). In that case
        # we silently fall back to the v1-style implicit binding so the
        # mesh still deforms instead of importing flat.
        content = d_layer.content
        explicit_id = getattr(content, "linked_layer_id", None)
        if explicit_id:
            existing_geom_ids = {
                getattr(layers[i], "id", None)
                for i in range(d_idx)
                if _is_geometry(i)
            }
            if explicit_id in existing_geom_ids:
                our_id = getattr(layers[geom_layer_index], "id", None)
                if our_id != explicit_id:
                    return None
            # else: stale link → fall through to implicit rule (already
            # validated above: target_idx == geom_layer_index).
        return content
    return None


# ---------------------------------------------------------------------------
#  Pre-deformation source-mesh subdivision
# ---------------------------------------------------------------------------

# Each face is represented as a dict:
#   {"inds": [v0, v1, ...], "mat": int, "uvs": [(u, v), ...]}
# where ``inds`` indexes into a flat list of (x, y, z) raw GBX positions and
# ``uvs`` is per-corner (one entry per ind) in the same order.

def subdivide_axial_quads(
    raw_positions: List[Vec3T],
    faces_simple: List[dict],
    n_cuts: int = 8,
) -> Tuple[List[Vec3T], List[dict]]:
    """Subdivide quad faces along their Z-axial direction so a downstream
    deformation can render smooth curves even from low-poly source meshes.

    Strategy:

    * Identify the deformation parameter axis (raw GBX +Z).
    * For each quad ABCD, classify each of its 4 edges by ``|dz|``. Pick the
      pair of opposite edges with the larger combined ``|dz|`` as "axial".
    * If neither pair has any Z extent (face perpendicular to spine, e.g.
      the top/bottom cap of a cube), leave the face untouched.
    * Else, split each axial edge into ``n_cuts + 1`` equal segments.
      Edge midpoints are *shared* between adjacent quads via an edge cache
      keyed on the unordered vertex pair, so no cracks form.
    * Replace the original quad with a strip of ``n_cuts + 1`` smaller quads
      whose UVs are bilinearly interpolated from the original corners.

    Triangles, n-gons and quads with no Z extent pass through untouched.
    Vertex positions are *only added* (never modified) — existing indices
    remain valid for any other faces that referenced them.
    """
    if n_cuts <= 0 or not faces_simple:
        return raw_positions, faces_simple

    raw_positions = list(raw_positions)  # mutable copy

    # Cache: (min_vidx, max_vidx) -> list of new vert indices, oriented
    # min -> max, with len == n_cuts.
    edge_mid_cache: dict = {}

    def get_edge_midpoints(va: int, vb: int) -> List[int]:
        key = (min(va, vb), max(va, vb))
        if key in edge_mid_cache:
            mids = edge_mid_cache[key]
        else:
            pa = raw_positions[key[0]]
            pb = raw_positions[key[1]]
            mids = []
            for k in range(1, n_cuts + 1):
                t = k / (n_cuts + 1)
                mids.append(len(raw_positions))
                raw_positions.append((
                    pa[0] + t * (pb[0] - pa[0]),
                    pa[1] + t * (pb[1] - pa[1]),
                    pa[2] + t * (pb[2] - pa[2]),
                ))
            edge_mid_cache[key] = mids
        # Return in va -> vb orientation
        return mids if va < vb else list(reversed(mids))

    out_faces: List[dict] = []
    for face in faces_simple:
        inds = face["inds"]
        if len(inds) != 4:
            out_faces.append(face)
            continue

        a, b, c, d = inds
        pa = raw_positions[a]
        pb = raw_positions[b]
        pc = raw_positions[c]
        pd = raw_positions[d]
        dz_ab = abs(pb[2] - pa[2])
        dz_bc = abs(pc[2] - pb[2])
        dz_cd = abs(pd[2] - pc[2])
        dz_da = abs(pa[2] - pd[2])

        # Pick the pair of opposite edges with the larger combined |dz|.
        ab_cd = dz_ab + dz_cd
        bc_da = dz_bc + dz_da
        if max(ab_cd, bc_da) < 1e-6:
            out_faces.append(face)
            continue

        uvs = face["uvs"]
        uvA, uvB, uvC, uvD = uvs[0], uvs[1], uvs[2], uvs[3]
        mat = face["mat"]

        if ab_cd >= bc_da:
            # Axial edges: A-B and D-C (both go along the spine).
            mid_ab = get_edge_midpoints(a, b)
            mid_dc = get_edge_midpoints(d, c)
            P = [a] + mid_ab + [b]            # along bottom edge
            Q = [d] + mid_dc + [c]            # along top edge
            n = len(P) - 1
            for k in range(n):
                t0, t1 = k / n, (k + 1) / n
                uvP0 = (uvA[0] + t0 * (uvB[0] - uvA[0]), uvA[1] + t0 * (uvB[1] - uvA[1]))
                uvP1 = (uvA[0] + t1 * (uvB[0] - uvA[0]), uvA[1] + t1 * (uvB[1] - uvA[1]))
                uvQ1 = (uvD[0] + t1 * (uvC[0] - uvD[0]), uvD[1] + t1 * (uvC[1] - uvD[1]))
                uvQ0 = (uvD[0] + t0 * (uvC[0] - uvD[0]), uvD[1] + t0 * (uvC[1] - uvD[1]))
                out_faces.append({
                    **face,
                    "inds": [P[k], P[k + 1], Q[k + 1], Q[k]],
                    "mat": mat,
                    "uvs": [uvP0, uvP1, uvQ1, uvQ0],
                })
        else:
            # Axial edges: B-C and A-D.
            mid_bc = get_edge_midpoints(b, c)
            mid_ad = get_edge_midpoints(a, d)
            P = [b] + mid_bc + [c]
            Q = [a] + mid_ad + [d]
            n = len(P) - 1
            for k in range(n):
                t0, t1 = k / n, (k + 1) / n
                uvP0 = (uvB[0] + t0 * (uvC[0] - uvB[0]), uvB[1] + t0 * (uvC[1] - uvB[1]))
                uvP1 = (uvB[0] + t1 * (uvC[0] - uvB[0]), uvB[1] + t1 * (uvC[1] - uvB[1]))
                uvQ1 = (uvA[0] + t1 * (uvD[0] - uvA[0]), uvA[1] + t1 * (uvD[1] - uvA[1]))
                uvQ0 = (uvA[0] + t0 * (uvD[0] - uvA[0]), uvA[1] + t0 * (uvD[1] - uvA[1]))
                # Original winding A,B,C,D -> here we used P=B..C, Q=A..D
                # Re-emit as Q[k], P[k], P[k+1], Q[k+1] to preserve outward normal.
                out_faces.append({
                    **face,
                    "inds": [Q[k], P[k], P[k + 1], Q[k + 1]],
                    "mat": mat,
                    "uvs": [uvQ0, uvP0, uvP1, uvQ1],
                })

    return raw_positions, out_faces


# ---------------------------------------------------------------------------
#  Pre-deformation source-mesh tiling
# ---------------------------------------------------------------------------

def _path_chord_length(deform) -> float:
    """Approximate arc length of the spline.

    Sums short straight-line samples along the Hermite curve (rather than
    chord lengths between knots) so that strongly-curved segments contribute
    their actual length. This is what determines ``n_tiles`` during
    pre-deformation tiling.
    """
    knot_R, knot_T = _build_knots(deform)
    if len(knot_T) < 2:
        return 0.0
    tan_out, tan_in = _build_segment_tangents(knot_R, knot_T)
    _s, _a, total = _build_arclength_lut(knot_R, knot_T, tan_out, tan_in)
    return total


def world_to_source_local(
    verts_world: Sequence[Vec3T], deform
) -> List[Vec3T]:
    """Transform world-space vertices into the deformation source-local frame.

    The deformation pipeline downstream (tile_along_axis, deform_vertices)
    assumes the source mesh's local +Z axis is the deformation parameter
    axis. In general the engine lets the author pick X/Y/Z (or any rotated
    axis) via ``start_iso``'s rotation. ``start_iso`` is the world-space
    pose of the source frame at curve s=0; its column 2 is the world
    direction of source-local +Z (i.e. the deform axis), and its translation
    is the source-mesh center in world coords.

    This helper applies ``v_local = R_start^T @ (v_world - T_start)`` to
    every vert so the downstream code can keep operating in its canonical
    "Z = axial" assumption. After ``deform_vertices`` runs, the curve frames
    (which are also in world space) emit world-space outputs directly — no
    inverse transform is needed.
    """
    R, T = _iso4_rt(deform.start_iso)
    # Inverse of a rotation matrix is its transpose.
    out: List[Vec3T] = []
    for vx, vy, vz in verts_world:
        dx = vx - T[0]
        dy = vy - T[1]
        dz = vz - T[2]
        # R^T @ d  (R is row-major, column-vector convention).
        lx = R[0][0] * dx + R[1][0] * dy + R[2][0] * dz
        ly = R[0][1] * dx + R[1][1] * dy + R[2][1] * dz
        lz = R[0][2] * dx + R[1][2] * dy + R[2][2] * dz
        out.append((lx, ly, lz))
    return out


def tile_along_axis(
    raw_positions: List[Vec3T],
    faces_simple: List[dict],
    deform,
) -> Tuple[List[Vec3T], List[dict]]:
    """Replicate the source mesh end-to-end along its local +Z axis.

    The TM engine bakes ``n_tiles = round(chord_length / source_z_extent)``
    copies of the source mesh, offsets each copy by ``k * source_z_extent``
    on +Z, then welds the boundary verts that overlap on the shared planes
    between adjacent copies (xy match within ``EPS``). The resulting tiled
    mesh is what gets fed into ``deform_vertices`` so the curve carries
    each tile's authored cross-section rings end-to-end (rather than
    smearing one cube across the bend).

    UVs are preserved per tile (each copy's UV islands run 0..1 — no
    stretching across the bend). Material indices carry through unchanged.

    Cap faces at z == z_min and z == z_max are emitted only on the first
    and last tile respectively (interior caps would otherwise hide inside
    the tiled solid). A face is detected as a "cap" when ALL its vertices
    lie on the source's z_min or z_max plane.
    """
    if not raw_positions or not faces_simple:
        return raw_positions, faces_simple

    z_vals = [v[2] for v in raw_positions]
    z_min = min(z_vals)
    z_max = max(z_vals)
    z_extent = z_max - z_min
    if z_extent < 1e-6:
        return raw_positions, faces_simple

    chord = _path_chord_length(deform)
    n_tiles = max(1, int(round(chord / z_extent)))
    if n_tiles == 1:
        return raw_positions, faces_simple

    EPS = 1e-4
    n_src = len(raw_positions)

    # For each source vert, mark whether it sits on the z_min or z_max plane.
    on_zmin = [abs(v[2] - z_min) < EPS for v in raw_positions]
    on_zmax = [abs(v[2] - z_max) < EPS for v in raw_positions]

    def _xy_key(v):
        return (round(v[0] / EPS), round(v[1] / EPS))

    # Decide whether the two cap rings match exactly: same set of (x, y)
    # keys on both sides. Only then is it safe to weld z_min->prev z_max.
    zmin_keys = {_xy_key(raw_positions[i]) for i in range(n_src) if on_zmin[i]}
    zmax_keys = {_xy_key(raw_positions[i]) for i in range(n_src) if on_zmax[i]}
    rings_match = bool(zmin_keys) and zmin_keys == zmax_keys

    # Build the welding map. For tile k > 0, any source vert on z_min should
    # collapse to its counterpart in tile (k-1)'s z_max ring (matching x, y).
    out_positions: List[Vec3T] = []
    # global_index[k][i] = position in out_positions for source vert i in tile k.
    global_index: List[List[int]] = [[-1] * n_src for _ in range(n_tiles)]

    zmax_xy_to_src: dict = {}
    if rings_match:
        for i, v in enumerate(raw_positions):
            if on_zmax[i]:
                zmax_xy_to_src[_xy_key(v)] = i

    for k in range(n_tiles):
        z_off = k * z_extent
        for i, (vx, vy, vz) in enumerate(raw_positions):
            if rings_match and k > 0 and on_zmin[i]:
                twin = zmax_xy_to_src.get(_xy_key((vx, vy, vz)))
                if twin is not None and global_index[k - 1][twin] >= 0:
                    global_index[k][i] = global_index[k - 1][twin]
                    continue
            global_index[k][i] = len(out_positions)
            out_positions.append((vx, vy, vz + z_off))

    # Replicate faces. The TM engine has a quirk we replicate verbatim:
    # tile 0 is emitted unchanged (keeps both caps); every subsequent tile
    # k > 0 drops its z_min cap faces (the cap that faces back toward the
    # previous tile). When rings match, this mirrors the welded-and-trimmed
    # behavior; when rings don't match it produces the small "missing-cap"
    # holes visible in hand-authored items (e.g. ``Deformed_by_hand2``),
    # which we intentionally preserve to stay byte-identical with the game.
    out_faces: List[dict] = []
    for face in faces_simple:
        inds = face["inds"]
        if len(inds) < 3:
            continue
        is_zmin_cap = all(on_zmin[i] for i in inds)
        is_zmax_cap = all(on_zmax[i] for i in inds)

        for k in range(n_tiles):
            if is_zmin_cap and k > 0:
                # When rings match, the z_min cap is fully welded to the
                # previous tile's z_max ring — emitting any face here
                # would duplicate triangles already in place.
                if rings_match:
                    continue
                # Engine quirk: on every added tile the z_min cap is
                # emitted as a single triangle per source quad (half the
                # cap). The diagonal used is coordinate-based — between
                # the source quad's (min_x+min_y) and (max_x+max_y)
                # corners — not based on source winding (which can vary
                # quad to quad). The triangle keeps the diagonal plus
                # whichever of the other two corners comes first in the
                # source winding (so face normal stays consistent).
                if len(inds) == 4:
                    src_xy = [(raw_positions[i][0], raw_positions[i][1])
                              for i in inds]
                    sums = [xy[0] + xy[1] for xy in src_xy]
                    diag_a = min(range(4), key=lambda p: sums[p])
                    diag_b = max(range(4), key=lambda p: sums[p])
                    others = [p for p in range(4) if p != diag_a and p != diag_b]
                    third = others[0]
                    # Emit in source order to preserve CCW winding.
                    chosen = sorted([diag_a, diag_b, third])
                    mapped = [global_index[k][inds[p]] for p in chosen]
                    out_faces.append({
                        **face,
                        "inds": mapped,
                        "mat": face["mat"],
                        "uvs": [face["uvs"][p] for p in chosen],
                    })
                continue
            # Only trim the z_max interior cap when rings match (so the
            # welded next-tile z_min cap doesn't leave a duplicate).
            # When rings don't match, keep z_max caps to mirror the engine.
            if rings_match and is_zmax_cap and k < n_tiles - 1:
                continue
            mapped = [global_index[k][i] for i in inds]
            out_faces.append({
                **face,
                "inds": mapped,
                "mat": face["mat"],
                "uvs": list(face["uvs"]),
            })

    log.debug(
        "Deformation tiling: %d source verts/%d faces -> %d tiles -> %d verts/%d faces "
        "(chord=%.3f, z_extent=%.3f, rings_match=%s)",
        n_src, len(faces_simple), n_tiles, len(out_positions), len(out_faces),
        chord, z_extent, rings_match,
    )
    return out_positions, out_faces


__all__ = [
    "deform_vertices",
    "find_deformation_layer",
    "find_deformation_layer_for_index",
    "subdivide_axial_quads",
    "tile_along_axis",
    "world_to_source_local",
]
