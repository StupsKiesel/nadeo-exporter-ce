"""Per-import asset loading and stock lookup; no persistent or global cache."""

from __future__ import annotations

import os
import hashlib
import logging

from .. import library, load, load_bytes
from ..errors import GbxbpyError
from ..model.asset import Asset
from ..resolve import BlockResolver, Resolver
from ..resolve.block_lookup import _aliases_for, _candidate_collection_roots
from .scene import ScenePlan

log = logging.getLogger("gbxbpy.map_import_v4")


class Dependencies:
    def __init__(
        self, resolver: Resolver, plan: ScenePlan,
        embedded: dict[str, bytes] | None = None,
    ) -> None:
        self.resolver = resolver
        self.plan = plan
        self.blocks = BlockResolver(resolver.gamedata_dir)
        self.loaded: dict[str, Asset | None] = {}
        self.embedded = embedded or {}
        self._embedded_sources: dict[str, bytes] = {}
        self._item_indexes: dict[str, dict[str, list[str]]] = {}
        self._spectators: list[tuple[str, Asset]] | None = None

    def spectator_meshes(self) -> list[tuple[str, Asset]]:
        """Stock spectator variants; the collection names ``<root>\\Spectators\\``."""
        if self._spectators is None:
            self._spectators = []
            roots = _candidate_collection_roots(
                self.resolver.gamedata_dir, preferred=_aliases_for(self.blocks.collection),
            )
            for root in roots:
                folder = os.path.join(root, "Spectators")
                if not library.isdir(folder):
                    continue
                for filename in sorted(library.listdir(folder), key=str.casefold):
                    if filename.casefold().endswith(".mesh.gbx"):
                        path = os.path.join(folder, filename)
                        asset = self.read(path)
                        if asset is not None:
                            self._spectators.append((path, asset))
                break
        return self._spectators

    def embedded_path(self, name: str) -> str | None:
        """Exact IDs first; suffix matching must never choose an ambiguous asset."""
        needle = name.replace("\\", "/").casefold()
        exact = [key for key in self.embedded if key.replace("\\", "/").casefold() == needle]
        matches = exact or [
            key for key in self.embedded
            if key.replace("\\", "/").casefold().endswith("/" + needle)
        ]
        if len(matches) > 1:
            self.plan.issue("ambiguous_embedded", name, "Multiple embedded resources match this ID")
            return None
        if not matches:
            return None
        identity = matches[0]
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        basename = identity.replace("\\", "/").rsplit("/", 1)[-1]
        path = os.path.abspath(os.path.join(self.plan.source_path + f".embedded-{digest}", basename))
        key = os.path.normcase(path)
        if key not in self._embedded_sources:
            log.info("Embedded resource %s -> %s", identity, path)
            self._embedded_sources[key] = self.embedded[identity]
        return path

    def is_embedded(self, path: str) -> bool:
        """Whether this source comes from the map's embedded resource table."""
        return os.path.normcase(os.path.abspath(path)) in self._embedded_sources

    def read(self, path: str) -> Asset | None:
        key = os.path.normcase(os.path.abspath(path))
        if key not in self.loaded:
            blob = self._embedded_sources.get(key)
            asset, error = self._parse(path, blob)
            # A broken extract parses badly; Trackmania can extract it again.
            if (asset is None or self._errors(asset)) and blob is None and library.refetch(path):
                asset, error = self._parse(path, blob)
            if asset is None:
                self.plan.issue("parse_failed", path, error)
            elif self._errors(asset):
                self.plan.issue("parser_diagnostics", path, "; ".join(map(str, self._errors(asset))))
            self.loaded[key] = asset
        return self.loaded[key]

    def _parse(self, path: str, blob: bytes | None) -> tuple[Asset | None, str]:
        try:
            asset = (
                load_bytes(blob, source_path=path, resolver=self.resolver, recursive=True)
                if blob is not None else load(path, resolver=self.resolver, recursive=True)
            )
        except (GbxbpyError, OSError) as exc:
            return None, str(exc)
        return asset, ""

    @staticmethod
    def _errors(asset: Asset) -> tuple:
        return tuple(getattr(asset.raw, "_errors", ()) or ())

    def resolve_path(self, reference: str, source: str) -> str | None:
        """Embedded, absolute or resolver path of ``reference``; reports misses."""
        path = self.embedded_path(reference)
        if path is None and os.path.isabs(reference) and library.ensure(reference):
            path = reference
        if path is None:
            path = self.resolver.resolve(reference, source_path=source)
        if not path:
            self.plan.issue("missing_reference", source, f"Cannot resolve {reference}")
            return None
        return path

    def reference(self, reference: str, source: str) -> tuple[str, Asset | None]:
        path = self.resolve_path(reference, source)
        if path is None:
            return reference, None
        return path, self.read(path)

    def stock(self, name: str, is_clip: bool) -> str | None:
        # Map custom-block IDs append a placement suffix to the resource name.
        marker = name.casefold().find(".block.gbx_")
        embedded_name = name[:marker + len(".block.gbx")] if marker >= 0 else name
        embedded = self.embedded_path(embedded_name)
        if embedded:
            return embedded
        path = self.blocks.find(name, is_clip=is_clip)
        return path or self.blocks.find(name, is_clip=not is_clip)

    def stock_block(
        self, name: str, is_clip: bool, kind: str, variant_index: int,
        mobil_index: int, mobil_variant_index: int,
    ) -> tuple[str | None, Asset | None]:
        """Read the block definition the map's collection uses for one placement.

        Vista collections override Stadium blocks (``BlueBay\\...\\Stadium\\``)
        and their versions win. A GameData extract only contains the geometry
        the game loaded, so when the selected variant/mobil of the override
        references prefabs that are not on disk, the next collection's
        definition (normally Stadium's) is used for that placement instead.

        Clips never fall back: a vista clip (``FC_Ground`` caps, ``ToDeco``
        caps, ``*FCBGround`` base slabs) has a different shape from Stadium's
        elevated ``FC_Air`` piece, so the override is kept without geometry
        and reported as ``missing_clip_geometry``.
        """
        stock = self.stock(name, is_clip)
        if stock is None or os.path.normcase(stock) in self._embedded_sources:
            return stock, self.read(stock) if stock else None
        candidates = (
            self.blocks.candidates(name, is_clip=is_clip)
            or self.blocks.candidates(name, is_clip=not is_clip) or [stock]
        )
        from ..model.clip import Clip

        first: tuple[str, Asset | None] | None = None
        is_clip_asset = False
        for path in candidates:
            asset = self.read(path)
            if first is None:
                first = (path, asset)
                is_clip_asset = isinstance(asset, Clip)
            if self._geometry_on_disk(asset, path, kind, variant_index, mobil_index, mobil_variant_index):
                if path != first[0]:
                    self.plan.issue(
                        "collection_fallback", first[0],
                        f"Selected {kind}[{variant_index}] mobil {mobil_index}/{mobil_variant_index} "
                        f"geometry is missing from GameData; using {path}",
                    )
                return path, asset
            if is_clip_asset:
                self.plan.issue(
                    "missing_clip_geometry", path,
                    f"Selected {kind}[{variant_index}] geometry is missing from GameData; clip skipped",
                )
                return first
        return first

    def _geometry_on_disk(
        self, asset: Asset | None, source: str, kind: str, variant_index: int,
        mobil_index: int, mobil_variant_index: int,
    ) -> bool:
        from ..model.block import Block
        from ..model.clip import Clip
        from .assets import pick_variant, select_mobil_alternative, select_mobil_group

        if not isinstance(asset, (Block, Clip)):
            return False
        variant = pick_variant(asset, kind, variant_index)
        if variant is None:
            return False
        group = select_mobil_group(variant, mobil_index)
        alternative = -1 if group < 0 else select_mobil_alternative(variant.mobils[group], mobil_variant_index)
        if alternative < 0:
            return True
        mobil = variant.mobils[group][alternative]
        return any(
            self.embedded_path(ref) or self.resolver.resolve(ref, source_path=source)
            for ref in (mobil.solid_path, mobil.prefab_path) if ref
        )

    def placement_item(self, placement: str, source: str) -> tuple[str, Asset | None]:
        """``<Collection>\\Items\\Vegetation\\<Placement>.Item.Gbx`` for a placement slot.

        The collection is the first GameData component of ``source`` (the
        prefab that owns the slot), so BlueBay vista prefabs draw BlueBay
        vegetation even inside a Stadium map.
        """
        filename = f"{placement}.Item.Gbx"
        relative = self.resolver.to_gamedata_relative(source) if source else ""
        collection = relative.replace("/", "\\").split("\\", 1)[0] if relative else ""
        if collection:
            candidate = os.path.join(self.resolver.gamedata_dir, collection, "Items", "Vegetation", filename)
            if library.isfile(candidate):
                return candidate, self.read(candidate)
        path = self.item(filename, collection)
        return (path, self.read(path)) if path else (filename, None)

    def item(self, name: str, collection: str) -> str | None:
        embedded = self.embedded_path(name)
        if embedded:
            return embedded
        direct = self.resolver.resolve(name, source_path=self.plan.source_path)
        if direct:
            return direct
        relative = name.replace("\\", os.sep).replace("/", os.sep)
        if not relative.casefold().endswith(".gbx"):
            relative += ".Item.Gbx"
        roots = _candidate_collection_roots(
            self.resolver.gamedata_dir,
            preferred=_aliases_for(collection or self.blocks.collection),
        )
        for root in roots:
            base = os.path.join(root, "Items")
            candidate = os.path.join(base, relative)
            if library.exists(candidate):
                return candidate
            if base not in self._item_indexes:
                index: dict[str, list[str]] = {}
                for directory, _dirs, files in library.walk(base):
                    for filename in files:
                        if filename.casefold().endswith(".item.gbx"):
                            index.setdefault(filename.casefold(), []).append(os.path.join(directory, filename))
                self._item_indexes[base] = index
            # Short Nadeo IDs may refer to a themed subdirectory.
            if os.path.basename(relative) == relative:
                matches = self._item_indexes[base].get(relative.casefold(), [])
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    self.plan.issue("ambiguous_item", name, f"Multiple item files under {base}")
                    return None
        self.plan.issue("missing_item", name, "Item not found in map resources or configured GameData roots")
        return None
