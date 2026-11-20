import logging
import os
from contextlib import contextmanager
from pathlib import Path
from functools import partial
from collections import OrderedDict

from construct import ConstructError, Container

from .gbx_structs import GbxStruct, GbxStructWithoutBodyParsed

log = logging.getLogger("tm2020.nice.parser")

# Externals parsed inside shared_external_cache(), keyed by recursive mode
# then path. NICE copies each external into a new node container, so one
# parsed file can back several top-level parses.
_shared_cache = None


@contextmanager
def shared_external_cache():
    """Reuse parsed external files across top-level parses (one import).

    A large mesh referenced by a block info and by its prefab is otherwise
    parsed once per top-level file. Nested scopes share the outer cache.
    """
    global _shared_cache
    if _shared_cache is not None:
        yield
        return
    _shared_cache = {}
    try:
        yield
    finally:
        _shared_cache = None


def _external_cache(recursive):
    if _shared_cache is None:
        return {}
    return _shared_cache.setdefault(bool(recursive), {})


# Called with the absolute path before parse_file reads a file; lets the
# caller fetch missing files on demand (GBXBPY.library).
_file_hook = None
_parse_failure_hook = None


def set_file_hook(hook):
    global _file_hook
    _file_hook = hook


def set_parse_failure_hook(hook):
    global _parse_failure_hook
    _parse_failure_hook = hook


def parse_bytes(raw_bytes, filepath="", log=False, recursive=False):
    """Use this for in-memory reading"""
    file_dir = os.path.dirname(filepath)

    gbx_data = {}
    nodes = []
    errors = []
    warns = []
    data = GbxStruct.parse(
        raw_bytes,
        gbx_data=gbx_data,
        nodes=nodes,
        filename=filepath,
        load_external_file=partial(_load_external_file, _external_cache(recursive), log, file_dir, recursive),
        errors=errors,
        warns=warns,
    )
    data.filepath = filepath
    data.node_offset = 0
    data._errors = list(set(errors))
    data._warns = list(set(warns))

    return data


def parse_file(file_path, recursive=True, log=False):
    file_path = os.path.abspath(file_path)

    if _file_hook is not None:
        _file_hook(file_path)

    if not os.path.exists(file_path):
        error = f"[FILE NOT FOUND] {file_path}"
        logging.getLogger("tm2020.nice.parser").error(error)
        return Container(_error=error)

    if log:
        logging.getLogger("tm2020.nice.parser").debug("%s", file_path)

    def read():
        with open(file_path, "rb") as f:
            return parse_bytes(f.read(), file_path, log, recursive)

    try:
        data = read()
    except (ConstructError, OSError):
        if _parse_failure_hook is None or not _parse_failure_hook(file_path):
            raise
        return read()
    if data._errors and _parse_failure_hook is not None and _parse_failure_hook(file_path):
        return read()
    return data


def _load_external_file(files_cache, log, root_path, recursive, relative_path):
    file_path = os.path.normpath(root_path + os.path.sep + relative_path)

    if file_path in files_cache:
        if log:
            logging.getLogger("tm2020.nice.parser").debug("reuse %s", file_path)
        return files_cache[file_path]

    force_recursive = (
        file_path.lower().endswith(".terrainmodifier.gbx")
        or file_path.lower().endswith(".kinematicconstraint.gbx")
        or file_path.lower().endswith(".gameskin.gbx")
    )

    if file_path.endswith(".Material.Gbx"):
        material_name = os.path.basename(file_path).split(".")[0]

        # Add modifier
        folders = file_path.replace("\\", "/").split("/")
        if len(folders) >= 3 and folders[-3].lower() == "modifier":
            material_name = folders[-2] + "_" + material_name

        files_cache[file_path] = create_custom_material(material_name)
        files_cache[file_path]._fakeMaterial = True
    elif not force_recursive and (
        not recursive
        or not file_path.lower().endswith(".gbx")
        or file_path.lower().endswith(".texture.gbx")
        or file_path.lower().endswith(".light.gbx")
        or file_path.lower().endswith(".sound.gbx")
    ):
        files_cache[file_path] = Container()
    else:
        if log:
            logging.getLogger("tm2020.nice.parser").debug("load external: %s", file_path)

        try:
            files_cache[file_path] = parse_file(file_path, recursive=True)
        except Exception as e:
            # Most failures here are upstream content problems (a referenced
            # file is corrupt, missing the GBX magic, or a non-GBX blob that
            # ended up with a .Gbx extension). The outer parse can continue
            # without the external — log at debug with full context and
            # surface the error through the container so callers can decide.
            logging.getLogger("tm2020.nice.parser").debug(
                "external load failed for %s: %s", file_path, e,
            )
            files_cache[file_path] = Container(_error="Unable to load file: " + file_path, _message=repr(e))
            files_cache[file_path]._errors = [f"{repr(e)} in {file_path}"]

    return files_cache[file_path]


def generate_file(data, remove_external=True, reindex_nodes=False):
    # force compression
    data.header.body_compression = "compressed"

    # remove external nodes because we merge them
    if remove_external:
        data.referenceTable.numExternalNodes = 0
        data.referenceTable.externalFolders = None
        data.referenceTable.externalNodes = []

    nodes = data.nodes if "nodes" in data else None
    new_bytes = GbxStruct.build(data, gbx_data={}, nodes=nodes, reindex_nodes=reindex_nodes)

    return new_bytes


def create_custom_material(material_name):
    return Container(
        classId=0x090FD000,
        body=OrderedDict(
            [
                (
                    0x090FD000,
                    Container(
                        version=11,
                        isUsingGameMaterial=False,
                        # materialName="TM_" + material_name + "_asset",
                        materialName=material_name,
                        model="",
                        baseTexture="",
                        surfacePhysicId=6,
                        surfaceGameplayId=0,
                        link=material_name,
                        csts=[],
                        color=[],
                        uvAnim=[],
                        u07=[],
                        userTextures=[],
                        hidingGroup="",
                    ),
                ),
                (
                    0x090FD001,
                    Container(
                        version=5,
                        u01=-1,
                        tilingU=0,
                        tilingV=0,
                        textureSize=1.0,
                        u02=0,
                        isNatural=False,
                    ),
                ),
                (0x090FD002, Container(version=0, u01=0)),
                (0xFACADE01, None),
            ]
        ),
    )
