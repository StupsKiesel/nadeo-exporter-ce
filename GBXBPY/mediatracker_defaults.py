"""Initial MediaTracker block data in the editor's plain domain representation.

Layouts follow NICE and GBX.NET's Game/*.chunkl class definitions.
These are neutral editor presets, not captured game-default presets.
"""

from copy import deepcopy
import math

from .model.mediatracker import MediaTrackerBlock


def _vec3(x=0.0, y=0.0, z=0.0):
    return {"x": x, "y": y, "z": z}


def _keys(start, end, values):
    return [{"time": time, **deepcopy(values)} for time in (start, end)]


_CAMERA = {
    "position": _vec3(), "pitchYawRoll": _vec3(), "fov": 90.0,
    "anchorRot": False, "anchor": -1, "anchorVis": False, "target": -1,
    "targetPosition": _vec3(),
}

_KEYED = {
    0x03024000: (0x03024000, {"separation": 0.0, "screenDistance": 4.0}, {}),
    0x030A0000: (0x030A0001, {
        "archiveMarker": 0, "radius": 10.0, "longitude": 0.0, "latitude": 0.0,
        "targetPosition": _vec3(), "fov": 90.0, "minRenderDistance": 0.1,
        "maxRenderDistance": 10000.0, "clipEntId": -1, "u03": 0.0, "targetOffset": _vec3(),
    }, {"version": 1}),
    0x030A1000: (0x030A1003, {
        **_CAMERA, "nearZ": 0.1, "weight": 1.0,
        "rotation": {**_vec3(), "w": 1.0},
        "legacyTargetSceneUId": -1, "legacyAnchorSceneUId": -1,
    }, {"version": 5}),
    0x030A2000: (0x030A2006, {
        **_CAMERA, "interpolation": 2, "nearZ": 0.1, "pssmDistScale": 1.0,
        "leftTangent": {
            "position": _vec3(), "pitchYawRoll": _vec3(), "fov": 0.0,
            "targetPosition": _vec3(), "nearZ": 0.0, "pssmDistScale": 0.0,
        },
        "rightTangent": {
            "position": _vec3(), "pitchYawRoll": _vec3(), "fov": 0.0,
            "targetPosition": _vec3(), "nearZ": 0.0, "pssmDistScale": 0.0,
        },
        "legacyTargetSceneUId": None, "legacyAnchorSceneUId": None,
    }, {"version": 4}),
    0x030A4000: (0x030A4000, {"intensity": 0.0, "tempo": 1.0}, {}),
    0x030A6000: (0x030A6001, {"musicVolume": 1.0, "soundVolume": 1.0}, {}),
    0x030AB000: (0x030AB000, {"opacity": 0.0}, {"color": {**_vec3(), "w": 1.0}}),
    0x03126000: (0x03126002, {
        "zFocus": 10.0, "lensSize": 0.0, "target": -1, "targetPosition": _vec3(),
    }, {}),
    0x03128000: (0x03128002, {"intensity": 0.0, "rayIntensity": 0.0, "rayAttenuation": 1.0}, {}),
    0x03165000: (0x03165000, {"intensity": 0.0}, {"version": 0}),
    0x03129000: (0x03129000, {"tempo": 1.0}, {}),
    0x030EB000: (0x030EB000, {"spectatorCount": 1.0}, {"version": 1}),
    0x03080000: (0x03080003, {
        f"u{index:02d}": (1.0 if index in (1, 10, 11, 12, 22, 23, 24) else 0.0)
        for index in range(1, 29)
    }, {}),
    0x03127000: (0x03127004, {"u01": 0.0, "u02": 1.0, "u03": 8.0, "u04": 0.0}, {}),
    0x03199000: (0x03199000, {f"u{index:02d}": 0.0 for index in range(1, 10)}, {"version": 2}),
    0x03161000: (0x03161000, {"a": 0.0, "b": 0.0, "c": 0.0}, {"version": 1, "script": ""}),
    0x0316D000: (0x0316D000, {"captureWeight": 0.0}, {"version": 0}),
    0x03081000: (0x03081001, {"lensSize": 0.0, "forceFocus": False, "focusZ": 10.0}, {}),
    0x03083000: (0x03083001, {"intensity": 0.0, "sensitivity": 0.0}, {}),
    0x03186000: (0x03186001, {"intensity": 0.0}, {}),
    0x030E5000: (0x030E5002, {"lights": 0}, {
        "version": 5, "ghostModel": None, "startOffset": 0.0, "noDamage": False,
        "forceLight": False, "forceHue": False, "trailIntensity": 1.0,
    }),
    0x0329F000: (0x0329F000, {"lights": 0}, {
        "version": 4, "recordData": None, "start": None, "end": None, "startOffset": 0.0,
        "noticeRecords": [], "noDamage": False, "u04": False, "forceLight": False,
        "forceHue": False, "lightTrailColor": _vec3(1.0, 1.0, 1.0),
        "u13": None, "playerModel": {"id": "", "collection": "", "author": ""},
        "u09": _vec3(1.0, 1.0, 1.0), "skinNames": [], "hasBadges": False,
        "skinOptions": None, "u10": None,
    }),
}

_BOUNDED = {
    0x03084000: (0x03084007, {
        "version": 3, "gameCam": 0, "clipEntId": -1,
        "camPosition": _vec3(), "camPitchYawRoll": _vec3(), "camFov": 90.0,
        "u01": 10.0, "u02": 0.0, "camNearClipPlane": -1.0, "camFarClipPlane": -1.0,
        "u03": True, "u04": True, "u05": True, "u06": 1.0, "u07": b"\x00\x00\xf0\x0f",
    }),
    0x03166000: (0x03166000, {"version": 0, "tracking": False, "autofocus": False, "autoZoom": False}),
    0x03195000: (0x03195000, {"version": 0, "showInterface": True, "manialink": ""}),
    0x0312A000: (0x0312A001, {"version": 0, "url": ""}),
    0x030A9000: (0x030A9000, {}),
    0x03082000: (0x03082000, {}),
    0x030C0000: (0x030C0000, {"version": 0}),
    0x03133000: (0x03133000, {}),
    0x03139000: (0x03139000, {}),
}


def _file_ref():
    return {"PackDescVersion": 0, "FilePath": "", "Checksum": None, "LocatorUrl": None}


def create_media_block(class_id: int, start: float, duration: float = 3.0) -> MediaTrackerBlock:
    """Create independent, typed editor data with an exact positive time range."""
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, duration)):
        raise ValueError("Block start and duration must be finite numbers")
    end = start + duration
    if duration <= 0 or not math.isfinite(end) or end <= start:
        raise ValueError("Block duration must be positive")
    if class_id == 0x0307F000:
        raise ValueError("Blur is abstract; use Motion Blur or Depth Blur")
    if class_id in _KEYED:
        chunk_id, values, properties = _KEYED[class_id]
        chunks = {chunk_id: {**deepcopy(properties), "keys": _keys(start, end, values)}}
    elif class_id in _BOUNDED:
        chunk_id, properties = _BOUNDED[class_id]
        chunks = {chunk_id: {**deepcopy(properties), "start": start, "end": end}}
    elif class_id == 0x03085000:
        chunks = {0x03085000: {"keys": [
            {"time": time, "u01": time, "u02": 1.0} for time in (start, end)
        ]}}
    elif class_id in (0x030A5000, 0x030A8000):
        effect = {"classId": 0x07010000, "body": {0x07010005: {
            "keys": _keys(start, end, {
                "position": {"x": 0.0, "y": 0.0}, "rotation": 0.0,
                "scale": {"x": 1.0, "y": 1.0}, "opacity": 1.0, "depth": 0.5,
                "unknown": [0.0] * 4,
            }),
            "centered": True, "colorBlendMode": 0, "isContinuous": False, "isInterpolated": True,
        }}}
        if class_id == 0x030A8000:
            chunks = {0x030A8001: {"text": "Text", "effect": effect}, 0x030A8002: {"color": _vec3(1.0, 1.0, 1.0)}}
        else:
            chunks = {0x030A5000: {
                "effect": effect,
                "image": _file_ref(),
            }}
    elif class_id == 0x030A7000:
        chunks = {
            0x030A7003: {
                "version": 2, "playCount": 1, "isLooping": False, "isMusic": False,
                "stopWithClip": True, "audioToSpeech": False, "audioToSpeechTarget": 0,
            },
            0x030A7004: {
                "sound": _file_ref(), "version": 1,
                "keys": _keys(start, end, {"volume": 1.0, "pan": 0.0, "position": _vec3()}),
            },
        }
    elif class_id in (0x0304B000, 0x0304C000):
        positions = [_vec3(-0.5, -0.5), _vec3(0.5, -0.5), _vec3(0.0, 0.5)]
        chunks = {0x03029001: {
            "times": [start, end], "numKeys": 2, "numVerts": 3,
            "verticesPositions": [deepcopy(positions), deepcopy(positions)],
            "verticesColors": [{**_vec3(1.0, 1.0, 1.0), "w": 1.0} for _ in positions],
            "faces": [{"x": 0, "y": 1, "z": 2}],
            "u01": 1, "u02": 0, "u03": 0, "u04": 1.0, "u05": 0, "u06": 0,
        }, 0x03029002: {"anchorLocation": -1}}
    else:
        raise ValueError(f"No MediaTracker block constructor for 0x{class_id:08X}")
    if class_id == 0x03186000:
        chunks[0x03186000] = {"image": _file_ref()}
    elif class_id == 0x03133000:
        chunks[0x03133001] = {"target": -1}
    elif class_id == 0x03139000:
        chunks[0x03139001] = {
            "archiveMarker": 0,
            "transform": {name: 1.0 if name in ("XX", "YY", "ZZ") else 0.0 for name in (
                "XX", "XY", "XZ", "YX", "YY", "YZ", "ZX", "ZY", "ZZ", "TX", "TY", "TZ",
            )},
            "fov": 90.0, "nearZ": -1.0, "farZ": -1.0, "image": _file_ref(),
        }
    return MediaTrackerBlock(class_id=class_id, chunks=chunks)
