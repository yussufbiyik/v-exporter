"""Rewrite the placeholder texture slots of the uncooked ``Material.json`` files.

The CP77 Blender add-on builds its materials from ``*.Material.json`` only.  Those
files are written by the mesh exporter from ``CR2W`` material instances, and for
an NPV that ships its own material repo the exported slots keep the engine
fallbacks (``engine\\textures\\editor\\grey.xbm``, ``small_flat_normal.xbm``,
``makigai\\uvchecker.xbm``) instead of the textures the ``.mi`` chain actually
uses.  The result renders black/uvchecker even though every texture is on disk.

Three passes fix it:

1. resolve the ``.mi`` inheritance chain (parent first, child wins) for every
   material entry and fill a slot only when it still holds a placeholder;
2. normalise ``BaseColorScale``/``AlbedoScale`` whenever a colour slot is a
   placeholder *or* was remapped.  The exported scale is a tinting leftover of
   the placeholder slot (``{1, 0, 0.522511, 1}`` renders magenta) and multiplies
   the real texture to black/magenta in the add-on;
3. materials that have no resolvable colour at all (``Default``/``Default.001``
   from auto-named submeshes, ``neckMAT``/``armsMAT`` whose json only holds the
   engine template) borrow the resolved skin albedo/normal/roughness of a
   sibling entry in the same file, or of the body-skin entry in another file.

Two more passes handle the cases the ``.mi`` chain cannot express:

4. **multilayer flatten** - a garment entry whose ``MultilayerSetup`` is the
   engine default (or whose template is ``multilayered.mt``) is pointed at the
   real textile albedo of its own ``.mlsetup`` (found through the mesh json of
   the same stem).  The layer's ``colorScale`` colour is recorded in
   ``remap_tints.json`` because the add-on cannot evaluate the mix and the glTF
   formats drop it: ``blender_export`` multiplies it into the baked pixels.
5. **arm repoint** - the ``arms_hq`` ``Material.json`` carries the *body*
   instances, whose atlas is the torso map (V's arms then show a wrong skin
   patch).  Those entries are rewritten to the ``female_arm_<suffix>.mi`` chain
   of ``character_mat_instance`` and their slots resolved from it.
6. **mod garments** - an NPV garment can ship its own material tree without a
   cached ``.mi`` chain (``makigai\\uvchecker`` in ``BaseColor``).  Its
   ``.mi``/``.mlsetup``/``.mltemplate`` definition is followed when the cache has
   one; otherwise the mod's own texture folder is matched by material/mesh name
   and its albedo/normal/roughness maps are used.
7. **empty material list adoption** - a mesh's ``*.Material.json`` whose
   ``Materials`` list is empty (the exporter wrote no instance for that mesh, so
   the add-on renders the body skin) adopts the ``Materials`` (and
   ``TexturesList``/``MaterialTemplates``) of the sibling ``*material*`` file of
   the same mod/material folder.  The adopted entries then run through every
   pass above like their own.

The multilayer flatten is gated: eye/head/plastic families (``multilayer_*``,
``npc_*``, ``*_eye``, ``eyelashes*``, ``head_*``...) are rebuilt correctly by the
add-on from their own setup, so flattening them to a textile albedo was wrong.
Their entries are restored from ``*.Material.orig.json``.

Pure stdlib, idempotent and never raises.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Sequence

# ``base\acm_npv\<slug>\`` - the depot prefix an NPV material tree is cached under.
_NPV_PREFIX = re.compile(r"^base[\\/]acm_npv[\\/][^\\/]+[\\/]", re.IGNORECASE)

# Slots that came from the engine fallbacks; these are the ones worth replacing.
_PLACEHOLDER_PREFIXES = ("engine/textures/editor/", "engine/textures/small_")
_PLACEHOLDER_HINTS = ("makigai/uvchecker",)

# Pseudo materials: never worth following, they only carry defaults.
_SKIP_ROOTS = ("engine/textures/", "engine/materials/")

_MAX_DEPTH = 10

# (Material.json field, .mi fields tried in order)
_SLOT_SOURCES = (
    ("BaseColor", ("Albedo", "SecondaryAlbedo", "BaseColor")),
    ("Albedo", ("Albedo", "SecondaryAlbedo", "BaseColor")),
    ("Normal", ("Normal", "DetailNormal")),
    ("Roughness", ("Roughness",)),
    ("Metalness", ("Metalness",)),
)

_BACKUP_SUFFIX = ".orig.json"

#: Sidecar the flattened layers' colours are written to (read by
#: ``blender_export.set_tint_overrides`` so the tint lands in the baked pixels).
_TINT_SIDECAR = "remap_tints.json"

#: Colour slots and the multiplier key that has to stop tinting them.
_COLOR_SLOTS = ("BaseColor", "Albedo")
_UNIT_SCALE = {"X": 1, "Y": 1, "Z": 1, "W": 1}

#: Entries that carry no usable colour and may borrow one.
_FALLBACK_HINT = re.compile(r"^(default(\.\d+)?|neckmat|armsmat)$", re.IGNORECASE)

#: Names of entries that look like skin/head materials.
_SKIN_HINT = re.compile(
    r"(skin|pale|head|01_ca_|body|face|arm)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _posix(value: object) -> str:
    """Depot/spos path as lowercase-able forward-slash text."""
    return re.sub(r"/{2,}", "/", str(value).strip().replace("\\", "/"))


def _depot_text(value: str) -> str:
    """Depot path the way the game writes it (backslashes)."""
    return _posix(value).replace("/", "\\")


def _depot_ref(value: object) -> Optional[str]:
    """The depot string of a ``{"DepotPath": {"$value": ...}}`` wrapper or plain str."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        inner = value.get("DepotPath")
        ref = _depot_ref(inner) if inner is not None else None
        if ref:
            return ref
        ref = _depot_ref(value.get("$value"))
        if ref:
            return ref
    return None


def _chunk_of(value: object) -> Optional[dict]:
    """A ``CMaterialInstance`` chunk, accepting a wrapper around ``RootChunk``."""
    if not isinstance(value, dict):
        return None
    if "values" in value or "baseMaterial" in value:
        return value
    inner = value.get("RootChunk")
    return inner if isinstance(inner, dict) else None


def _values_of(chunk: dict) -> dict:
    """``{"values": [{"$type": ..., "<Field>": {...}}]}`` -> ``{Field: depot}``."""
    out: dict = {}
    for entry in chunk.get("values") or []:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if not isinstance(key, str) or key.startswith("$"):
                continue
            ref = _depot_ref(value)
            # ``{material}_d.xbm`` style templates cannot be resolved here.
            if ref and not any(ch in ref for ch in "{}*?"):
                out[key] = ref
            break
    return out


def _is_placeholder(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = _posix(value).lower()
    if not text:
        return True
    if text.startswith(_PLACEHOLDER_PREFIXES):
        return True
    return any(hint in text for hint in _PLACEHOLDER_HINTS)


def _is_checker(value: object) -> bool:
    """True for the ``uvchecker`` slot an NPV garment keeps when its chain is absent.

    Narrower than ``_is_placeholder``: ``grey.xbm`` is the eye/skin fallback and
    must not be repointed at a garment albedo.
    """
    if not isinstance(value, str):
        return False
    text = _posix(value).lower()
    return bool(text) and any(hint in text for hint in _PLACEHOLDER_HINTS)


def _skip_ref(rel: str) -> bool:
    text = _posix(rel).lower()
    return any(root in text for root in _SKIP_ROOTS)


def _is_unit_scale(value: object) -> bool:
    """True for a colour multiplier that leaves the texture unchanged."""
    if not isinstance(value, dict):
        return True
    try:
        return all(float(value.get(key, 1)) == 1.0 for key in _UNIT_SCALE)
    except (TypeError, ValueError):
        return False


def _load_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _npv_prefix(raw_dir: Path) -> str:
    """``base/acm_npv/<slug>/`` of this cache, or ""."""
    root = Path(raw_dir) / "base" / "acm_npv"
    try:
        children = sorted(child for child in root.iterdir() if child.is_dir())
    except OSError:
        return ""
    return f"base/acm_npv/{children[0].name}/" if children else ""


# ---------------------------------------------------------------------------
# .mi chain resolution
# ---------------------------------------------------------------------------
class _Resolver:
    """Merge the ``.mi`` inheritance chain of a material instance."""

    def __init__(self, raw_dir: Path, prefix: str, warnings: list) -> None:
        self.raw_dir = Path(raw_dir)
        self.prefix = prefix or ""
        self.warnings = warnings
        self._cache: dict = {}
        self._visiting: set = set()

    def _variants(self, rel: str) -> list:
        """Depot path candidates: prefixed copy first, then the vanilla one."""
        rel = _posix(rel).lstrip("/")
        if not rel:
            return []
        if self.prefix and rel.lower().startswith(self.prefix.lower()):
            stripped = rel[len(self.prefix):]
            return [rel, stripped] if stripped else [rel]
        return [self.prefix + rel, rel] if self.prefix else [rel]

    def _local(self, rel: str) -> Path:
        return self.raw_dir.joinpath(*[part for part in rel.split("/") if part])

    def _find(self, rel: str, suffixes: Sequence[str] = ("",)) -> Optional[Path]:
        for candidate in self._variants(rel):
            base = self._local(candidate)
            for suffix in suffixes:
                path = Path(str(base) + suffix)
                if path.is_file():
                    return path
        return None

    def texture_target(self, rel: str) -> Optional[str]:
        """Depot path to use for ``rel``: the cached sibling ``.png`` when there is one."""
        for candidate in self._variants(rel):
            base = self._local(candidate)
            png = base.with_suffix(".png")
            if png.is_file():
                swapped = str(Path(candidate).with_suffix(".png"))
                return _depot_text(swapped)
            if base.is_file() or Path(str(base) + ".json").is_file():
                return _depot_text(candidate)
        return None

    def json_of(self, rel: str) -> Optional[Path]:
        """Cached ``<rel>.json`` of a depot resource, or ``None``."""
        return self._find(rel, (".json",))

    def png_of(self, rel: str) -> Optional[str]:
        """Depot path of the cached sibling ``.png`` of a texture, or ``None``."""
        for candidate in self._variants(rel):
            if self._local(candidate).with_suffix(".png").is_file():
                return _depot_text(str(Path(candidate).with_suffix(".png")))
        return None

    # -- chain ---------------------------------------------------------
    def resolve(self, rel: str, depth: int = 0) -> dict:
        """All values of the ``.mi`` at ``rel``, its parents already merged."""
        path = self._find(rel, (".json",))
        if path is None:
            return {}
        return dict(self._resolve_path(path, depth))

    def _resolve_path(self, path: Path, depth: int) -> dict:
        key = str(path).lower()
        if key in self._cache:
            return self._cache[key]
        if key in self._visiting or depth > _MAX_DEPTH:
            return {}
        doc = _load_json(path)
        if doc is None:
            return {}
        self._visiting.add(key)
        try:
            merged = self._resolve_doc(doc, depth)
        finally:
            self._visiting.discard(key)
        self._cache[key] = merged
        return merged

    def _resolve_doc(self, doc: dict, depth: int) -> dict:
        data = doc.get("Data")
        if not isinstance(data, dict):
            return {}
        embedded: dict = {}
        order: list = []
        for item in data.get("EmbeddedFiles") or []:
            if not isinstance(item, dict):
                continue
            chunk = _chunk_of(item.get("Content"))
            if chunk is None:
                continue
            name = _depot_ref(item.get("FileName"))
            if name:
                embedded[_posix(name).lower()] = chunk
            order.append(chunk)
        root = _chunk_of(data.get("RootChunk"))
        if root is None:
            merged: dict = {}
            for chunk in reversed(order):
                merged.update(self._chunk_chain(chunk, depth, embedded))
            return merged
        return self._chunk_chain(root, depth, embedded)

    def _chunk_chain(self, chunk: dict, depth: int, embedded: dict) -> dict:
        out: dict = {}
        base = _depot_ref(chunk.get("baseMaterial"))
        if base and depth < _MAX_DEPTH:
            lowered = _posix(base).lower()
            if lowered in embedded:
                out.update(self._chunk_chain(embedded[lowered], depth + 1, embedded))
            elif not _skip_ref(base):
                out.update(self.resolve(base, depth + 1))
        out.update(_values_of(chunk))
        return out


# ---------------------------------------------------------------------------
# multilayer flatten: mlsetup -> a real textile albedo (+ tint sidecar)
# ---------------------------------------------------------------------------
_MLSETUP_EXT = ".mlsetup"
_MLSETUP_SUFFIX = ".mlsetup.json"
_MESH_SUFFIX = ".mesh.json"
_MATERIAL_SUFFIX = ".material.json"

#: The add-on's simple "metal base" handler reads these slots; the multilayer
#: handler rebuilds the surface from ``MultilayerSetup`` and bakes a checkered
#: DEBUG texture when that setup is the engine default.
_METAL_TEMPLATE = "engine\\materials\\metal_base.remt"
_MULTILAYER_KEYS = ("MultilayerSetup", "MultilayerMask", "MultilayerMask1", "MultilayerMask2")

#: Material families the flatten must never rewrite.  These are eye/head/plastic
#: materials: the add-on rebuilds them from their own (wrapper) multilayer setup
#: and they were only flattened by accident - the textile albedo + tint turned
#: every iris into green nylon.  Matching is by material name.
_FLATTEN_BLOCKED = re.compile(
    r"^(?:multilayer_|npc_|head_|eye|eyelashes|eyes_|cybereye|double_eye|stand_clear)"
    r"|_eye(?:_black)?$|_plastic_",
    re.IGNORECASE,
)


def _flatten_blocked(name: object) -> bool:
    """True for an eye/head/plastic material the multilayer flatten must skip."""
    text = str(name or "").strip()
    return bool(text) and bool(_FLATTEN_BLOCKED.search(text))


#: ``float`` colour multiplier of a layer, or ``None`` when the slot has no tint.
def _cname_text(value: object) -> str:
    """The string of a ``CName`` (``{"$value": "8f0487_69039f"}``)."""
    if isinstance(value, dict):
        return str(value.get("$value") or "").strip()
    return str(value or "").strip()


def _hex_tint(cname: str) -> Optional[tuple]:
    """``8f0487_69039f`` -> ``(0.561, 0.016, 0.529, 1.0)`` (first hex group is RGB)."""
    head = cname.split("_", 1)[0].strip().lower()
    if len(head) != 6 or any(char not in "0123456789abcdef" for char in head):
        return None
    return (
        int(head[0:2], 16) / 255.0,
        int(head[2:4], 16) / 255.0,
        int(head[4:6], 16) / 255.0,
        1.0,
    )


def _mlsetup_refs(doc: object) -> list:
    """Every ``.mlsetup`` depot path a mesh json references (first-seen order)."""
    found: list = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            text = _posix(value).lstrip("/*")
            if not text.lower().endswith(_MLSETUP_EXT) or "/" not in text:
                return
            if any(char in text for char in "{}*?"):
                return
            if text not in found:
                found.append(text)

    walk(doc)
    return found


def _load_tints(root: Path) -> dict:
    """Existing ``remap_tints.json`` (kept: a flattened entry lost its setup)."""
    doc = _load_json(root / _TINT_SIDECAR)
    if not isinstance(doc, dict):
        return {}
    out: dict = {}
    for name, rgba in doc.items():
        try:
            values = [float(value) for value in rgba][:4]
        except (TypeError, ValueError):
            continue
        if len(values) >= 3:
            out[str(name).lower()] = values + [1.0] * (4 - len(values))
    return out


def _mesh_mlsetup_index(raw_dir: Path) -> dict:
    """``{"by_mesh": {mesh stem: [mlsetup]}, "by_name": {mlsetup stem: [ref]}}``."""
    index: dict = {"by_mesh": {}, "by_name": {}}
    try:
        paths = sorted(Path(raw_dir).rglob(f"*{_MESH_SUFFIX}"))
    except OSError:
        return index
    for path in paths:
        doc = _load_json(path)
        if doc is None:
            continue
        refs = _mlsetup_refs(doc)
        if not refs:
            continue
        stem = path.name[: -len(_MESH_SUFFIX)].lower()
        index["by_mesh"][stem] = refs
        for ref in refs:
            name = Path(_posix(ref)).stem.lower()
            bucket = index["by_name"].setdefault(name, [])
            if ref not in bucket:
                bucket.append(ref)
    return index


def _is_multilayer(entry: dict) -> bool:
    """True for an entry the add-on rebuilds from a (placeholder) multilayer setup."""
    data = entry.get("Data")
    if isinstance(data, dict) and data.get("MultilayerSetup") is not None:
        return True
    template = str(entry.get("MaterialTemplate") or "").lower()
    return "multilayered" in template


def _mlsetup_for(entry: dict, mesh_refs: Sequence[str], index: dict,
                 primary: Optional[str]) -> Optional[str]:
    """The real setup of one entry: its own name first, then the mesh's primary."""
    name = str(entry.get("Name") or "").strip().lower()
    if name:
        for ref in mesh_refs:
            if Path(_posix(ref)).stem.lower() == name:
                return ref
        for ref in index["by_name"].get(name, []):
            return ref
        # Loose match: ``silver`` -> ``silver_hardware``,
        # ``personal_link`` -> ``a0_001__personal_link_1``.
        for ref in mesh_refs:
            stem = Path(_posix(ref)).stem.lower()
            if stem.startswith(name) or name.startswith(stem):
                return ref
            if len(name) >= 6 and name in stem:
                return ref
    return primary


def _mlsetup_layers(doc: Optional[dict]) -> list:
    data = doc.get("Data") if isinstance(doc, dict) else None
    root = data.get("RootChunk") if isinstance(data, dict) else None
    layers = root.get("layers") if isinstance(root, dict) else None
    if not isinstance(layers, list):
        return []
    return [layer for layer in layers if isinstance(layer, dict)]


def _is_default_albedo(ref: str) -> bool:
    """True for the engine's own fallback maps - never a garment's colour."""
    text = _posix(ref).lower()
    return "/materials/default/" in text or "/materials/special/" in text


def _pick_layer(layers: list, resolver: _Resolver) -> Optional[tuple]:
    """``(albedo png depot, layer template stem, colorScale CName)`` of a layer.

    The first layer whose textile albedo is cached and that carries a colour
    ``CName`` wins (that is the layer the garment is actually coloured by);
    an uncoulored-but-cached layer is the fallback.
    """
    fallback: Optional[tuple] = None
    for layer in layers:
        template = _depot_ref(layer.get("material"))
        if not template or _skip_ref(template):
            continue
        template_doc = _load_json(resolver.json_of(template) or Path(""))
        root = (template_doc or {}).get("Data", {}).get("RootChunk") \
            if isinstance(template_doc, dict) else None
        if not isinstance(root, dict):
            continue
        albedo = _depot_ref(root.get("colorTexture"))
        if not albedo or _is_default_albedo(albedo):
            continue
        png = resolver.png_of(albedo)
        if not png:
            continue
        try:
            opacity = float(layer.get("opacity", 1) or 0)
        except (TypeError, ValueError):
            opacity = 1.0
        if opacity <= 0:
            continue
        stem = Path(_posix(template)).stem
        cname = _cname_text(layer.get("colorScale"))
        if cname and not cname.lower().startswith("null"):
            return png, stem, cname
        if fallback is None:
            fallback = (png, stem, cname)
    return fallback


def _flatten_entry(entry: dict, layers: list, resolver: _Resolver) -> Optional[dict]:
    """Point an entry at a real textile albedo and drop its multilayer setup."""
    picked = _pick_layer(layers, resolver)
    if picked is None:
        return None
    png, stem, cname = picked
    data = entry.get("Data")
    if not isinstance(data, dict):
        return None
    for field in _COLOR_SLOTS:
        data[field] = png
        data[f"{field}Scale"] = dict(_UNIT_SCALE)
    entry["MaterialTemplate"] = _METAL_TEMPLATE
    for key in _MULTILAYER_KEYS:
        data.pop(key, None)
    return {"image": png, "layer": stem, "colorScale": cname}


# ---------------------------------------------------------------------------
# arm repoint: the arms_hq entries must use the arm atlas, not the body one
# ---------------------------------------------------------------------------
_ARM_BODY_INSTANCE = re.compile(
    r"(?:^|[\\/])skin[\\/](?:player|character)_mat_instance[\\/]female[\\/]body"
    r"[\\/]female_(?P<suffix>[^\\/]+)\.mi$",
    re.IGNORECASE,
)
_ARM_INSTANCE_DIR = (
    "base\\characters\\common\\skin\\character_mat_instance\\female\\arms"
)


def _arm_instance(suffix: str) -> str:
    return _ARM_INSTANCE_DIR + "\\female_arm_" + suffix + ".mi"


def _arm_repair(entry: dict, resolver: _Resolver, warnings: list) -> Optional[dict]:
    """Repoint one ``arms_hq`` skin entry at its ``female_arm_<suffix>.mi`` chain.

    The exported ``Material.json`` of an arm mesh carries the *body* instances,
    whose atlas is the torso/leg/head body map (V's arms then show the wrong
    skin patch).  The arm atlas lives in the ``character_mat_instance`` chain.
    Returns ``{"base", "writes", "image"}`` or ``None`` when nothing changed.
    """
    base = _depot_ref(entry.get("BaseMaterial")) or ""
    match = _ARM_BODY_INSTANCE.search(_depot_text(base))
    if not match:
        return None
    suffix = match.group("suffix")
    target = _arm_instance(suffix)
    if resolver.json_of(target) is None:
        warnings.append(
            f"{entry.get('Name') or '?'}: arm instance not in cache: {target}"
        )
        return None
    chain = resolver.resolve(target)
    writes: dict = {}
    for field, sources in _SLOT_SOURCES:
        wanted = next((chain[src] for src in sources if chain.get(src)), None)
        if not wanted or _skip_ref(wanted):
            continue
        resolved = resolver.texture_target(wanted)
        if resolved is not None:
            writes[field] = resolved
    if not writes:
        warnings.append(f"{entry.get('Name') or '?'}: arm chain has no textures: {target}")
        return None
    data = entry.get("Data")
    if not isinstance(data, dict):
        return None
    data.update(writes)
    entry["BaseMaterial"] = target
    entry["MaterialTemplate"] = _METAL_TEMPLATE
    for field in _COLOR_SLOTS:
        data[f"{field}Scale"] = dict(_UNIT_SCALE)
    return {"base": target, "writes": writes, "image": writes.get("BaseColor")}


# ---------------------------------------------------------------------------
# mod garments: the textures an NPV ships itself (no cached .mi chain)
# ---------------------------------------------------------------------------
_GARMENT_DEF_SUFFIXES = (".mi.json", ".mlsetup.json", ".mltemplate.json")
_TEXTURE_FILE_SUFFIXES = (".xbm", ".dds", ".png")

#: Trim pieces (stitches, zips...) that ship no textures of their own.
_BORROW_TRIM = re.compile(r"(stitch|trim|seam|button|buckle|rivet|zip)", re.IGNORECASE)


def _cache_slug_dir(raw_dir: Path) -> Path:
    """``.../base/acm_npv/<slug>`` if this is an NPV cache, else the cache root."""
    root = Path(raw_dir)
    npv = root / "base" / "acm_npv"
    try:
        for child in sorted(npv.iterdir()):
            if child.is_dir():
                return child
    except OSError:
        pass
    return root


def _mod_source_dir(material_path: Path, raw_dir: Path) -> Optional[Path]:
    """The mod's ``textures`` folder above a ``Material.json``, or ``None``."""
    root = Path(raw_dir)
    stop = _cache_slug_dir(root)
    folder = Path(material_path).parent
    for _ in range(6):
        candidate = folder / "textures"
        if candidate.is_dir():
            return candidate
        if folder == stop or folder == root or folder.parent == folder:
            return None
        folder = folder.parent
    return None


def _texture_kind(stem: str) -> str:
    """``albedo``/``normal``/``roughness``/``metalness`` of a texture file stem."""
    low = stem.lower()
    if any(hint in low for hint in ("normal", "n_n", "_n_")) or low.endswith("_n"):
        return "normal"
    if any(hint in low for hint in ("roughness", "refl", "r_r", "_r_")) or low.endswith("_r"):
        return "roughness"
    if any(hint in low for hint in ("metallic", "metalness", "m_m", "_m_")) or low.endswith("_m"):
        return "metalness"
    return "albedo"


def _texture_files(folder: Path) -> list:
    """Texture files of one folder (``.xbm`` preferred over its ``.dds`` twin)."""
    out: list = []
    try:
        children = sorted(path for path in Path(folder).iterdir() if path.is_file())
    except OSError:
        return out
    for path in children:
        suffix = path.suffix.lower()
        if suffix not in _TEXTURE_FILE_SUFFIXES:
            continue
        if suffix == ".dds" and path.with_suffix(".xbm").is_file():
            continue
        out.append(path)
    return out


def _texture_dirs(source: Path, depth: int = 3) -> list:
    """``source`` and its subfolders, breadth first and bounded."""
    out = [source]
    level = [source]
    for _ in range(depth):
        nxt: list = []
        for folder in level:
            try:
                children = sorted(path for path in folder.iterdir() if path.is_dir())
            except OSError:
                children = []
            out.extend(children)
            nxt.extend(children)
        level = nxt
        if not level:
            break
    return out


def _norm_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _garment_texture_dir(source: Path, name: str, mesh_stem: str) -> Optional[Path]:
    """The mod's texture folder of one garment material (name, then mesh token)."""
    wanted = _norm_token(name)
    tokens = {
        token for token in re.split(r"[^a-z0-9]+", str(mesh_stem).lower())
        if len(token) >= 4
    }
    best: Optional[Path] = None
    best_score = 0
    seen: set = set()
    for folder in _texture_dirs(source):
        files = _texture_files(folder)
        if not files:
            continue
        # ``textures/pants/0`` / ``1``: colour variants of their parent folder.
        target = folder.parent if re.fullmatch(r"\d+", folder.name) else folder
        key = str(target).lower()
        if key in seen:
            continue
        seen.add(key)
        stems = [_norm_token(path.stem) for path in files]
        score = 0
        if wanted and len(wanted) >= 4 and any(wanted in stem for stem in stems):
            score = 3                       # texture files carry the material name
        elif wanted and _norm_token(target.name) == wanted:
            score = 2                       # folder is named after the material
        elif _norm_token(target.name) in tokens:
            score = 1                       # folder is named after the mesh part
        if score > best_score:
            best, best_score = target, score
    return best


def _variant_dir(folder: Path, index: int) -> Path:
    """``<folder>/<0|1|...>`` when the mod ships colour variants as subfolders."""
    try:
        subs = sorted(
            path for path in folder.iterdir()
            if path.is_dir() and re.fullmatch(r"\d+", path.name) and _texture_files(path)
        )
    except OSError:
        return folder
    if not subs:
        return folder
    return subs[min(index, len(subs) - 1)]


def _pick_texture(folder: Path, kind: str) -> Optional[Path]:
    """The first texture of ``kind`` in ``folder``."""
    for path in _texture_files(folder):
        if _texture_kind(path.stem) == kind:
            return path
    return None


def _mod_definition(source: Optional[Path], name: str) -> Optional[Path]:
    """A ``.mi``/``.mlsetup``/``.mltemplate`` json the mod ships for ``name``."""
    if source is None:
        return None
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    base = source.parent if source.name.lower() == "textures" else source
    for suffix in _GARMENT_DEF_SUFFIXES:
        try:
            files = sorted(base.rglob(f"*{suffix}"))
        except OSError:
            continue
        for path in files:
            stem = path.name[: -len(suffix)].lower()
            if stem == wanted or stem.startswith(wanted) or wanted.startswith(stem):
                return path
    return None


def _definition_slots(path: Path, resolver: _Resolver) -> dict:
    """Albedo/normal/roughness depot paths of a mod ``.mi``/``.mlsetup`` json."""
    writes: dict = {}
    try:
        rel = str(path.relative_to(resolver.raw_dir)).replace("\\", "/")
    except ValueError:
        return writes
    if rel.lower().endswith(".json"):
        rel = rel[: -len(".json")]
    if path.name.lower().endswith(_MLSETUP_SUFFIX):
        layers = _mlsetup_layers(_load_json(path))
        picked = _pick_layer(layers, resolver)
        if picked is None:
            return writes
        writes["BaseColor"] = picked[0]
        template = next(
            (_depot_ref(layer.get("material")) for layer in layers
             if _depot_ref(layer.get("material"))),
            None,
        )
        template_path = resolver.json_of(template) if template else None
        template_doc = _load_json(template_path) if template_path else None
        root = ((template_doc or {}).get("Data") or {}).get("RootChunk") or {}
        for field, key in (("Normal", "normalTexture"), ("Roughness", "roughnessTexture")):
            ref = _depot_ref(root.get(key))
            target = resolver.png_of(ref) if ref else None
            if target:
                writes[field] = target
        return writes
    chain = resolver.resolve(rel)
    for field, sources in _SLOT_SOURCES:
        if field not in ("BaseColor", "Normal", "Roughness"):
            continue
        wanted = next((chain[src] for src in sources if chain.get(src)), None)
        if wanted and not _skip_ref(wanted):
            target = resolver.texture_target(wanted)
            if target:
                writes[field] = target
    return writes


def _resolve_mod_garments(
    path: Path,
    entries: list,
    resolver: _Resolver,
    tints: dict,
    result: dict,
    warnings: list,
) -> bool:
    """Fill the placeholder slots of garments whose textures only the NPV ships.

    The material arrives with ``makigai\\uvchecker`` in ``BaseColor`` and no
    cached ``.mi`` chain, so neither the chain resolver nor the flatten can help.
    A ``.mi``/``.mlsetup`` definition of the mod is followed when present;
    otherwise the mod's own texture folder is matched by material and mesh name.
    A trim entry without textures of its own borrows the garment albedo already
    resolved in the same file (never the checker).  Every decision is recorded.
    """
    root = Path(resolver.raw_dir)
    source = _mod_source_dir(path, root)
    mesh_stem = path.name[: -len(_MATERIAL_SUFFIX)].lower()
    changed = False
    index = 0
    for entry, _mapped in entries:
        data = entry.get("Data")
        if not isinstance(data, dict) or not _is_checker(data.get("BaseColor")):
            continue
        name = str(entry.get("Name") or "").strip()
        if _flatten_blocked(name):
            continue
        definition = _mod_definition(source, name)
        writes = _definition_slots(definition, resolver) if definition is not None else {}
        how = "definition"
        folder: Optional[Path] = None
        if not writes.get("BaseColor"):
            # No usable definition: match the mod's own texture folder.
            definition = None
            writes = {}
            folder = _garment_texture_dir(source, name, mesh_stem) if source else None
            if folder is not None:
                folder = _variant_dir(folder, index)
                for field, kind in (
                    ("BaseColor", "albedo"), ("Normal", "normal"), ("Roughness", "roughness")
                ):
                    chosen = _pick_texture(folder, kind)
                    if chosen is None:
                        continue
                    try:
                        rel = str(chosen.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        continue
                    target = resolver.texture_target(rel)
                    if target:
                        writes[field] = target
            how = "texture folder"
        index += 1
        if not writes.get("BaseColor"):
            continue
        for field in _COLOR_SLOTS:
            data[field] = writes["BaseColor"]
            data[f"{field}Scale"] = dict(_UNIT_SCALE)
        for field in ("Normal", "Roughness"):
            if field in writes:
                data[field] = writes[field]
        entry["BaseMaterial"] = _METAL_TEMPLATE
        entry["MaterialTemplate"] = _METAL_TEMPLATE
        for key in _MULTILAYER_KEYS:
            data.pop(key, None)
        tints.pop(name.lower(), None)          # a real albedo needs no tint
        result["garments"].append(
            {
                "entry": name,
                "file": path.name,
                "how": how,
                "source": str(definition) if definition else (
                    str(folder) if folder is not None else ""),
                "image": writes["BaseColor"],
                "normal": writes.get("Normal", ""),
                "roughness": writes.get("Roughness", ""),
            }
        )
        changed = True

    # Trim entries with no textures of their own: borrow the garment albedo that
    # is already resolved in this file, so no checker survives on them either.
    pool = [
        (name, entry)
        for entry, _mapped in entries
        for name in [str(entry.get("Name") or "").strip()]
        if isinstance(entry.get("Data"), dict)
        and not _is_placeholder(entry["Data"].get("BaseColor"))
        and entry["Data"].get("BaseColor")
        and not _flatten_blocked(name)
    ]
    pool.sort(key=lambda item: 0 if item[0].lower().startswith("ml_") else 1)
    for entry, _mapped in entries:
        data = entry.get("Data")
        if not isinstance(data, dict) or not _is_checker(data.get("BaseColor")):
            continue
        name = str(entry.get("Name") or "").strip()
        if _flatten_blocked(name) or not _BORROW_TRIM.search(name):
            continue
        for candidate_name, candidate in pool:
            candidate_data = candidate.get("Data")
            if not candidate_data or candidate_data is data:
                continue
            for field in _COLOR_SLOTS:
                data[field] = candidate_data[field]
                data[f"{field}Scale"] = dict(_UNIT_SCALE)
            writes = {"BaseColor": candidate_data.get("BaseColor")}
            for field in ("Normal", "Roughness"):
                if candidate_data.get(field) and not _is_placeholder(candidate_data[field]):
                    data[field] = candidate_data[field]
                    writes[field] = candidate_data[field]
            entry["BaseMaterial"] = _METAL_TEMPLATE
            entry["MaterialTemplate"] = _METAL_TEMPLATE
            for key in _MULTILAYER_KEYS:
                data.pop(key, None)
            tint = tints.get(candidate_name.lower())
            if tint:
                tints[name.lower()] = list(tint)
            else:
                tints.pop(name.lower(), None)
            result["garments"].append(
                {
                    "entry": name,
                    "file": path.name,
                    "how": f"borrowed {candidate_name}",
                    "source": path.name,
                    "image": writes["BaseColor"],
                    "normal": writes.get("Normal", ""),
                    "roughness": writes.get("Roughness", ""),
                }
            )
            changed = True
            break
    return changed



# ---------------------------------------------------------------------------
# empty Materials[]: adopt the sibling *material* file of the same folder
# ---------------------------------------------------------------------------
#: Tint a flattened metal layer would otherwise carry (``silver`` decodes to a
#: green garment colour); hardware must stay neutral.
_METAL_TINT = (0.72, 0.72, 0.74, 1.0)
_METAL_TINT_NAME = re.compile(r"silver|steel|zipper|metal|chrome", re.IGNORECASE)


def _adoption_candidates(path: Path) -> list:
    """Sibling ``*material*.Material.json`` files of an empty material list.

    The exporter writes one ``Material.json`` per mesh, but a mesh that carries
    no material instance of its own ends up with ``"Materials": []`` while the
    mod's ``material\\*_material.Material.json`` holds the real entries.  Look in
    the same folder and in its ``material`` sibling folder(s).
    """
    parent = Path(path).parent
    folders = [parent]
    try:
        for child in sorted(parent.parent.iterdir()):
            if child.is_dir() and "material" in child.name.lower() and child != parent:
                folders.append(child)
    except OSError:
        pass
    out: list = []
    for folder in folders:
        try:
            files = sorted(entry for entry in folder.iterdir() if entry.is_file())
        except OSError:
            continue
        for file in files:
            if not file.name.lower().endswith(_MATERIAL_SUFFIX):
                continue
            stem = file.name[: -len(_MATERIAL_SUFFIX)]
            if file == path or "material" not in stem.lower():
                continue
            out.append(file)
    return out


def _material_stem_score(mesh_stem: str, candidate_stem: str) -> int:
    """Shared name tokens of a mod garment vs its generated material names."""
    def tokens(value: str) -> set:
        return {
            token for token in re.split(r"[^a-z0-9]+", str(value).lower())
            if len(token) >= 4 and token != "material"
        }
    return len(tokens(mesh_stem) & tokens(candidate_stem))


def _adopt_materials(path: Path, doc: dict, result: dict, warnings: list, note) -> bool:
    """Fill an empty ``Materials`` list from the sibling ``*material*`` file.

    Idempotent: a file that already lists materials is left alone.  The adopted
    entries take part in the normal per-entry passes afterwards; an adopted
    entry whose template is still multilayer/uvchecker is forced onto the plain
    metal base so the add-on does not bake the grey DEBUG checker.
    """
    current = doc.get("Materials")
    if isinstance(current, list) and current:
        return False
    mesh_stem = Path(path).name[: -len(_MATERIAL_SUFFIX)]
    best: Optional[Path] = None
    best_score = 0
    for candidate in _adoption_candidates(path):
        stem = candidate.name[: -len(_MATERIAL_SUFFIX)]
        score = _material_stem_score(mesh_stem, stem)
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < 2:
        warnings.append(f"{Path(path).name}: empty Materials[] and no sibling *material* file")
        return False
    source = _load_json(best)
    entries = source.get("Materials") if isinstance(source, dict) else None
    if not isinstance(entries, list) or not entries:
        warnings.append(f"{Path(path).name}: sibling {best.name} has no Materials[]")
        return False

    doc["Materials"] = json.loads(json.dumps(entries))
    for key in ("TexturesList", "MaterialTemplates"):
        value = source.get(key)
        if not value:
            continue
        mine = doc.get(key)
        if not mine:
            doc[key] = json.loads(json.dumps(value))
        elif isinstance(mine, list) and isinstance(value, list):
            for item in value:
                if item not in mine:
                    mine.append(item)

    # Adopted entries with a multilayer/uvchecker template cannot be evaluated
    # here; force the flat template, unless their BaseColor is still a checker
    # (the mod-garment pass owns those slots).
    forced = 0
    for entry in doc["Materials"]:
        if not isinstance(entry, dict):
            continue
        template = str(entry.get("MaterialTemplate") or "").lower()
        if not (_is_multilayer(entry) or "uvchecker" in template or "layered" in template):
            continue
        data = entry.get("Data")
        if not isinstance(data, dict) or _is_checker(data.get("BaseColor")):
            continue
        entry["BaseMaterial"] = _METAL_TEMPLATE
        entry["MaterialTemplate"] = _METAL_TEMPLATE
        for key in _MULTILAYER_KEYS:
            data.pop(key, None)
        forced += 1

    names = [str(entry.get("Name") or "") for entry in doc["Materials"] if isinstance(entry, dict)]
    result["adopted"].append(
        {
            "file": Path(path).name,
            "from": best.name,
            "materials": names,
            "forced_template": forced,
        }
    )
    note(
        "info",
        f"{Path(path).name}: empty Materials[] -> adopted {len(names)} entr(ies) from "
        f"sibling {best.name} ({', '.join(name for name in names if name)})"
        + (f"; {forced} forced onto metal_base.remt" if forced else ""),
    )
    return True


# ---------------------------------------------------------------------------
# per-entry remap
# ---------------------------------------------------------------------------
def _remap_entry(entry: dict, resolver: _Resolver, warnings: list) -> dict:
    """Field writes and scale fixes for one ``Materials[]`` entry.

    Returns ``{"writes": {...}, "scales": {...}, "targets": {...}}``.
    """
    result = {"writes": {}, "scales": {}, "targets": {}, "meta_changed": False}
    data = entry.get("Data")
    if not isinstance(data, dict):
        return result

    base = _depot_ref(entry.get("BaseMaterial"))
    chain = {} if (not base or _skip_ref(base)) else resolver.resolve(base)

    writes: dict = result["writes"]
    targets: dict = result["targets"]
    if chain:
        for field, sources in _SLOT_SOURCES:
            wanted = next((chain[src] for src in sources if chain.get(src)), None)
            resolved = None
            if wanted and not _skip_ref(wanted):
                resolved = resolver.texture_target(wanted)
            if resolved is not None:
                targets[field] = resolved

            present = field in data
            current = data.get(field)
            if present and not _is_placeholder(current):
                continue
            if not present and field != "BaseColor":
                continue
            if resolved is None:
                if wanted and not _skip_ref(wanted):
                    warnings.append(
                        f"{entry.get('Name') or '?'}: {field} source not in cache: {wanted}"
                    )
                continue
            if present and _posix(resolved).lower() == _posix(current).lower():
                continue
            writes[field] = resolved

    # Rule 1: a colour slot that is still a placeholder must stop being tinted,
    # no matter whether a replacement texture was found.  The exported scale is
    # a leftover of the placeholder slot ({1, 0, 0.52, 1} renders magenta).
    scales: dict = result["scales"]
    for field in _COLOR_SLOTS:
        scale_key = f"{field}Scale"
        scale = data.get(scale_key)
        if not isinstance(scale, dict) or _is_unit_scale(scale):
            continue
        if field in writes or _is_placeholder(data.get(field)) or field not in data:
            scales[scale_key] = dict(_UNIT_SCALE)
    return result


def _borrow(
    entry: dict,
    candidate: dict,
    reason: str,
    result: dict,
    fallbacks: list,
) -> bool:
    """Fill this entry's placeholder colour slots from a resolved sibling."""
    data = entry.get("Data")
    if not isinstance(data, dict):
        return False
    targets = candidate["targets"]
    writes = result["writes"]
    copied = False
    for field in _COLOR_SLOTS + ("Normal", "Roughness"):
        source = targets.get(field)
        if source is None:
            continue
        if field in writes:
            continue
        current = data.get(field)
        if field in data and not _is_placeholder(current):
            continue
        if field not in data and field not in ("BaseColor",):
            continue
        writes[field] = source
        copied = True
    if copied:
        for field in _COLOR_SLOTS:
            scale_key = f"{field}Scale"
            if isinstance(data.get(scale_key), dict) and not _is_unit_scale(data[scale_key]):
                result["scales"][scale_key] = dict(_UNIT_SCALE)
        fallbacks.append(
            {
                "entry": entry.get("Name"),
                "source": candidate.get("name"),
                "file": candidate.get("file"),
                "reason": reason,
            }
        )
    return copied


def _restore_gated(path: Path, materials: list, result: dict) -> bool:
    """Put the gated eye/head/plastic entries back the way the exporter wrote them.

    ``<name>.Material.orig.json`` is the untouched export of this file; an
    earlier, ungated flatten rewrote some of its entries in place.  Only the
    gated ones are restored, so every other fix of this pass survives.
    """
    backup = Path(str(path)[: -len(".json")] + _BACKUP_SUFFIX)
    if not backup.is_file():
        return False
    orig = _load_json(backup)
    source = orig.get("Materials") if isinstance(orig, dict) else None
    if not isinstance(source, list):
        return False
    by_name: dict = {}
    for item in source:
        if isinstance(item, dict):
            by_name[str(item.get("Name") or "").strip().lower()] = item
    changed = False
    for index, entry in enumerate(materials):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or "").strip()
        if not _flatten_blocked(name):
            continue
        original = by_name.get(name.lower())
        if original is None or original == entry:
            continue
        materials[index] = original
        result["gated_restored"].append({"entry": name, "file": path.name})
        changed = True
    return changed


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def remap_material_jsons(raw_dir, logger=None) -> dict:
    """Fill placeholder slots of every ``*.Material.json`` under ``raw_dir``.

    Returns ``{"files", "materials", "remapped", "skipped", "scales_fixed",
    "scales_fixed_unresolved", "fallbacks", "field_counts", "warnings",
    "flattened", "tinted", "mlsetups_found", "layers_used",
    "flattened_entries", "armed", "gated_restored", "garments", "adopted"}``.
    Modified files are backed up once as ``<name>.orig.json``.
    """
    result = {
        "files": 0,
        "materials": 0,
        "remapped": 0,
        "skipped": 0,
        "scales_fixed": 0,
        "scales_fixed_unresolved": 0,
        "fallbacks": [],
        "field_counts": {},
        "warnings": [],
        "flattened": 0,
        "tinted": 0,
        "mlsetups_found": 0,
        "layers_used": {},
        "flattened_entries": [],
        "armed": [],
        "gated_restored": [],
        "garments": [],
        "adopted": [],
    }
    warnings = result["warnings"]
    fallbacks = result["fallbacks"]

    def note(level: str, message: str) -> None:
        if logger is None:
            return
        try:
            getattr(logger, level)(message)
        except Exception:
            pass

    try:
        root = Path(raw_dir)
        paths = sorted(path for path in root.rglob("*.Material.json") if path.is_file())
    except OSError as exc:
        warnings.append(f"Material remap: cannot scan {raw_dir}: {exc}")
        return result

    resolver = _Resolver(root, _npv_prefix(root), warnings)
    index = _mesh_mlsetup_index(root)
    tints: dict = _load_tints(root)
    # A tint for an eye/head/plastic material is a leftover of the ungated
    # flatten; the restored entries carry their own multilayer colour.
    tints = {name: rgba for name, rgba in tints.items() if not _flatten_blocked(name)}
    setups_seen: set = set()
    docs: list = []
    for path in paths:
        doc = _load_json(path)
        adopted = False
        if isinstance(doc, dict):
            try:
                adopted = _adopt_materials(path, doc, result, warnings, note)
            except Exception as exc:                       # never crash the pipeline
                warnings.append(f"{path.name}: material adoption failed: {exc}")
        materials = doc.get("Materials") if isinstance(doc, dict) else None
        if not isinstance(materials, list):
            warnings.append(f"{path.name}: no Materials[] list")
            continue
        result["files"] += 1
        stem = path.name[: -len(_MATERIAL_SUFFIX)].lower()
        mesh_refs = index["by_mesh"].get(stem) or []
        names = {
            str(item.get("Name") or "").strip().lower()
            for item in materials
            if isinstance(item, dict)
        }
        primary = next(
            (ref for ref in mesh_refs if Path(_posix(ref)).stem.lower() in names), None
        ) or (mesh_refs[0] if mesh_refs else None)
        is_arm = "arms_hq" in str(path).lower()
        # Eye/head/plastic entries an earlier flatten rewrote: undo that first,
        # so the gated families stay exactly as the exporter wrote them.
        restored = _restore_gated(path, materials, result)
        entries = []
        dirty = restored or adopted
        for entry in materials:
            if not isinstance(entry, dict):
                continue
            result["materials"] += 1
            name = str(entry.get("Name") or "")
            # Garments: a placeholder multilayer setup bakes the grey DEBUG checker.
            # Eye/head/plastic families are gated: the add-on rebuilds them.
            if _is_multilayer(entry) and not _flatten_blocked(name):
                ref = _mlsetup_for(entry, mesh_refs, index, primary)
                layers = _mlsetup_layers(_load_json(resolver.json_of(ref)) if ref else None)
                if layers:
                    setups_seen.add(_posix(ref or "").lower())
                    done = _flatten_entry(entry, layers, resolver)
                    if done:
                        dirty = True
                        result["flattened"] += 1
                        result["flattened_entries"].append(
                            {"entry": name, "mlsetup": Path(_posix(ref or "")).stem,
                             "layer": done["layer"], "image": done["image"]}
                        )
                        counts = result["layers_used"]
                        counts[done["layer"]] = counts.get(done["layer"], 0) + 1
                        tint = _hex_tint(done["colorScale"])
                        if tint and name:
                            tints[name.lower()] = list(tint)
                            result["tinted"] += 1
                else:
                    warnings.append(
                        f"{path.name}/{name}: multilayer setup not resolvable"
                        + (f": {ref}" if ref else "")
                    )
            # Arms: the exported chain is the body instance -> wrong skin atlas.
            if is_arm:
                repaired = _arm_repair(entry, resolver, warnings)
                if repaired:
                    dirty = True
                    result["armed"].append({"entry": name, **repaired})
            try:
                mapped = _remap_entry(entry, resolver, warnings)
            except Exception as exc:                       # never crash the pipeline
                warnings.append(f"{path.name}/{entry.get('Name')}: {exc}")
                mapped = {"writes": {}, "scales": {}, "targets": {}}
            entries.append((entry, mapped))
        docs.append((path, doc, entries, dirty))

    result["mlsetups_found"] = len(setups_seen)

    # Candidates for rule 3: entries whose BaseColor really resolves.
    same_file_pool: dict = {}
    global_pool: list = []
    for path, _doc, entries, _dirty in docs:
        pool = []
        for entry, mapped in entries:
            target = mapped["targets"].get("BaseColor")
            if not target or _is_placeholder(target):
                continue
            name = str(entry.get("Name") or "")
            candidate = {
                "name": name,
                "file": path.name,
                "targets": mapped["targets"],
            }
            pool.append(candidate)
            global_pool.append(candidate)
        pool.sort(key=lambda item: 0 if _SKIN_HINT.search(item["name"]) else 1)
        same_file_pool[str(path)] = pool
    global_pool.sort(key=lambda item: 0 if _SKIN_HINT.search(item["name"]) else 1)

    for path, doc, entries, dirty in docs:
        changed = dirty
        pool = same_file_pool.get(str(path)) or []
        for entry, mapped in entries:
            writes = mapped["writes"]
            scales = mapped["scales"]
            name = str(entry.get("Name") or "")

            # Rule 3: no colour at all -> borrow from a sibling skin entry.
            has_colour = bool(
                writes.get("BaseColor")
                or writes.get("Albedo")
                or (
                    entry.get("Data")
                    and not _is_placeholder(entry["Data"].get("BaseColor"))
                    and entry["Data"].get("BaseColor")
                )
            )
            # Only genuine "no colour at all" entries borrow a skin albedo:
            # auto-named Default* submeshes, the seam-fix neck/armpit materials,
            # and entries with no BaseMaterial.  Hair keeps its own chain.
            borrowable = bool(_FALLBACK_HINT.match(name)) or not _depot_ref(
                entry.get("BaseMaterial")
            )
            if borrowable and not has_colour and (not mapped["targets"].get("BaseColor")):
                borrowed_from = None
                for candidate in pool:
                    if candidate["name"] == name:
                        continue
                    if _borrow(entry, candidate, "same-file skin fallback", mapped, fallbacks):
                        borrowed_from = candidate
                        break
                if borrowed_from is None:
                    for candidate in global_pool:
                        if _borrow(
                            entry, candidate, "global body-skin fallback", mapped, fallbacks
                        ):
                            borrowed_from = candidate
                            break
                # A placeholder entry that borrowed has no own resolved colour;
                # note it as a fallback but not as a remap.
                if borrowed_from is not None:
                    for field in mapped["writes"]:
                        counts = result["field_counts"]
                        counts[field] = counts.get(field, 0) + 1
                    for scale_key in list(mapped["scales"]):
                        result["scales_fixed_unresolved"] += 1
                    changed = True
                    continue

            # Rule 1 bookkeeping: scales fixed on entries that kept a placeholder.
            if not writes:
                for scale_key in scales:
                    result["scales_fixed_unresolved"] += 1

            if not writes and not scales and not mapped.get("meta_changed"):
                result["skipped"] += 1
                continue
            entry["Data"].update(scales)
            entry["Data"].update(writes)
            result["scales_fixed"] += len(scales)
            changed = True
            if writes:
                result["remapped"] += 1
                for field in writes:
                    counts = result["field_counts"]
                    counts[field] = counts.get(field, 0) + 1

        # Mod garments: slots still holding ``uvchecker`` because the mod ships
        # its textures without a cached ``.mi`` chain.
        try:
            if _resolve_mod_garments(path, entries, resolver, tints, result, warnings):
                changed = True
        except Exception as exc:                           # never crash the pipeline
            warnings.append(f"{path.name}: garment resolution: {exc}")

        if not changed:
            continue
        backup = Path(str(path)[: -len(".json")] + _BACKUP_SUFFIX)
        try:
            if not backup.is_file():
                backup.write_bytes(path.read_bytes())
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            warnings.append(f"{path.name}: write failed: {exc}")

    # Hardware must stay neutral: a metal-ish name keeps a neutral metal tint
    # instead of the garment colour its (now flattened) layer decoded to.
    metal_tints = 0
    for name in list(tints):
        if _METAL_TINT_NAME.search(name) and list(tints[name]) != list(_METAL_TINT):
            tints[name] = list(_METAL_TINT)
            metal_tints += 1

    try:
        with open(root / _TINT_SIDECAR, "w", encoding="utf-8") as handle:
            json.dump(tints, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        warnings.append(f"{_TINT_SIDECAR}: write failed: {exc}")

    note(
        "info",
        f"Material remap: {result['remapped']} material(s) re-pointed "
        f"({result['skipped']} left as placeholders), "
        f"{len(result['fallbacks'])} fallback(s), {result['scales_fixed']} colour "
        f"scale(s) normalised (+{result['scales_fixed_unresolved']} on unresolved) "
        f"in {result['files']} json(s).",
    )
    note(
        "info",
        f"Multilayer flatten: {result['flattened']} material(s) on "
        f"{result['mlsetups_found']} real setup(s), {result['tinted']} tint(s) "
        f"({len(result['layers_used'])} distinct layer template(s)); "
        f"arm repoint: {len(result['armed'])} material(s); "
        f"eye/head/plastic gate: {len(result['gated_restored'])} entry(ies) restored.",
    )
    if result["garments"]:
        note(
            "info",
            f"Mod garments: {len(result['garments'])} material(s) resolved from "
            "the NPV's own material tree: "
            + "; ".join(
                f"{item['entry']} <- {item['image'].split('/')[-1]} ({item['how']})"
                for item in result["garments"]
            ),
        )
    if result["adopted"]:
        note(
            "info",
            f"Empty material lists: {len(result['adopted'])} json(s) adopted a sibling "
            "*material* file"
            + (f"; {metal_tints} metal tint(s) normalised" if metal_tints else ""),
        )
    return result
