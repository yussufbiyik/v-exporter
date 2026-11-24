"""Headless Blender exporter used by V-Exporter.

Invoked by the GUI as::

    blender.exe --background --python-exit-code 1 ^
        --python blender_export.py -- ^
        --raw "<uncooked raw tree>" --ent "<entity>.ent.json" ^
        --look "<LookName|ALL>" --out "<output>\\<NPV>" --npv "<NPV name>"

What it does
------------
1. Enables the Cyberpunk Blender IO Suite (``io_scene_cp77_gltf`` or its
   ``i_scene_cp77_gltf`` spelling, including extension-style module names).
2. Imports the entity for ONE appearance through the add-on's internal API
   (``import_entity(EntityImportRequest(...))``) - the ``bpy.ops`` operator
   cannot select a specific appearance from a script (see research notes §3).
3. Exports GLB, FBX and OBJ for that look.

If the material build fails headless, the entity is re-imported with
``with_materials=False`` and geometry is exported anyway (clearly reported via
``"materials_fallback": true`` in the result block).

The last line printed on success is::

    ##VEXPORT_RESULT## {json}

The GUI parses that line; everything else is human-readable log output.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Optional, Sequence

try:  # available whenever this file runs inside Blender
    import bpy  # type: ignore
    import addon_utils  # type: ignore
    from mathutils import Vector  # type: ignore
except ImportError:  # pragma: no cover - only for py_compile / non-Blender runs
    bpy = None  # type: ignore
    addon_utils = None  # type: ignore
    Vector = None  # type: ignore

SENTINEL = "##VEXPORT_RESULT##"
ADDON_MODULES = ("io_scene_cp77_gltf", "i_scene_cp77_gltf")

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def log(message: object) -> None:
    print(f"[VEXPORT] {message}", flush=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def safe_filename(name: str, fallback: str = "look", max_length: int = 120) -> str:
    """Mirror of pipeline.safe_filename(); keep both copies in sync."""
    text = _INVALID_FILENAME.sub("_", str(name))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = fallback
    if text.upper() in _RESERVED_NAMES:
        text = "_" + text
    if len(text) > max_length:
        text = text[: max_length].rstrip(". ")
    return text or fallback


def emit_result(payload: dict) -> None:
    print(SENTINEL + " " + json.dumps(payload), flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="blender_export.py")
    parser.add_argument("--raw", required=True, help="root of the uncooked raw tree")
    parser.add_argument("--ent", required=True, help="entity .ent.json to import")
    parser.add_argument("--look", required=True, help="appearance/look name, or ALL")
    parser.add_argument("--out", required=True, help="output folder for this NPV")
    parser.add_argument("--npv", default="", help="NPV name (reporting only)")
    parser.add_argument("--formats", default="glb,fbx,obj", help="comma separated formats")
    parser.add_argument(
        "--no-proofs", action="store_true",
        help="skip the 1024x1024 proof render for each look",
    )
    return parser.parse_args(argv)


def looks_in_entity(path: Path) -> list[str]:
    """Small tolerant re-scan used only for actionable error messages."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    found: list[str] = []
    key_re = re.compile(r"appearances?(?:names|list)?$", re.IGNORECASE)

    def walk(node: object, depth: int) -> None:
        if depth > 24:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list) and key_re.search(str(key)):
                    for item in value:
                        if isinstance(item, dict):
                            for name_key in ("name", "Name", "appearanceName"):
                                candidate = item.get(name_key)
                                if isinstance(candidate, str) and candidate.strip():
                                    found.append(candidate.strip())
                                    break
                            else:
                                wrapped = item.get("$value")
                                if isinstance(wrapped, str) and wrapped.strip():
                                    found.append(wrapped.strip())
                        elif isinstance(item, str) and item.strip():
                            found.append(item.strip())
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data, 0)
    seen: set[str] = set()
    unique: list[str] = []
    for name in found:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    return unique


# ---------------------------------------------------------------------------
# add-on / scene
# ---------------------------------------------------------------------------

def find_addon_module() -> str | None:
    if addon_utils is None:
        return None
    try:
        installed = [module.__name__ for module in addon_utils.modules()]
    except Exception:  # noqa: BLE001
        installed = []
    for target in ADDON_MODULES:
        for name in installed:
            if name == target or name.endswith("." + target) or name.endswith("_" + target):
                return name
    return None


def enable_addon() -> str | None:
    name = find_addon_module()
    if name is None:
        # Direct attempts: covers installs whose module list could not be read.
        for candidate in ADDON_MODULES:
            try:
                addon_utils.enable(candidate, default_set=True)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                log(f"add-on {candidate} could not be enabled: {exc}")
            name = find_addon_module()
            if name is not None:
                break
        if name is None:
            return None
    try:
        loaded_default, loaded_state = addon_utils.check(name)  # type: ignore[union-attr]
        if not loaded_state:
            addon_utils.enable(name, default_set=True)  # type: ignore[union-attr]
        log(f"Using add-on module: {name}")
    except Exception as exc:  # noqa: BLE001
        log(f"note: enable({name}) raised {exc}; continuing (it may already be loaded)")
    return name


def load_cp77_api(module_name: str):
    """Import the entity-import internals documented in research-notes.md §3."""
    options_mod = importlib.import_module(f"{module_name}.importers.entity.options")
    execution_mod = importlib.import_module(f"{module_name}.importers.entity.execution")
    index_policy = None
    for index_path in (f"{module_name}.assetio.index", f"{module_name}.assetio.index.index"):
        try:
            index_mod = importlib.import_module(index_path)
            index_policy = getattr(index_mod, "IndexPolicy", None)
            if index_policy is not None:
                break
        except ImportError:
            continue
    entity_request = getattr(options_mod, "EntityImportRequest", None)
    import_entity = getattr(execution_mod, "import_entity", None)
    if entity_request is None or import_entity is None:
        raise ImportError(
            f"{module_name} does not expose EntityImportRequest/import_entity "
            "(unsupported add-on version?)"
        )
    return entity_request, import_entity, index_policy


def reset_scene() -> None:
    for obj in list(bpy.data.objects):  # type: ignore[union-attr]
        try:
            bpy.data.objects.remove(obj, do_unlink=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    for coll in list(bpy.data.collections):  # type: ignore[union-attr]
        if coll.users == 0:
            try:
                bpy.data.collections.remove(coll)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
    try:
        bpy.context.view_layer.update()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def set_scene_materials(on: bool) -> None:
    try:
        bpy.context.scene.cp77_panel_props.with_materials = bool(on)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def build_request(entity_request, index_policy, *, ent: Path, raw: Path, look: str, with_materials: bool):
    wanted = {
        "with_materials": with_materials,
        "filepath": str(ent),
        "appearances": (look,),
        "include_animations": False,
        "include_shadow_meshes": True,
        "depot_root": str(raw),
    }
    if index_policy is not None:
        wanted["index_policy"] = index_policy.REFRESH
    kwargs = dict(wanted)
    try:
        field_names = {field.name for field in dataclasses.fields(entity_request)}
        kwargs = {key: value for key, value in wanted.items() if key in field_names}
        dropped = sorted(set(wanted) - set(field_names))
        if dropped:
            log(f"note: this add-on version ignores request fields: {', '.join(dropped)}")
        if "report_owner" in field_names:
            kwargs["report_owner"] = None
    except Exception:  # noqa: BLE001
        pass
    return entity_request(**kwargs)


def run_import(
    import_entity,
    entity_request,
    index_policy,
    *,
    ent: Path,
    raw: Path,
    look: str,
    with_materials: bool,
):
    request = build_request(
        entity_request,
        index_policy,
        ent=ent,
        raw=raw,
        look=look,
        with_materials=with_materials,
    )
    return import_entity(request)


# ---------------------------------------------------------------------------
# components without a mesh reference (e.g. the base-body torso)
# ---------------------------------------------------------------------------

def _value(node) -> str:
    """Unwrap CName/ResourcePath JSON values to their string payload."""
    if isinstance(node, dict):
        for key in ("$value", "DepotPath", "name"):
            if key in node:
                text = _value(node[key])
                if text:
                    return text
        return ""
    if isinstance(node, str):
        return node.strip()
    return ""


def _find_appearance_list(node, key: str, depth: int = 0):
    """Locate an appearance list tolerantly (entity chunks differ by version)."""
    if depth > 12 or not isinstance(node, (dict, list)):
        return None
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            first = node[0].get("Data", node[0])
            if isinstance(first, dict) and key in first:
                return node
        for item in node:
            found = _find_appearance_list(item, key, depth + 1)
            if found is not None:
                return found
        return None
    for item_key, value in node.items():
        if isinstance(value, (dict, list)):
            found = _find_appearance_list(value, key, depth + 1)
            if found is not None:
                return found
    return None


def looks_with_missing_mesh(ent: Path, raw: Path, look: str) -> dict[str, list[tuple[str, str]]]:
    """Return ``{look: [(component name, morphResource), ...]}``.

    These are the entity components that carry no ``mesh`` depot path, so the
    add-on skips them. Two flavours exist:

    * ``entSkinnedMeshComponent``/``entGarmentSkinnedMeshComponent`` whose
      ``mesh`` is empty and whose name matches a cached ``<component>.glb``
      (the base-body torso);
    * ``entMorphTargetSkinnedMeshComponent`` carrying only a ``morphResource``
      - the morphed body *and the morphed head/face* live in
      ``baked\\<hash>.morphtarget.glb``, which is never named after its
      component, so the depot stem is returned for a second lookup.
    """
    try:
        with open(ent, "r", encoding="utf-8-sig", errors="replace") as handle:
            entity = json.load(handle)
    except (OSError, ValueError) as exc:
        log(f"note: could not read entity for fallback meshes: {exc}")
        return {}

    resources: dict[str, str] = {}
    for entry in _find_appearance_list(entity, "appearanceResource") or ():
        data = entry.get("Data", entry)
        if not isinstance(data, dict):
            continue
        name = _value(data.get("appearanceName") or data.get("name"))
        depot = _value(data.get("appearanceResource"))
        if name and depot:
            resources[name.lower()] = depot

    wanted = None if look.strip().upper() == "ALL" else {look.strip().lower()}
    result: dict[str, list[tuple[str, str]]] = {}
    for name, depot in resources.items():
        if wanted is not None and name not in wanted:
            continue
        candidate = raw / depot.replace("\\", "/")
        app_path = candidate if candidate.suffix == ".json" else candidate.with_suffix(".json")
        if not app_path.exists():
            app_path = Path(str(candidate) + ".json")
        if not app_path.exists():
            log(f"note: appearance resource for fallback not found: {app_path}")
            continue
        try:
            with open(app_path, "r", encoding="utf-8-sig", errors="replace") as handle:
                app_data = json.load(handle)
        except (OSError, ValueError) as exc:
            log(f"note: could not read {app_path.name} for fallback meshes: {exc}")
            continue
        for appearance in _find_appearance_list(app_data, "components") or ():
            data = appearance.get("Data", appearance)
            if not isinstance(data, dict):
                continue
            app_name = _value(data.get("name") or data.get("appearanceName"))
            if app_name.strip().lower() != name:
                continue
            found: list[tuple[str, str]] = []
            for component in data.get("components") or ():
                comp = component.get("Data", component)
                if not isinstance(comp, dict):
                    continue
                comp_type = str(comp.get("$type") or "")
                if "Mesh" not in comp_type:
                    continue
                comp_name = _value(comp.get("name"))
                mesh = comp.get("mesh") or {}
                if comp_name and not _value(mesh):
                    found.append((comp_name, _value(comp.get("morphResource"))))
            if found:
                result[app_name] = found
    return result


def index_cache_glbs(raw: Path) -> dict[str, Path]:
    """Map lower-case glb stem -> path for cached meshes in the raw tree.

    ``<hash>.morphtarget.glb`` is also indexed under ``<hash>`` so a
    component's ``morphResource`` depot path resolves without the suffix.
    """
    index: dict[str, Path] = {}
    skip = (".physicalscene.glb", ".w2mesh.glb", ".anims.glb")
    try:
        paths = raw.rglob("*.glb")
    except OSError:
        return index
    for path in paths:
        name = path.name.lower()
        if name.endswith(skip):
            continue
        stem = path.stem.lower()
        index.setdefault(stem, path)
        for suffix in (".morphtarget", ".mesh"):
            if stem.endswith(suffix):
                index.setdefault(stem[: -len(suffix)], path)
    return index



def primary_armature(objects):
    """Armature most mesh objects are parented to (the merged entity rig)."""
    counts: dict[object, int] = {}
    for obj in objects:
        parent = getattr(obj, "parent", None)
        if obj.type == "MESH" and getattr(parent, "type", "") == "ARMATURE":
            counts[parent] = counts.get(parent, 0) + 1
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]
    candidates = [obj for obj in objects if getattr(obj, "type", "") == "ARMATURE"]
    return candidates[0] if candidates else None


def attach_meshes_to(armature, meshes) -> None:
    """Attach imported meshes to an existing rig, keeping skinning weights."""
    inverse = armature.matrix_world.inverted()
    for obj in meshes:
        if obj.type != "MESH":
            continue
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
        for modifier in [m for m in obj.modifiers if m.type == "ARMATURE"]:
            obj.modifiers.remove(modifier)
        modifier = obj.modifiers.new("Armature", "ARMATURE")
        modifier.object = armature
        obj.parent = armature
        obj.parent_type = "OBJECT"
        obj.matrix_parent_inverse = inverse
        obj.matrix_world = world


def import_missing_components(raw: Path, ent: Path, look: str) -> dict[str, list]:
    """Import cached meshes the add-on skipped (components without a ``mesh``).

    Resolution order per component (case-insensitive, first hit wins):

    1. the component name itself - ``t0_000_pwa_base__full.glb``;
    2. its ``morphResource`` depot stem - the morphed head/face lives in
       ``baked\\<hash>.morphtarget.glb`` and is never named after its component;
    3. head/face completion: for a look with head-ish components, cached
       ``*basehead*.glb`` parts (eyes, neutral head) fill whatever is left.

    Duplicates (same bounding box within :data:`DEDUPE_TOLERANCE` *and* the
    same vertex count within :data:`DEDUPE_VERTEX_RATIO`) and helper meshes are
    dropped, so the morphed head wins over the neutral one while the eyes
    (same bbox, different vertex count) still import.

    Returns ``{look: [mesh objects]}``; empty when nothing matches.
    """
    missing = looks_with_missing_mesh(ent, raw, look)
    if not missing:
        return {}
    index = index_cache_glbs(raw)
    imported: dict[str, list] = {}
    for look_name, components in missing.items():
        main_rig = primary_armature(list(bpy.data.objects))  # type: ignore[union-attr]
        if main_rig is None:
            log("warning: fallback meshes found but no entity armature to attach them to")
            return imported
        existing = [
            _mesh_signature(obj) for obj in bpy.data.objects  # type: ignore[union-attr]
            if getattr(obj, "type", "") == "MESH"
            and not str(getattr(obj, "name", "")).startswith(DEDUPE_SKIP_PREFIXES)
        ]
        queue: list[tuple[Path, str]] = []
        unresolved: list[str] = []
        for component_name, morph_ref in components:
            glb = None
            used = ""
            for stem in _glb_stem_candidates(component_name, morph_ref):
                glb = index.get(stem)
                if glb is not None:
                    used = stem
                    break
            if glb is None:
                unresolved.append(component_name)
                continue
            if used != component_name.strip().lower():
                log(f"Fallback: component {component_name!r} resolved by resource name {used!r}")
            queue.append((glb, component_name))
        # --- head/face completion (eyes etc. are referenced by no component) --
        headish = any(
            hint in component_name.lower()
            for component_name, _ in components
            for hint in HEAD_COMPONENT_HINTS
        )
        if headish:
            family: dict[Path, str] = {}
            for stem, path in index.items():
                if stem.endswith(HEAD_FAMILY_SUFFIX):
                    family.setdefault(path, stem)
            for path, stem in family.items():
                log(f"Fallback: head/face sibling {stem!r} queued for {look_name!r}")
                queue.append((path, "head/face sibling"))
        for component_name in unresolved:
            log(f"note: no cached glb for component {component_name!r}")
        seen: set[str] = set()
        for glb, reason in queue:
            key = str(glb).lower()
            if key in seen:
                continue
            seen.add(key)
            meshes = _import_cached_mesh(glb, main_rig, existing)
            if meshes:
                imported.setdefault(look_name, []).extend(meshes)
                log(f"Fallback: {len(meshes)} mesh object(s) from {glb.name} for {reason!r}")
    return imported


HEAD_COMPONENT_HINTS = ("head", "face", "eye", "brow", "lash", "teeth", "mouth", "jaw")
HEAD_FAMILY_SUFFIX = "basehead"
DEDUPE_TOLERANCE = 0.02  # metres; two copies of one part differ by ~2 mm
DEDUPE_VERTEX_RATIO = 0.05  # two copies of one part share a vertex count
DOUBLED_SUFFIX = "_doubled"  # asset marker: second, coarser shell of one part


def _strip_asset_suffix(text: str) -> str:
    for suffix in (".glb", ".morphtarget", ".mesh"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _glb_stem_candidates(*names: str) -> list[str]:
    """Ordered, de-duplicated lower-case stems to look up in the cache index.

    A component name, its ``mesh`` path and its ``morphResource`` path differ
    only in separators/suffixes in practice, so exact, suffix-stripped and
    single-underscore spellings are enough. ``"0"`` is the "no resource" CName.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for value in names:
        text = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
        stripped = _strip_asset_suffix(text)
        for candidate in (text, stripped, text.replace("__", "_"), stripped.replace("__", "_")):
            if not candidate or candidate == "0" or candidate in seen:
                continue
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _mesh_extent(obj):
    """World-space ``(min, max)`` corners of an object's bounding box."""
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    low = [min(corner[i] for corner in corners) for i in range(3)]
    high = [max(corner[i] for corner in corners) for i in range(3)]
    return low, high


def _extent_center(extent):
    return Vector([(extent[0][i] + extent[1][i]) * 0.5 for i in range(3)])


def _extent_matches(first, second, tolerance: float = DEDUPE_TOLERANCE) -> bool:
    return (
        all(abs(first[0][i] - second[0][i]) <= tolerance for i in range(3))
        and all(abs(first[1][i] - second[1][i]) <= tolerance for i in range(3))
    )


def _mesh_signature(obj):
    """``(world extent, vertex count)`` fingerprint used for duplicate tests."""
    return _mesh_extent(obj), len(getattr(obj.data, "vertices", ()) or ())


def _signature_is_duplicate(signature, known, name: str = "") -> bool:
    """Same bbox *and* same vertex count means a real copy of one part.

    Extent alone is not enough: the eyes and the head share a bounding box
    within tolerance, so the old bbox-only test silently dropped the eyes.
    The resolved eye glb nevertheless ships a second, coarser shell of the lid
    under a ``*_doubled`` name; that one is a duplicate of the denser sibling
    at the same extent and is dropped by name.
    """
    extent, count = signature
    known_extent, known_count = known
    if not _extent_matches(extent, known_extent):
        return False
    if name.lower().endswith(DOUBLED_SUFFIX) and count <= known_count:
        return True
    return abs(count - known_count) <= max(1, known_count * DEDUPE_VERTEX_RATIO)


def _nearest_bone(armature, point):
    """Deform bone whose rest segment is closest to a world-space point."""
    local = armature.matrix_world.inverted() @ Vector(point)
    best = None
    best_distance = None
    for bone in armature.data.bones:
        if not bone.use_deform:
            continue
        head = bone.head_local
        axis = bone.tail_local - head
        length_squared = axis.length_squared
        factor = 0.0 if not length_squared else max(
            0.0, min(1.0, (local - head).dot(axis) / length_squared)
        )
        distance = (local - (head + axis * factor)).length
        if best_distance is None or distance < best_distance:
            best, best_distance = bone, distance
    if best is None:
        best = next((bone for bone in armature.data.bones if bone.use_deform), None)
    return best


EYE_BONE_HINTS = ("eye",)


def _rigid_bind_bone(armature, point):
    """Bone a whole fallback mesh is rigidly attached to.

    The nearest bone can be an eyeball joint (``RightEye``); anchoring a full
    eye/lid/lash mesh to one eyeball tears the other side away as soon as the
    eyes look around, so the walk climbs to the parent (``Head``) instead.
    """
    bone = _nearest_bone(armature, point)
    while bone is not None and any(hint in bone.name.lower() for hint in EYE_BONE_HINTS):
        bone = bone.parent
    return bone


def _ensure_skin_weights(obj, armature) -> None:
    """Guarantee every vertex is driven by a bone that exists on the rig.

    A ``.morphtarget.glb`` has no skin at all and a foreign glTF skin may name
    bones the merged rig does not have; either way the exported skin would
    contain zero-weight vertices (which viewers collapse to the origin). Those
    vertices are rigidly bound to the nearest rig bone instead (walking up from
    eyeball joints, so eyes and lids follow ``Head``).
    """
    if not getattr(obj.data, "vertices", None):
        return
    deform = {bone.name for bone in armature.data.bones if bone.use_deform}
    by_index = {group.index: group.name for group in obj.vertex_groups}
    valid = {index for index, name in by_index.items() if name in deform}
    extent = _mesh_extent(obj)
    if not valid:
        bone = _rigid_bind_bone(armature, _extent_center(extent))
        if bone is not None:
            group = obj.vertex_groups.get(bone.name) or obj.vertex_groups.new(name=bone.name)
            group.add(list(range(len(obj.data.vertices))), 1.0, "REPLACE")
            log(f"Fallback: {obj.name} had no skin weights - rigidly bound to {bone.name!r}")
        return
    orphaned = [
        vertex.index for vertex in obj.data.vertices
        if not any(group.group in valid and group.weight > 0.0 for group in vertex.groups)
    ]
    if not orphaned:
        return
    bone = _rigid_bind_bone(armature, _extent_center(extent))
    if bone is None:
        return
    group = obj.vertex_groups.get(bone.name) or obj.vertex_groups.new(name=bone.name)
    group.add(orphaned, 1.0, "REPLACE")
    log(f"Fallback: {len(orphaned)} unweighted vertex/vertices of {obj.name} bound to {bone.name!r}")


def _import_cached_mesh(glb: Path, main_rig, existing_extents: list) -> list:
    """Import one cached glb, drop helpers/duplicates, bind it and attach it."""
    before = set(bpy.data.objects)  # type: ignore[union-attr]
    log(f"Fallback: importing {glb.name} for component fallback")
    try:
        bpy.ops.import_scene.gltf(filepath=str(glb))  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        log(f"warning: fallback import failed for {glb.name}: {exc}")
        return []
    created = [obj for obj in bpy.data.objects if obj not in before]  # type: ignore[union-attr]
    # Partition FIRST: removing a datablock invalidates the Object reference
    # (StructRNA removed), so never re-read .type/.name from a list that may
    # already contain deleted objects.
    snapshot = [
        (obj, str(getattr(obj, "type", "")), str(getattr(obj, "name", "")))
        for obj in created
    ]
    # Only real meshes are body parts; the glTF importer also creates helper
    # meshes (the 42-poly Icosphere bone custom shape) which must not be kept.
    meshes = [
        obj for obj, kind, name in snapshot
        if kind == "MESH"
        and not name.startswith(DEDUPE_SKIP_PREFIXES)
        and len(getattr(obj.data, "polygons", ()) or ())
    ]
    junk = [
        obj for obj, kind, name in snapshot
        if obj not in meshes and (kind == "MESH" or kind in {"ARMATURE", "EMPTY"})
    ]
    unique = []
    for obj in meshes:
        signature = _mesh_signature(obj)
        if any(
            _signature_is_duplicate(signature, known, str(getattr(obj, "name", "")))
            for known in existing_extents
        ):
            log(f"Fallback: {obj.name} from {glb.name} duplicates an existing part - skipped")
            continue
        unique.append(obj)
        existing_extents.append(signature)
    if not unique:
        for obj in meshes + junk:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        return []
    for obj in unique:
        shape_keys = getattr(getattr(obj.data, "shape_keys", None), "key_blocks", None)
        if shape_keys and ".morphtarget" in glb.name.lower():
            dropped = len(shape_keys) - 1
            obj.shape_key_clear()
            log(
                f"Fallback: {obj.name} imported as base geometry; dropped {dropped} "
                "zero-weight morph target(s) to keep the export size sane"
            )
        _ensure_skin_weights(obj, main_rig)
    attach_meshes_to(main_rig, unique)
    for obj in meshes + junk:
        if obj in unique:
            continue
        try:
            bpy.data.objects.remove(obj, do_unlink=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    log(f"Fallback: attached {len(unique)} mesh object(s) from {glb.name}")
    return unique


def collection_objects(collection) -> list:
    objects = []
    try:
        for obj in collection.all_objects:
            objects.append(obj)
    except Exception:  # noqa: BLE001
        try:
            for child in collection.children:
                objects.extend(collection_objects(child))
            for obj in collection.objects:
                objects.append(obj)
        except Exception:  # noqa: BLE001
            pass
    return objects


def scene_collections() -> list:
    try:
        return list(bpy.context.scene.collection.children_recursive)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return []


def ensure_linked(obj) -> None:
    """Objects in un-linked collections cannot be selected/exported."""
    linked = scene_collections()
    for coll in getattr(obj, "users_collection", ()) or ():
        if any(coll is existing for existing in linked):
            continue
        if not coll.users_collection:  # orphaned collection
            try:
                bpy.context.scene.collection.children.link(coll)  # type: ignore[union-attr]
                log(f"note: linked orphan collection {coll.name} into the scene")
            except Exception as exc:  # noqa: BLE001
                log(f"warning: could not link collection {coll.name}: {exc}")


DEDUPE_SKIP_PREFIXES = ("BoneCustomShape", "Icosphere")


def view_layer_object_set() -> set:
    """Objects in the current view layer (empty set when unavailable)."""
    try:
        return set(bpy.context.view_layer.objects)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return set()


def dedupe_to_primary_rig(objects) -> list:
    """Keep only the merged entity rig and the meshes living under it.

    The add-on imports every asset twice: once under its own ``Armature__*``
    rig and once under the merged entity rig (``acm_<npv>_rig``). Exporting both
    trees duplicates geometry and is a classic FBX skin-corruption source
    ("exploded shards"), so the kept set is resolved by walking the parent
    chain down from the primary rig:

    * only objects reachable from the rig survive - per-asset ``Armature__*``
      hierarchies (and everything below them) are dropped as a block;
    * objects missing from the current view layer are skipped (the glTF
      importer's ``BoneCustomShape`` helper never sits in the body);
    * helper objects are skipped by name (``BoneCustomShape*``/``Icosphere*``);
    * a mesh whose ``obj.data`` is already used by a kept mesh is a duplicate.
    """
    objects = list(objects)
    rig = primary_armature(objects)
    if rig is None:
        return objects
    visible = view_layer_object_set()
    children: dict[object, list] = {}
    for obj in objects:
        parent = getattr(obj, "parent", None)
        if parent is not None:
            children.setdefault(parent, []).append(obj)

    keep = [rig]
    visited = {rig}  # visited guard: also blocks re-walking skipped subtrees
    used_data: set = set()
    mesh_count = 0
    queue = [rig]
    while queue:
        parent = queue.pop()
        for child in children.get(parent, ()):
            if child in visited:
                continue
            visited.add(child)  # do not re-walk a skipped subtree either
            name = str(getattr(child, "name", ""))
            if name.startswith(DEDUPE_SKIP_PREFIXES):
                continue
            if visible and child not in visible:
                continue
            kind = str(getattr(child, "type", ""))
            if kind == "ARMATURE":
                # A second rig below the merged one is one of the duplicates.
                continue
            if kind == "MESH":
                data = getattr(child, "data", None)
                if data is not None:
                    if data in used_data:
                        continue
                    used_data.add(data)
                mesh_count += 1
            keep.append(child)
            queue.append(child)

    if mesh_count == 0:
        # Never hand the exporters an empty selection because of a filter.
        log("warning: no mesh found under the primary rig; using the objects unfiltered")
        return objects
    if len(keep) == len(objects):
        return objects
    log(
        f"note: deduped rigs: keeping {getattr(rig, 'name', '?')!r} + {mesh_count} mesh(es); "
        f"{len(objects) - len(keep)} duplicate/foreign object(s) skipped"
    )
    return keep


STRAY_HELPER_MAX_VERTS = 64
STRAY_HELPER_MAX_LOCATION = 0.05  # metres from the origin


def is_stray_helper(obj) -> bool:
    """Unmaterialed, tiny, origin-centred mesh (bone custom shape / gizmo).

    The glTF importer can leave a 42-vert ``Icosphere`` (the bone custom shape,
    radius ~1 m, no material, object origin at 0,0,0) parented into the body
    tree; it slipped past the name filter when the importer renamed it.
    """
    if str(getattr(obj, "type", "")) != "MESH":
        return False
    data = getattr(obj, "data", None)
    if data is None:
        return False
    if len(getattr(data, "vertices", ()) or ()) >= STRAY_HELPER_MAX_VERTS:
        return False
    if any(slot.material is not None for slot in getattr(obj, "material_slots", ())):
        return False
    try:
        return obj.matrix_world.translation.length <= STRAY_HELPER_MAX_LOCATION
    except Exception:  # noqa: BLE001
        return False


def select_objects(objects) -> list:
    """Select MESH/ARMATURE objects for the exporters; returns the selection."""
    for obj in bpy.data.objects:  # type: ignore[union-attr]
        try:
            obj.select_set(False)
        except Exception:  # noqa: BLE001
            pass
    chosen = []
    for obj in objects:
        if getattr(obj, "type", "") not in {"MESH", "ARMATURE"}:
            continue
        if is_stray_helper(obj):
            log(f"note: {getattr(obj, 'name', '?')} is an unmaterialed "
                f"{len(getattr(getattr(obj, 'data', None), 'vertices', ()) or ())}-vert "
                "helper at the origin; excluded from the export")
            continue
        ensure_linked(obj)
        try:
            obj.hide_set(False)
        except Exception:  # noqa: BLE001
            pass
        obj.hide_viewport = False
        obj.hide_render = False
        try:
            obj.select_set(True)
            chosen.append(obj)
        except Exception as exc:  # noqa: BLE001
            log(f"warning: could not select {getattr(obj, 'name', '?')}: {exc}")
    if chosen:
        try:
            active = next((obj for obj in chosen if obj.type == "MESH"), chosen[0])
            bpy.context.view_layer.objects.active = active  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    return chosen


# ---------------------------------------------------------------------------
# exporters
# ---------------------------------------------------------------------------

def _filtered_kwargs(operator, kwargs: dict) -> dict:
    try:
        properties = operator.get_rna_type().properties
        allowed = set(properties.keys())
    except Exception:  # noqa: BLE001
        return dict(kwargs)
    dropped = sorted(set(kwargs) - allowed)
    if dropped:
        log(f"note: this Blender build ignores exporter options: {', '.join(dropped)}")
    return {key: value for key, value in kwargs.items() if key in allowed}


def _run_export(operator, kwargs: dict, path: Path) -> tuple[bool, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        operator(**_filtered_kwargs(operator, kwargs))
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc().strip().splitlines()[-1]
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        return False, f"{path.suffix.lstrip('.').upper()} export failed: {detail}"
    try:
        ok = path.exists() and path.stat().st_size > 0
    except OSError:
        ok = False
    if not ok:
        return False, f"{path.suffix.lstrip('.').upper()} export produced no file"
    return True, ""


def export_glb(path: Path, objects: Sequence = (), baked: Optional[dict] = None) -> tuple[bool, str, dict]:
    kwargs = {
        "filepath": str(path),
        "export_format": "GLB",
        "use_selection": True,
        "export_materials": "EXPORT",
        "export_apply": False,     # keeps shape keys
        "export_animations": False,
        "export_skins": True,
        "export_morph": True,
        "export_yup": True,
    }
    # The add-on's Base Color graph cannot be represented in glTF (its ATTRIBUTE
    # node is dropped, leaving the placeholder mask), so the baked PNG is what
    # the GLB must point at.
    assignments, clones, renamed, stats = prepare_simple_materials(objects, "GLB", baked)
    if stats["materials"]:
        log(f"GLB textures: {stats['textured']}/{stats['materials']} material(s) textured "
            f"({stats['baked']} baked), {len(stats['texture_names'])} image(s) referenced")
    try:
        ok, error = _run_export(bpy.ops.export_scene.gltf, kwargs, path)  # type: ignore[union-attr]
    finally:
        restore_simple_materials(assignments, clones, renamed)
    return ok, error, stats


def _pick_enum(operator, prop_name: str, candidates: Sequence[str]) -> Optional[str]:
    """Return the first supported enum identifier, or None.

    Needed because the OBJ exporter's ``export_eval_mode`` uses ``DAG_EVAL_*``
    identifiers in current Blender releases; research notes listed "RENDER".
    """
    try:
        prop = operator.get_rna_type().properties[prop_name]
        identifiers = {item.identifier for item in prop.enum_items}
    except Exception:  # noqa: BLE001
        return None
    for candidate in candidates:
        if candidate in identifiers:
            return candidate
    return None


def export_fbx(
    path: Path, objects: Sequence = (), baked: Optional[dict] = None
) -> tuple[bool, str, dict]:
    kwargs = {
        "filepath": str(path),
        "use_selection": True,
        "object_types": {"ARMATURE", "MESH"},
        "add_leaf_bones": False,
        # Only deform bones: stray control/helper bones without skin weights
        # make some FBX readers scatter the skinned meshes ("exploded shards").
        "use_armature_deform_only": True,
        # FBX has no "export_apply"; these two are the shape-key-safe equivalents.
        "use_mesh_modifiers": True,
        "use_mesh_modifiers_render": True,
        "use_triangles": False,      # triangulating drops shape keys
        "use_subsurf": False,
        "bake_anim": False,
        "use_custom_props": False,
        "global_scale": 1.0,
        "apply_scale_options": "FBX_SCALE_UNITS",
        "embed_textures": True,
        # Blender only embeds media when textures are copied with the file;
        # with AUTO the images were merely referenced, never written.
        "path_mode": "COPY",
    }
    # The add-on's node-group shaders have no chain the FBX writer understands, so
    # it used to embed no images at all; the simple stand-ins give it map_Kd-style
    # diffuse links. Skinning/armature data is untouched (only slots are swapped).
    assignments, clones, renamed, stats = prepare_simple_materials(objects, "FBX", baked)
    if stats["materials"]:
        log(f"FBX textures: {stats['textured']}/{stats['materials']} material(s) textured "
            f"({stats['baked']} baked), {len(stats['texture_names'])} image(s) referenced")
    try:
        ok, error = _run_export(bpy.ops.export_scene.fbx, kwargs, path)  # type: ignore[union-attr]
    finally:
        restore_simple_materials(assignments, clones, renamed)
    return ok, error, stats


# ---------------------------------------------------------------------------
# Simple export materials (all formats)
# ---------------------------------------------------------------------------
#
# The add-on builds node-group shaders; glTF drops their ATTRIBUTE node and
# Blender's FBX/MTL writers only understand a plain ``Image Texture ->
# Principled Base Color`` chain, so a stock export writes a placeholder mask or
# no ``map_Kd`` at all. Before each export the material slots are therefore
# swapped for throwaway clones whose Base Color is the baked image from
# :func:`bake_look_materials` (fallback: the albedo image traced upstream from
# the real Base Color socket), and the originals are restored right after.

SIMPLE_MATERIAL_STASH_SUFFIX = "__cp77"
SIMPLE_TRACE_DEPTH = 160

# Channel/helper textures that are never a base colour, plus the engine's
# placeholder swatches (``grey``/``white``/``ml_default_d`` …) which would only
# paint the model a flat stand-in colour.
SIMPLE_ALBEDO_SKIP_TOKENS = (
    "normal", "nrm", "rough", "gloss", "microblend", "detail", "mask", "height",
    "bump", "displace", "opacity", "alpha", "bloodflow", "tint", "specular",
    " uvchecker", "ml_default",
)
SIMPLE_ALBEDO_PLACEHOLDER_STEMS = {
    "grey", "white", "black", "default", "normal", "small_flat_normal",
    "small_white", "uvchecker",
}
SIMPLE_ALBEDO_PLACEHOLDER_DIRS = ("/engine/textures/", "/materials/default/", "/microblends/")
SIMPLE_ALBEDO_CHANNEL_SUFFIX = re.compile(r"[_\-](?:n|r|rm|m|nm|h|e|ao|msk|mask)\d*$", re.IGNORECASE)
SIMPLE_ALBEDO_DIFFUSE_SUFFIX = re.compile(r"(?:^|[_\-])d\d*$", re.IGNORECASE)


def _simple_output_principled(material):
    """Principled BSDF driving the material output (fallback: any Principled)."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    queue = []
    for node in tree.nodes:
        if node.bl_idname != "ShaderNodeOutputMaterial":
            continue
        surface = node.inputs.get("Surface")
        for link in getattr(surface, "links", ()) if surface is not None else ():
            queue.append(link.from_node)
    seen: set = set()
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
        for socket in node.inputs:
            for link in getattr(socket, "links", ()):
                queue.append(link.from_node)
    return next((n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)


def _simple_group_output_socket(tree, source_socket):
    """Group-output socket matching the outer group node's output socket."""
    identifier = str(getattr(source_socket, "identifier", ""))
    name = str(getattr(source_socket, "name", ""))
    for node in tree.nodes:
        if node.bl_idname != "NodeGroupOutput":
            continue
        for socket in node.inputs:
            if identifier and str(getattr(socket, "identifier", "")) == identifier:
                return socket
            if name and socket.name == name:
                return socket
    return None


def _simple_group_input_socket(group_node, source_socket):
    """Outer group-node input matching a ``NodeGroupInput`` output socket."""
    identifier = str(getattr(source_socket, "identifier", ""))
    name = str(getattr(source_socket, "name", ""))
    for socket in group_node.inputs:
        if identifier and str(getattr(socket, "identifier", "")) == identifier:
            return socket
        if name and socket.name == name:
            return socket
    return None


def _simple_trace_images(tree, socket, depth, seen, found, trail, group_stack=()):
    """Collect ``TEX_IMAGE`` nodes upstream of ``socket`` (node groups followed)."""
    if tree is None or socket is None or depth > SIMPLE_TRACE_DEPTH:
        return
    for link in list(getattr(socket, "links", ())):
        node = link.from_node
        source = link.from_socket
        identifier = str(getattr(source, "identifier", "")) or str(source.name)
        # The guard is per group *instance*: one shared node group (the CP77
        # "Layer Blend" tree) backs all 20 layer slots, and those are different
        # paths through the graph, not a loop.
        instance = "/".join(outer_node.name for _, outer_node in group_stack)
        key = (instance, tree.name, node.name, identifier)
        if key in seen:  # cycle guard (material graphs can loop back)
            continue
        seen.add(key)
        if node.bl_idname == "ShaderNodeTexImage":
            if getattr(node, "image", None) is not None:
                found.append((node, node.image, trail + (node.name,)))
            continue
        if node.bl_idname == "NodeGroupInput" and group_stack:
            # A group output feeding this socket comes from a group *input*:
            # continue on the outer group node's matching input socket.
            outer_tree, group_node = group_stack[-1]
            outer_socket = _simple_group_input_socket(group_node, source)
            if outer_socket is not None:
                _simple_trace_images(
                    outer_tree, outer_socket, depth + 1, seen, found,
                    trail + (group_node.name,), group_stack[:-1],
                )
            continue
        group_tree = getattr(node, "node_tree", None)
        group_socket = _simple_group_output_socket(group_tree, source) if group_tree else None
        if group_socket is not None:
            # Inside a group the outer socket maps to the group-output socket.
            _simple_trace_images(
                group_tree, group_socket, depth + 1, seen, found,
                trail + (node.name,), group_stack + ((tree, node),),
            )
            continue
        for input_socket in node.inputs:
            if getattr(input_socket, "is_linked", False):
                _simple_trace_images(
                    tree, input_socket, depth + 1, seen, found,
                    trail + (node.name,), group_stack,
                )


def _simple_image_file(image) -> Optional[Path]:
    """On-disk file for an image datablock (packed/empty images are unusable)."""
    try:
        filepath = bpy.path.abspath(image.filepath)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None
    if not filepath:
        return None
    path = Path(filepath)
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _simple_albedo_score(image, node, path: Path) -> int:
    """Rank a traced image as a base colour; 0 means "not usable"."""
    stem = path.stem.lower()
    text = " ".join((
        str(getattr(node, "name", "")),
        str(getattr(node, "label", "")),
        str(image.name),
        stem,
    )).lower()
    posix = path.as_posix().lower()
    if stem in SIMPLE_ALBEDO_PLACEHOLDER_STEMS or any(part in posix for part in SIMPLE_ALBEDO_PLACEHOLDER_DIRS):
        return 0
    if any(token in text for token in SIMPLE_ALBEDO_SKIP_TOKENS):
        return 0
    if SIMPLE_ALBEDO_CHANNEL_SUFFIX.search(stem):
        return 0
    score = 0
    if "albedo" in text:
        score += 120
    if any(token in text for token in ("basecolor", "base color", "base_colour", "basecolour")):
        score += 110
    if "diffuse" in text or "diff" in stem:
        score += 100
    if SIMPLE_ALBEDO_DIFFUSE_SUFFIX.search(stem):  # CP77 ``_d``/``_d01`` diffuse map
        score += 80
    if "color" in text or "colour" in text:
        score += 40
    if "base" in text:
        score += 20
    if "substance" in text:
        score += 10
    return score


def find_simple_albedo_image(material):
    """Best base-colour image reachable from the material's Base Color socket."""
    principled = _simple_output_principled(material)
    tree = getattr(material, "node_tree", None)
    if tree is None or principled is None:
        return None
    socket = principled.inputs.get("Base Color") or principled.inputs.get("BaseColor")
    if socket is None:
        return None
    found: list = []
    _simple_trace_images(tree, socket, 0, set(), found, ())
    ranked = []
    for node, image, trail in found:
        path = _simple_image_file(image)
        if path is None:
            continue
        score = _simple_albedo_score(image, node, path)
        if score <= 0:  # normal/roughness/mask/placeholder - never a base colour
            continue
        ranked.append((-score, len(trail), str(path).lower(), image))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[:3])
    return ranked[0][3]


def _simple_material(source, image):
    """Textured stand-in the glTF/FBX/MTL writers understand: image -> Base Color.

    The source datablock is renamed out of the way first so the clone inherits
    the exact original name and the exported material keeps its identity.
    """
    original_name = source.name
    stash = f"{original_name}{SIMPLE_MATERIAL_STASH_SUFFIX}"
    while bpy.data.materials.get(stash) is not None:
        stash += "_"
    source.name = stash
    clone = bpy.data.materials.new(name=original_name)
    clone.use_nodes = True
    tree = clone.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (300, 0)
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.location = (-300, 0)
    tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    origin = _simple_output_principled(source)
    if origin is not None:
        for name in ("Roughness", "Metallic", "Alpha"):
            target_socket = principled.inputs.get(name)
            source_socket = origin.inputs.get(name)
            if target_socket is None or source_socket is None or source_socket.is_linked:
                continue
            try:  # texture-driven values cannot be sampled cheaply - keep defaults
                target_socket.default_value = source_socket.default_value
            except Exception:  # noqa: BLE001
                pass
    return clone, original_name


#: Flat tints multiplied onto a baked base colour.  Extend locally when a
#: garment layer bakes grey; the shared default stays empty.
_BASE_TINTS: dict = {}

#: Per-run tints written by ``material_remap`` (``remap_tints.json``): the
#: ``colorScale`` colour of the layer a flattened garment was pointed at.
_TINT_OVERRIDES: dict = {}


def set_tint_overrides(path) -> int:
    """Load the ``remap_tints.json`` sidecar; returns the number of tints."""
    _TINT_OVERRIDES.clear()
    if path is None:
        return 0
    try:
        with open(Path(path), "r", encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return 0
    if not isinstance(doc, dict):
        return 0
    for name, rgba in doc.items():
        try:
            values = tuple(float(value) for value in rgba)[:3]
        except (TypeError, ValueError):
            continue
        if len(values) != 3:
            continue
        _TINT_OVERRIDES[str(name).strip().lower()] = (*values, 1.0)
    return len(_TINT_OVERRIDES)


def _tint_for(material_name) -> Optional[tuple]:
    """Tint for a material, matching the full base name (clone suffix stripped)."""
    low = str(material_name).lower()
    base = low.rsplit("__submesh", 1)[0]
    for key, rgba in _BASE_TINTS.items():
        if low.startswith(key) or base == key:
            return rgba
    for key, rgba in _TINT_OVERRIDES.items():
        if base == key or base.startswith(key + ".") or low.startswith(key + "__"):
            return rgba
    return None


def _apply_base_tint(material, rgba) -> bool:
    """Multiply ``rgba`` onto a simple material's base-colour texture."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return False
    principled = next(
        (node for node in tree.nodes if getattr(node, "type", "") == "BSDF_PRINCIPLED"),
        None,
    )
    if principled is None:
        return False
    socket = principled.inputs.get("Base Color")
    if socket is None:
        return False
    if not socket.is_linked:
        try:
            socket.default_value = rgba
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        upstream = socket.links[0].from_socket
        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.inputs["Factor"].default_value = 1.0
        tree.links.new(upstream, mix.inputs["A"])
        mix.inputs["B"].default_value = rgba
        tree.links.new(mix.outputs["Result"], socket)
        return True
    except Exception:  # noqa: BLE001
        return False


def _simple_used_materials(objects) -> list:
    seen: set[str] = set()
    used: list = []
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        for slot in getattr(obj, "material_slots", ()):
            material = slot.material
            if material is None or material.name in seen:
                continue
            seen.add(material.name)
            used.append(material)
    return used


# ---------------------------------------------------------------------------
# Cycles base-colour bake
# ---------------------------------------------------------------------------
#
# The add-on's Base Color is a node graph - placeholder masks multiplied by a
# colour attribute, layered detail groups, tint nodes - which no file exporter
# reproduces: glTF drops the ATTRIBUTE node and the FBX/MTL writers only follow
# one image into Base Color. The look the graph produces *inside Blender* is
# therefore baked per material into a 1024² diffuse-colour image (Cycles,
# colour pass only, no lighting) and the exporters get an
# ``Image -> Base Color`` clone pointed at that PNG.
#
# Every step is best-effort: a material without UVs, without faces or whose
# bake raises keeps the traced-albedo / colour-only path, and the total bake
# time is capped so one pathological material cannot stall the export.

BAKE_SIZE = 1024
BAKE_SAMPLES = 8
BAKE_MARGIN = 8
BAKE_TIME_BUDGET = 20 * 60  # seconds; remaining bakes are skipped after this
BAKE_DUMMY_IMAGE = "vexport_bake_dummy"


def _bake_state_uv_meshes(objects, material) -> list:
    """Mesh objects that use ``material`` and have a UV map + faces to bake."""
    meshes = []
    for obj in objects:
        if str(getattr(obj, "type", "")) != "MESH":
            continue
        data = getattr(obj, "data", None)
        if data is None or not len(getattr(data, "polygons", ()) or ()):
            continue
        if not len(getattr(data, "uv_layers", ()) or ()):
            continue
        if not any(slot.material is material for slot in getattr(obj, "material_slots", ())):
            continue
        meshes.append(obj)
    return meshes


def _bake_configure(scene) -> None:
    scene.render.engine = "CYCLES"
    try:
        scene.cycles.device = "CPU"
        scene.cycles.samples = BAKE_SAMPLES
        scene.cycles.use_denoising = False
    except Exception:  # noqa: BLE001
        pass
    settings = getattr(scene.render, "bake", None)
    if settings is not None:
        for name, value in (
            ("use_pass_direct", False),
            ("use_pass_indirect", False),
            ("use_pass_color", True),
            ("margin", BAKE_MARGIN),
            ("use_clear", True),
            ("use_selected_to_active", False),
            ("target", "IMAGE_TEXTURES"),
        ):
            try:
                setattr(settings, name, value)
            except Exception:  # noqa: BLE001
                pass
    try:
        scene.cycles.bake_type = "DIFFUSE"
    except Exception:  # noqa: BLE001
        pass


def _bake_target_node(material, image):
    """Add (and activate) the image node the bake writes into."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    node = tree.nodes.new("ShaderNodeTexImage")
    node.label = "V-Exporter bake target"
    node.image = image
    node.location = (-700, -700)
    for other in tree.nodes:
        try:
            other.select = False
        except Exception:  # noqa: BLE001
            pass
    node.select = True
    try:
        tree.nodes.active = node
    except Exception:  # noqa: BLE001
        pass
    return node


def _tint_image(image, rgba) -> bool:
    """Multiply RGB of a baked image in place (formats drop node-based tints)."""
    red, green, blue = float(rgba[0]), float(rgba[1]), float(rgba[2])
    try:
        pixels = list(image.pixels[:])
    except Exception:  # noqa: BLE001
        return False
    if not pixels:
        return False
    try:
        for index in range(0, len(pixels) - 3, 4):
            pixels[index] *= red
            pixels[index + 1] *= green
            pixels[index + 2] *= blue
        image.pixels[:] = pixels
        return True
    except Exception:  # noqa: BLE001
        try:
            image.pixels.foreach_set(pixels)
            return True
        except Exception:  # noqa: BLE001
            return False


def _image_mean_rgb(image) -> Optional[float]:
    """Sampled mean of the RGB channels (cheap, used only for the report)."""
    try:
        pixels = image.pixels[:]
    except Exception:  # noqa: BLE001
        return None
    if not pixels:
        return None
    step = 4 * max(1, (len(pixels) // 4) // 20000)
    total, count = 0.0, 0
    for index in range(0, len(pixels) - 3, step):
        total += pixels[index] + pixels[index + 1] + pixels[index + 2]
        count += 3
    return round(total / count, 4) if count else None


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _glb_vertex_index(raw: Path) -> dict:
    """stem -> set of POSITION counts for every cached source .glb."""
    index: dict = {}
    try:
        glbs = sorted(Path(raw).rglob("*.glb"))
    except OSError:
        return index
    for path in glbs:
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
            if len(blob) < 20 or blob[:4] != b"glTF":
                continue
            length = int.from_bytes(blob[12:16], "little")
            doc = json.loads(blob[20:20 + length].decode("utf-8", "replace"))
            accessors = doc.get("accessors") or []
            counts: set = set()
            for mesh in doc.get("meshes") or []:
                for primitive in mesh.get("primitives") or []:
                    position = (primitive.get("attributes") or {}).get("POSITION")
                    if isinstance(position, int) and 0 <= position < len(accessors):
                        count = accessors[position].get("count")
                        if isinstance(count, int):
                            counts.add(count)
            if counts:
                index[path.stem.lower()] = counts
        except Exception:  # noqa: BLE001
            continue
    return index


def _source_material_file(raw: Path, stem: str):
    try:
        candidates = sorted(Path(raw).rglob("*.Material.json"))
    except OSError:
        return None
    wanted = stem.lower()
    exact = next((p for p in candidates if p.name.lower() == wanted + ".material.json"), None)
    return exact or next((p for p in candidates if p.name.lower().startswith(wanted)), None)


def _expected_material_names(raw: Path, stem: str) -> list:
    """Material names the source mesh's own ``.Material.json`` expects."""
    return [name for name, _entry in _source_material_entries(raw, stem)]


def _source_material_entries(raw: Path, stem: str) -> list:
    """``[(name, entry)]`` from the mesh's own ``.Material.json`` (order kept)."""
    path = _source_material_file(raw, stem)
    if path is None:
        return []
    doc = _load_json(path)
    if not isinstance(doc, dict):
        return []
    entries: list = []
    seen: set = set()
    for entry in doc.get("Materials") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name")
        if not isinstance(name, str) or name.lower() in seen:
            continue
        seen.add(name.lower())
        entries.append((name, entry))
    return entries


def _cached_image(raw: Path, depot) -> object:
    """Load the cached PNG (or the file itself) for a depot texture path."""
    if bpy is None or not isinstance(depot, str) or not depot.strip():
        return None
    rel = depot.strip().replace("\\", "/").lstrip("/")
    base = Path(raw).joinpath(*[part for part in rel.split("/") if part])
    for candidate in (base.with_suffix(".png"), base):
        try:
            if candidate.is_file():
                image = bpy.data.images.load(str(candidate), check_existing=True)  # type: ignore[union-attr]
                return image
        except Exception:  # noqa: BLE001
            continue
    return None


def _material_from_entry(raw: Path, name: str, entry: dict):
    """Build a simple textured material from one ``.Material.json`` entry."""
    if bpy is None:
        return None
    data = entry.get("Data") if isinstance(entry.get("Data"), dict) else {}
    depot = data.get("BaseColor") or data.get("Albedo")
    image = _cached_image(Path(raw), depot)
    if image is None:
        return None
    try:
        material = bpy.data.materials.new(name=name)  # type: ignore[union-attr]
        material.use_nodes = True
        tree = material.node_tree
        for node in list(tree.nodes):
            tree.nodes.remove(node)
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
        tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
        log(f"material from source entry: {name!r} <- {image.name!r} ({depot})")
        return material
    except Exception as exc:  # noqa: BLE001
        log(f"material from source entry: could not build {name!r}: {exc}")
        return None


def assign_materials_from_source(objects, raw: Path) -> int:
    """Give each mesh the material its own ``.Material.json`` defines.

    The add-on leaves some submeshes without a material, and borrowing a head or
    body skin material puts the wrong atlas on the mesh (a toe patch on an arm).
    The source material list is the authority.  When the add-on did not build a
    listed material (its mesh reference is empty, for example a garment whose
    material lives in a separate mod archive), the material is created from the
    entry's own cached texture instead.
    """
    if bpy is None:
        return 0
    by_name: dict = {}
    for material in bpy.data.materials:  # type: ignore[union-attr]
        by_name.setdefault(str(material.name).lower(), material)
    cache: dict = {}
    vertex_index: dict = {}
    count_maps: dict = {}
    obj_counters: dict = {}
    fixed = 0
    metal_name = re.compile(r"(silver|steel|chrome|metal|zipper|gold|brass)", re.IGNORECASE)
    for obj in objects:
        if getattr(obj, "type", "") != "MESH" or getattr(obj, "data", None) is None:
            continue
        stem = str(obj.data.name).split(".")[0]
        orphan = stem.lower().startswith("submesh")
        counts = len(obj.data.vertices)
        key = (stem, counts) if orphan else stem
        matched = stem
        if key not in cache:
            entries = [] if orphan else _source_material_entries(Path(raw), stem)
            if not entries and orphan:
                # The add-on names orphan submeshes generically; match them back
                # to their source mesh through the cached glbs' vertex signature.
                if not vertex_index:
                    vertex_index = _glb_vertex_index(Path(raw))
                wanted = {counts}
                matches = [name for name, known in vertex_index.items() if wanted & known]
                preferred = [name for name in matches if not name.endswith("_material")]
                pick = preferred if preferred else matches
                if len(pick) == 1:
                    entries = _source_material_entries(Path(raw), pick[0])
                    if entries:
                        matched = pick[0]
                        log(f"material source by vertex match: {obj.name} -> {pick[0]}")
                elif pick:
                    log(f"material source by vertex match: {obj.name} ambiguous {pick[:4]}")
            cache[key] = (entries, matched)
        entries, matched = cache[key]
        if not entries:
            continue
        materials: list = []
        for name, entry in entries:
            material = by_name.get(name.lower())
            if material is None:
                material = _material_from_entry(Path(raw), name, entry)
                if material is not None:
                    by_name[name.lower()] = material
            if material is not None:
                materials.append(material)
        if not materials:
            continue
        expected_lower = {name.lower() for name, _entry in entries}
        orphan_target = None
        if orphan:
            # Map source submesh sizes to the material list: the largest shell
            # is the main fabric, the middle band the latex panel, the smallest
            # strip the metal zip.  Duplicate shells share the first material.
            mapping = count_maps.get(matched)
            if mapping is None:
                # Large shells are cloth, small parts are hardware: hand the
                # biggest submeshes the non-metal materials first, then the
                # metal ones to whatever is left.  List order alone is wrong
                # (e.g. the pants list is fabric, silver ring, fabric).
                known = sorted(vertex_index.get(matched, ()), reverse=True)
                primary = [item for item in materials if not metal_name.search(item.name)]
                hardware = [item for item in materials if metal_name.search(item.name)]
                ordered = primary + hardware
                mapping = {}
                for idx, count in enumerate(known):
                    if idx < len(ordered):
                        mapping[count] = ordered[idx]
                count_maps[matched] = mapping
            orphan_target = mapping.get(counts, materials[0])
        slots = list(getattr(obj, "material_slots", ()) or ())
        if not slots:
            try:
                target = orphan_target if orphan_target is not None else materials[0]
                obj.data.materials.append(target)
                fixed += 1
                log(f"material from source: {obj.name} <- {target.name}")
            except Exception:  # noqa: BLE001
                continue
            continue
        for index, slot in enumerate(slots):
            if orphan_target is not None:
                target = orphan_target
            else:
                counter_key = stem
                offset = obj_counters.get(counter_key, 0)
                target = materials[(offset + index) % len(materials)]
            item = getattr(slot, "material", None)
            if item is not None:
                base = str(item.name).rsplit("__submesh", 1)[0].lower()
                if base in expected_lower:
                    continue
            try:
                slot.material = target
                fixed += 1
                log(f"material from source: {obj.name} <- {target.name} "
                    f"(was {getattr(item, 'name', None)!r})")
            except Exception:  # noqa: BLE001
                continue
        if not orphan:
            obj_counters[counter_key] = obj_counters.get(counter_key, 0) + 1
    return fixed


def normalize_uv_layers(objects) -> int:
    """Bake against the UV map the exporters will use (layer 0)."""
    if bpy is None:
        return 0
    touched = 0
    for obj in objects:
        if getattr(obj, "type", "") != "MESH" or getattr(obj, "data", None) is None:
            continue
        layers = getattr(obj.data, "uv_layers", None)
        if layers is None or len(layers) == 0:
            continue
        try:
            layers.active_index = 0
            layers[0].active_render = True
            if len(layers) > 1:
                log(f"uv: {obj.name} has {len(layers)} UV maps; baking against layer 0")
            touched += 1
        except Exception:  # noqa: BLE001
            continue
    return touched


def assign_missing_materials(objects) -> int:
    """Give meshes that arrived without a real material a skin material.

    The add-on leaves submeshes that have no material entry in ``.mesh.json``
    with an empty slot, so Blender invents ``Default``/``Default.00x`` and the
    bake produces black.  Borrow the head or body skin material already in use
    by the rest of the look (the face mesh needs the head texture, everything
    else the body skin).
    """
    try:
        meshes = [obj for obj in objects if getattr(obj, "type", "") == "MESH"]
    except Exception:  # noqa: BLE001
        return 0

    head_candidate = None
    body_candidate = None
    for obj in meshes:
        for slot in getattr(obj, "material_slots", ()) or ():
            material = getattr(slot, "material", None)
            if material is None:
                continue
            name = str(getattr(material, "name", "")).lower()
            if name.startswith("default"):
                continue
            if head_candidate is None and ("head" in name or "1_ca" in name or "basehead" in name):
                head_candidate = material
            if body_candidate is None and (
                "skin" in name or "01_ca" in name or "body" in name or "arm" in name
            ):
                body_candidate = material
    fallback = body_candidate or head_candidate

    fixed = 0
    for obj in meshes:
        data = getattr(obj, "data", None)
        if data is None:
            continue
        label = str(getattr(data, "name", "") or getattr(obj, "name", "")).lower()
        target = head_candidate if ("head" in label or "basehead" in label) else fallback
        if target is None:
            continue
        slots = list(getattr(obj, "material_slots", ()) or ())
        if not slots:
            try:
                data.materials.append(target)
                fixed += 1
                log(f"assigned skin material '{target.name}' to {obj.name} (no material)")
            except Exception:  # noqa: BLE001
                continue
            continue
        for slot in slots:
            material = getattr(slot, "material", None)
            if material is not None and not str(material.name).lower().startswith("default"):
                continue
            try:
                slot.material = target
                fixed += 1
                log(f"assigned skin material '{target.name}' to {obj.name}")
            except Exception:  # noqa: BLE001
                continue
    return fixed


def clone_materials_per_object(objects) -> int:
    """Give every mesh its own material clone before baking.

    The add-on hands the same skin material to dozens of body meshes; baking
    them into one atlas makes their overlapping UV islands overwrite each
    other (smears and blotches on chest, hands, thighs).  A per-object clone
    means every mesh bakes into its own texture, so nothing overlaps.
    """
    clones = 0
    try:
        meshes = [obj for obj in objects if getattr(obj, "type", "") == "MESH"]
    except Exception:  # noqa: BLE001
        return 0
    for obj in meshes:
        for slot in list(getattr(obj, "material_slots", ()) or ()):
            material = getattr(slot, "material", None)
            if material is None:
                continue
            try:
                clone = material.copy()
                stem = safe_filename(getattr(obj, "name", "obj"), fallback="obj")
                clone.name = f"{material.name}__{stem}"[:63]
                slot.material = clone
                clones += 1
            except Exception:  # noqa: BLE001
                continue
        data = getattr(obj, "data", None)
        if data is not None:
            # keep the data material list in sync with the object slots
            try:
                for index, slot in enumerate(getattr(obj, "material_slots", ()) or ()):
                    if index < len(data.materials):
                        data.materials[index] = slot.material
            except Exception:  # noqa: BLE001
                pass
    return clones


def bake_look_materials(objects, textures_dir: Path, *, label: str = "look") -> tuple[dict, dict]:
    """Bake the base colour of every material used by ``objects``.

    Returns ``({material name: baked image}, report)``. The report lists baked,
    skipped (no UV) and failed materials so the run can be audited without the
    original node graphs.
    """
    start = time.monotonic()
    report: dict = {
        "label": label,
        "textures_dir": str(textures_dir),
        "baked": [],
        "skipped_no_uv": [],
        "failed": [],
        "budget_seconds": BAKE_TIME_BUDGET,
    }
    materials = _simple_used_materials(objects)
    if not materials:
        return {}, report
    scene = bpy.context.scene  # type: ignore[union-attr]
    engine_before = getattr(getattr(scene, "render", None), "engine", None)
    try:
        _bake_configure(scene)
    except Exception as exc:  # noqa: BLE001 - no Cycles (e.g. stripped build)
        detail = f"{type(exc).__name__}: {exc}"
        log(f"Bake: Cycles unavailable ({detail}); keeping traced albedo/colour path")
        report["failed"] = [{"material": material.name, "error": detail} for material in materials]
        return {}, report
    try:
        for stale in textures_dir.glob("*.png"):
            stale.unlink()
    except OSError as exc:  # noqa: BLE001
        log(f"Bake: could not prune {textures_dir}: {exc}")
    textures_dir.mkdir(parents=True, exist_ok=True)
    dummy = bpy.data.images.new(BAKE_DUMMY_IMAGE, 4, 4, alpha=False)  # type: ignore[union-attr]
    dummy_nodes: dict[int, object] = {}
    baked: dict = {}
    try:
        try:  # bake is an object-mode operator
            bpy.ops.object.mode_set(mode="OBJECT")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        for material in materials:
            if time.monotonic() - start > BAKE_TIME_BUDGET:
                log(f"Bake: time budget ({BAKE_TIME_BUDGET}s) reached - "
                    f"{len(materials) - len(report['baked']) - len(report['failed'])} material(s) skipped")
                break
            meshes = _bake_state_uv_meshes(objects, material)
            if not meshes:
                report["skipped_no_uv"].append(material.name)
                log(f"Bake: {material.name!r} has no UV-mapped faces; skipped")
                continue
            image = bpy.data.images.new(  # type: ignore[union-attr]
                f"bake_{safe_filename(material.name, fallback='material')}",
                BAKE_SIZE, BAKE_SIZE, alpha=False,
            )
            try:
                image.colorspace_settings.name = "sRGB"
            except Exception:  # noqa: BLE001
                pass
            target = _bake_target_node(material, image)
            # Every other material on the selected meshes needs an active image
            # node too, otherwise the bake operator aborts the whole call.
            for obj in meshes:
                for slot in getattr(obj, "material_slots", ()):
                    other = slot.material
                    if other is None or other is material or id(other) in dummy_nodes:
                        continue
                    node = _bake_target_node(other, dummy)
                    if node is not None:
                        dummy_nodes[id(other)] = node
            active_before = getattr(
                getattr(getattr(bpy.context, "view_layer", None), "objects", None), "active", None
            )
            for obj in bpy.data.objects:  # type: ignore[union-attr]
                try:
                    obj.select_set(False)
                except Exception:  # noqa: BLE001
                    pass
            for obj in meshes:
                try:
                    obj.hide_viewport = False
                    obj.select_set(True)
                except Exception:  # noqa: BLE001
                    pass
            ok = False
            error = ""
            started_bake = time.monotonic()
            try:
                bpy.context.view_layer.objects.active = meshes[0]  # type: ignore[union-attr]
                bpy.context.view_layer.update()  # type: ignore[union-attr]
                bpy.ops.object.bake(  # type: ignore[union-attr]
                    type="DIFFUSE",
                    pass_filter={"COLOR"},
                    margin=BAKE_MARGIN,
                    use_clear=True,
                    use_selected_to_active=False,
                )
                ok = True
            except Exception as exc:  # noqa: BLE001 - never fail the whole export
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if target is not None:
                    try:
                        material.node_tree.nodes.remove(target)
                    except Exception:  # noqa: BLE001
                        pass
            if ok:
                tint = _tint_for(material.name)
                if tint and _tint_image(image, tint):
                    log(f"Bake: tinted {material.name!r} {tuple(round(c, 2) for c in tint[:3])} into the texture")
                path = textures_dir / f"{safe_filename(material.name, fallback='material')}.png"
                try:
                    image.filepath_raw = str(path)
                    image.file_format = "PNG"
                    image.save()
                    if not path.is_file() or path.stat().st_size == 0:
                        raise OSError("bake wrote no pixels")
                    if (_image_mean_rgb(image) or 0.0) < 0.002:
                        path.unlink(missing_ok=True)
                        raise OSError("blank bake (no lit pixels); kept traced/colour path")
                    baked[material.name] = image
                    report["baked"].append({
                        "material": material.name,
                        "image": path.name,
                        "bytes": path.stat().st_size,
                        "seconds": round(time.monotonic() - started_bake, 2),
                        "mean_rgb": _image_mean_rgb(image),
                    })
                    log(f"Bake: {material.name!r} -> {path.name} "
                        f"({report['baked'][-1]['seconds']}s, {len(meshes)} mesh(es))")
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    error = f"could not save baked image: {exc}"
            if not ok:
                report["failed"].append({"material": material.name, "error": error})
                log(f"Bake: {material.name!r} failed ({error}); traced albedo/colour kept")
                try:
                    bpy.data.images.remove(image)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
            try:
                bpy.context.view_layer.objects.active = active_before  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
    finally:
        for node in dummy_nodes.values():
            try:
                node.id_data.nodes.remove(node)
            except Exception:  # noqa: BLE001
                pass
        try:
            bpy.data.images.remove(dummy)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        if engine_before:
            try:
                scene.render.engine = engine_before
            except Exception:  # noqa: BLE001
                pass
        report["seconds"] = round(time.monotonic() - start, 2)
    log(f"Bake: {len(report['baked'])}/{len(materials)} material(s) baked in "
        f"{report['seconds']}s ({len(report['failed'])} failed, "
        f"{len(report['skipped_no_uv'])} without UVs)")
    return baked, report


def prepare_simple_materials(
    objects, label: str = "OBJ", baked: Optional[dict] = None
) -> tuple[list, list, list, dict]:
    """Swap textured clones into the slots.

    ``baked`` maps material name -> baked base-colour image; those win over the
    traced albedo. Returns ``(assignments, clones, renamed, stats)``; the
    originals must be put back through :func:`restore_simple_materials` (call it
    in a ``finally``).
    """
    used = _simple_used_materials(objects)
    baked = baked or {}
    clones: dict[int, object] = {}
    images: set[str] = set()
    renamed: list = []
    baked_count = 0
    for material in used:
        image = baked.get(material.name)
        is_baked = image is not None
        if image is None:
            image = find_simple_albedo_image(material)
        if image is None:
            log(f"{label}: no base-colour texture traced for {material.name!r}; kept colour-only")
            continue
        clone, original_name = _simple_material(material, image)
        clones[id(material)] = clone
        renamed.append((material, original_name))
        images.add(image.name)
        if is_baked:
            baked_count += 1
        log(f"{label}: {original_name!r} -> {'baked' if is_baked else 'traced'} texture {image.name!r}")
    assignments: list = []
    data_assignments: list = []
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        for slot in getattr(obj, "material_slots", ()):
            source = slot.material
            clone = clones.get(id(source)) if source is not None else None
            if clone is None:
                continue
            assignments.append((slot, source))
            slot.material = clone
    # Evaluated meshes read the *data* material list, which is a separate copy
    # when the slots are object-linked; keep both in sync during the export so
    # no stale material ends up in the .mtl/.fbx.
    for obj in objects:
        data = getattr(obj, "data", None)
        if getattr(obj, "type", "") != "MESH" or data is None:
            continue
        for index, material in enumerate(list(getattr(data, "materials", []) or [])):
            clone = clones.get(id(material)) if material is not None else None
            if clone is None:
                continue
            data_assignments.append((data, index, material))
            data.materials[index] = clone
    stats = {
        "materials": len(used),
        "textured": len(clones),
        "baked": baked_count,
        "texture_names": sorted(images),
    }
    try:
        # The exporters read evaluated meshes; without an update they may still
        # carry the pre-swap materials.
        bpy.context.view_layer.update()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    return assignments, list(clones.values()), renamed, stats


def restore_simple_materials(assignments, clones, renamed) -> None:
    """Put the originals (and their names) back, then drop the throwaway clones."""
    for slot, source in assignments:
        try:
            slot.material = source
        except Exception:  # noqa: BLE001
            pass
    for material in clones:
        try:
            bpy.data.materials.remove(material, do_unlink=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    for material, original_name in renamed:
        try:
            material.name = original_name  # clone is gone, the name is free again
        except Exception:  # noqa: BLE001
            pass
    try:
        bpy.context.view_layer.update()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def _unique_texture_path(folder: Path, filename: str, used: set[str]) -> Path:
    """A non-colliding PNG path inside ``folder``."""
    name = safe_filename(filename, fallback="texture")
    if not Path(name).suffix:
        name += ".png"
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = folder / name
    index = 2
    while candidate.exists() or candidate.name.lower() in used:
        candidate = folder / f"{stem}_{index}{suffix}"
        index += 1
    used.add(candidate.name.lower())
    return candidate


def _localize_obj_images(materials, textures_dir: Path) -> int:
    """Copy every image used by the OBJ stand-ins into ``textures_dir``.

    Baked images already live there. A material whose bake failed keeps a traced
    albedo from the uncook cache; without this step the OBJ ``.mtl`` would point
    outside the output folder. Returns the number of images written.
    """
    textures_dir.mkdir(parents=True, exist_ok=True)
    try:
        root = textures_dir.resolve()
    except OSError:
        root = textures_dir
    copied = 0
    by_source: dict[str, Path] = {}
    used: set[str] = set()
    for material in materials:
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        for node in tree.nodes:
            if getattr(node, "bl_idname", "") != "ShaderNodeTexImage":
                continue
            image = getattr(node, "image", None)
            if image is None:
                continue
            try:
                source = Path(bpy.path.abspath(image.filepath))  # type: ignore[union-attr]
                source = source.resolve()
            except Exception:  # noqa: BLE001
                source = None
            key = str(source).lower() if source is not None and source.is_file() else f"generated:{id(image)}"
            if source is not None and source.is_file():
                try:
                    source.relative_to(root)
                    continue  # already in the textures folder
                except ValueError:
                    pass
                target = by_source.get(key)
                if target is None:
                    target = _unique_texture_path(textures_dir, source.name, used)
                    try:
                        shutil.copyfile(source, target)
                    except OSError as exc:
                        log(f"OBJ: could not copy texture {source}: {exc}")
                        continue
                    by_source[key] = target
                    copied += 1
                image.filepath = str(target)
                continue
            # Packed or generated image with no file on disk: write it out.
            target = by_source.get(key)
            if target is None:
                target = _unique_texture_path(textures_dir, f"{image.name}.png", used)
                try:
                    image.filepath_raw = str(target)
                    image.file_format = "PNG"
                    image.save()
                except Exception as exc:  # noqa: BLE001
                    log(f"OBJ: could not save texture {image.name!r}: {exc}")
                    continue
                by_source[key] = target
                copied += 1
            image.filepath = str(target)
    return copied


def export_obj(
    path: Path, objects: Sequence = (), baked: Optional[dict] = None
) -> tuple[bool, str, dict]:
    operator = bpy.ops.wm.obj_export  # type: ignore[union-attr]
    kwargs = {
        "filepath": str(path),
        "export_selected_objects": True,
        "export_materials": True,
        "export_uv": True,
        "export_normals": True,
        "export_animation": False,
        # RELATIVE keeps the baked textures in textures\<look>\ instead of copying
        # them next to the .obj/.mtl; the .mtl points at the relative sub-folder.
        "path_mode": "RELATIVE",
        "apply_modifiers": True,     # evaluated/deformed mesh
    }
    eval_mode = _pick_enum(operator, "export_eval_mode", ("DAG_EVAL_RENDER", "RENDER"))
    if eval_mode:
        kwargs["export_eval_mode"] = eval_mode
        log(f"OBJ export_eval_mode={eval_mode}")
    assignments, clones, renamed, stats = prepare_simple_materials(objects, "OBJ", baked)
    if clones:
        localized = _localize_obj_images(clones, path.parent / f"{path.stem}_textures")
        if localized:
            log(f"OBJ: stored {localized} traced texture(s) in {path.parent / (path.stem + '_textures')}")
    if stats["materials"]:
        log(f"OBJ textures: {stats['textured']}/{stats['materials']} material(s) textured "
            f"({stats['baked']} baked), {len(stats['texture_names'])} image(s) referenced")
    try:
        ok, error = _run_export(operator, kwargs, path)
    finally:
        restore_simple_materials(assignments, clones, renamed)
    return ok, error, stats


EXPORTERS = {"glb": export_glb, "fbx": export_fbx, "obj": export_obj}


PROOF_SIZE = 1024
PROOF_SAMPLES = 48


def _proof_bounds(objects):
    """World-space bounding box of the export meshes, or None."""
    if bpy is None or Vector is None:
        return None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None
    low = [float("inf"), float("inf"), float("inf")]
    high = [float("-inf"), float("-inf"), float("-inf")]
    found = False
    for obj in objects:
        if str(getattr(obj, "type", "")) != "MESH":
            continue
        try:
            evaluated = obj.evaluated_get(depsgraph)
            matrix = evaluated.matrix_world
            for corner in evaluated.bound_box:
                point = matrix @ Vector(corner)
                for axis in range(3):
                    low[axis] = min(low[axis], point[axis])
                    high[axis] = max(high[axis], point[axis])
                found = True
        except Exception:  # noqa: BLE001
            continue
    if not found:
        return None
    return Vector(low), Vector(high)


def _aim_at(obj, target) -> None:
    direction = target - obj.location
    if direction.length:
        obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _add_proof_light(name, location, target, energy, size, color):
    data = bpy.data.lights.new(name, type="AREA")  # type: ignore[union-attr]
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    data.color = color
    obj = bpy.data.objects.new(name, data)  # type: ignore[union-attr]
    bpy.context.scene.collection.objects.link(obj)  # type: ignore[union-attr]
    obj.location = location
    _aim_at(obj, target)
    return obj


def render_proof(
    objects, path: Path, *, label: str = "look"
) -> tuple[Optional[Path], Optional[str]]:
    """Render a 1024x1024 proof image of the exported look.

    Uses Cycles on the CPU so it works in ``blender --background`` without a
    display. A failure is reported and never fails the actual export.
    """
    if bpy is None or Vector is None:
        return None, "bpy/Vector unavailable"
    scene = bpy.context.scene  # type: ignore[union-attr]
    bounds = _proof_bounds(objects)
    if bounds is None:
        return None, "no mesh bounds for proof"
    low, high = bounds
    center = (low + high) * 0.5
    size = high - low
    extent = max(size.x, size.y, size.z, 0.2)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {obj for obj in objects if str(getattr(obj, "type", "")) == "MESH"}
    hidden = []
    for obj in list(scene.objects):
        if str(getattr(obj, "type", "")) == "MESH" and obj not in keep and not obj.hide_render:
            obj.hide_render = True
            hidden.append(obj)
    unhidden = []
    for obj in keep:
        if getattr(obj, "hide_render", False):
            obj.hide_render = False
            unhidden.append(obj)

    old_camera = scene.camera
    old_engine = scene.render.engine
    old_resolution = (scene.render.resolution_x, scene.render.resolution_y)
    old_percentage = scene.render.resolution_percentage
    old_filepath = scene.render.filepath
    old_format = scene.render.image_settings.file_format
    old_color_mode = scene.render.image_settings.color_mode
    old_transparent = scene.render.film_transparent
    created = []
    started = time.monotonic()
    try:
        camera_data = bpy.data.cameras.new("VExporterProofCamera")  # type: ignore[union-attr]
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = extent * 1.18
        camera_data.clip_start = 0.01
        camera_data.clip_end = max(extent * 20.0, 50.0)
        camera = bpy.data.objects.new("VExporterProofCamera", camera_data)  # type: ignore[union-attr]
        scene.collection.objects.link(camera)
        created.append(camera)
        direction = Vector((0.9, 1.45, 0.3)).normalized()
        camera.location = center + direction * max(extent * 2.8, 3.5)
        _aim_at(camera, center)
        scene.camera = camera

        world = scene.world
        if world is None:
            world = bpy.data.worlds.new("VExporterProofWorld")  # type: ignore[union-attr]
            scene.world = world
        world.use_nodes = True
        background = world.node_tree.nodes.get("Background")
        if background is not None:
            background.inputs[0].default_value = (0.035, 0.045, 0.06, 1.0)
            background.inputs[1].default_value = 0.7

        lights = (
            ("VExporterProofKey", center + Vector((extent * 1.7, extent * 2.0, extent * 1.6)),
             900.0, extent * 1.4, (1.0, 0.94, 0.86)),
            ("VExporterProofFill", center + Vector((-extent * 2.1, extent * 1.3, extent * 1.0)),
             380.0, extent * 1.8, (0.72, 0.84, 1.0)),
            ("VExporterProofRim", center + Vector((extent * 0.4, -extent * 2.2, extent * 1.8)),
             650.0, extent * 1.2, (0.8, 0.9, 1.0)),
        )
        for name, location, energy, light_size, color in lights:
            created.append(_add_proof_light(name, location, center, energy, light_size, color))

        scene.render.engine = "CYCLES"
        try:
            scene.cycles.device = "CPU"
            scene.cycles.samples = PROOF_SAMPLES
            scene.cycles.use_denoising = False
            scene.cycles.max_bounces = 4
            scene.cycles.diffuse_bounces = 2
            scene.cycles.glossy_bounces = 2
            scene.cycles.transmission_bounces = 2
        except Exception:  # noqa: BLE001
            pass
        scene.render.resolution_x = PROOF_SIZE
        scene.render.resolution_y = PROOF_SIZE
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = False
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGB"
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)  # type: ignore[union-attr]
        if not path.is_file() or path.stat().st_size == 0:
            raise OSError("render wrote no image")
        log(f"Proof: {label!r} -> {path.name} "
            f"({round(time.monotonic() - started, 1)}s, {PROOF_SIZE}x{PROOF_SIZE})")
        return path, None
    except Exception as exc:  # noqa: BLE001 - never fail the export
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        for obj in created:
            data = getattr(obj, "data", None)
            try:
                bpy.data.objects.remove(obj, do_unlink=True)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            try:
                if isinstance(data, bpy.types.Camera):  # type: ignore[union-attr]
                    bpy.data.cameras.remove(data)  # type: ignore[union-attr]
                elif isinstance(data, bpy.types.Light):  # type: ignore[union-attr]
                    bpy.data.lights.remove(data)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        for obj in hidden:
            try:
                obj.hide_render = False
            except Exception:  # noqa: BLE001
                pass
        for obj in unhidden:
            try:
                obj.hide_render = True
            except Exception:  # noqa: BLE001
                pass
        scene.camera = old_camera
        scene.render.engine = old_engine
        scene.render.resolution_x, scene.render.resolution_y = old_resolution
        scene.render.resolution_percentage = old_percentage
        scene.render.filepath = old_filepath
        scene.render.image_settings.file_format = old_format
        scene.render.image_settings.color_mode = old_color_mode
        scene.render.film_transparent = old_transparent


def export_target(
    base_name: str,
    out_dir: Path,
    formats: list[str],
    selected: Sequence = (),
    baked: Optional[dict] = None,
) -> dict:
    files: dict[str, str] = {}
    errors: list[str] = []
    textures: dict = {}
    if selected:
        meshes = len([obj for obj in selected if getattr(obj, "type", "") == "MESH"])
    else:
        meshes = len([obj for obj in bpy.data.objects if obj.type == "MESH"])  # type: ignore[union-attr]
    for fmt in formats:
        exporter = EXPORTERS.get(fmt)
        if exporter is None:
            errors.append(f"unknown format: {fmt}")
            continue
        path = out_dir / f"{base_name}.{fmt}"
        ok, error, stats = exporter(path, selected, baked)
        if ok:
            files[fmt] = str(path)
            if stats:
                textures[fmt] = stats
            log(f"{fmt.upper()} written: {path}")
        else:
            errors.append(error)
            log(f"warning: {error}")
    return {"files": files, "errors": errors, "meshes": meshes, "textures": textures}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args(sys.argv)
    raw = Path(args.raw)
    ent = Path(args.ent)
    out_dir = Path(args.out)
    requested = args.look.strip() or "ALL"
    formats = [fmt.strip().lower() for fmt in args.formats.split(",") if fmt.strip()]
    formats = [fmt for fmt in formats if fmt in EXPORTERS] or list(EXPORTERS)
    render_proofs = not args.no_proofs

    payload: dict = {
        "ok": False,
        "npv": args.npv,
        "ent": str(ent),
        "requested": requested,
        "blender": getattr(bpy.app, "version_string", "unknown") if bpy else None,
        "materials": None,
        "materials_fallback": False,
        "looks": {},
        "warnings": [],
        "error": None,
    }

    if bpy is None:
        payload["error"] = "bpy is not available; run this script through Blender."
        emit_result(payload)
        return 1
    if addon_utils is None:
        payload["error"] = "addon_utils is not available; run this script through Blender."
        emit_result(payload)
        return 1
    for required, label in ((raw, "raw folder"), (ent, "entity .ent.json")):
        if not required.exists():
            payload["error"] = f"{label} not found: {required}"
            emit_result(payload)
            return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    # Shared textures live at the NPV root; per-look textures live next to each
    # look's model files. The shared folder may stay empty until a texture is
    # actually shared by more than one look.
    (out_dir / "textures").mkdir(parents=True, exist_ok=True)
    log(f"NPV={args.npv!r} look={requested!r} blender={payload['blender']} "
        f"proofs={'on' if render_proofs else 'off'}")
    tints = set_tint_overrides(raw / "remap_tints.json")
    if tints:
        log(f"Tint overrides: {tints} material(s) from remap_tints.json")

    module_name = enable_addon()
    if module_name is None:
        payload["error"] = (
            "Cyberpunk Blender IO Suite add-on not found. Install add-on 2.5.0 "
            "(Edit > Preferences > Add-ons > Install from Disk) and enable it, then retry."
        )
        emit_result(payload)
        return 1
    try:
        entity_request, import_entity, index_policy = load_cp77_api(module_name)
    except Exception as exc:  # noqa: BLE001
        payload["error"] = (
            f"Could not load the entity import API from {module_name}: {exc}. "
            "The installed add-on version may be incompatible with Cyberpunk Blender IO Suite 2.5.0."
        )
        emit_result(payload)
        return 1

    if requested.upper() != "ALL":
        available = looks_in_entity(ent)
        if available and requested.lower() not in {name.lower() for name in available}:
            message = (
                f"look {requested!r} is not listed in the entity; available looks: "
                + ", ".join(available)
                + "; the importer may fall back to the default appearance."
            )
            log("WARNING: " + message)
            payload["warnings"].append(message)
            payload["look_missing"] = True

    # --- import (with a geometry-only retry if material building fails) -----
    result = None
    materials_ok = True
    for with_materials in (True, False):
        reset_scene()
        set_scene_materials(with_materials)
        log(f"Importing entity (materials={'on' if with_materials else 'off'}) ...")
        try:
            result = run_import(
                import_entity, entity_request, index_policy,
                ent=ent, raw=raw, look=requested, with_materials=with_materials,
            )
            materials_ok = with_materials
            break
        except Exception:  # noqa: BLE001
            trace = traceback.format_exc()
            log(f"Import failed (materials={'on' if with_materials else 'off'}):")
            for line in trace.strip().splitlines():
                log("  " + line)
            if not with_materials:
                break
            payload["materials_fallback"] = True
            log("Retrying without materials to still export geometry ...")

    if result is None:
        available = looks_in_entity(ent)
        payload["error"] = (
            "Entity import failed even without materials. "
            + (f"Looks found in the .ent.json: {', '.join(available)}. " if available else
               "No appearance names could be read from the .ent.json. ")
            + "See the per-attempt traceback in the log."
        )
        emit_result(payload)
        return 1

    if not materials_ok:
        log("WARNING: materials could not be built headless; exporting geometry only.")
        payload["materials_fallback"] = True

    for warning in list(getattr(result, "warnings", ()) or ())[:20]:
        log(f"add-on warning: {warning}")
        payload["warnings"].append(str(warning))
    failures = list(getattr(result, "failures", ()) or ())
    for failure in failures[:20]:
        log(f"add-on failure: {failure}")
    if failures:
        payload["error"] = "; ".join(str(item) for item in failures[:5])
        emit_result(payload)
        return 1

    # --- import components the add-on skipped (no mesh depot reference) -----
    fallback = import_missing_components(raw=raw, ent=ent, look=requested)
    if fallback:
        fallback_count = sum(len(items) for items in fallback.values())
        payload["fallback_meshes"] = {
            name: [getattr(obj, "name", "?") for obj in items] for name, items in fallback.items()
        }
        log(f"Fallback meshes imported for components without mesh refs: {fallback_count} object(s)")

    created = list(getattr(result, "created_items", ()) or ())
    try:
        imported_collections = [item for item in created if isinstance(item, bpy.types.Collection)]
    except Exception:  # noqa: BLE001
        imported_collections = [item for item in created if type(item).__name__ == "Collection"]

    mesh_total = len([obj for obj in bpy.data.objects if obj.type == "MESH"])  # type: ignore[union-attr]
    materials_total = len(bpy.data.materials)  # type: ignore[union-attr]
    log(f"Imported: {mesh_total} mesh object(s), {materials_total} material(s), "
        f"{len(imported_collections)} collection(s)")
    payload["materials"] = materials_total > 0 and materials_ok
    if not mesh_total:
        available = looks_in_entity(ent)
        payload["error"] = (
            f"Import produced no mesh objects for look {requested!r}. "
            + (f"Looks found in the .ent.json: {', '.join(available)}. " if available else
               "No appearance names could be read from the .ent.json. ")
            + "Check the look name / entity selection."
        )
        emit_result(payload)
        return 1

    # --- pick export targets ----------------------------------------------
    targets: list[tuple[str, list]] = []
    if requested.upper() == "ALL":
        for collection in imported_collections:
            appearance = ""
            try:
                appearance = str(
                    collection.get("appearanceName")
                    or collection.get("cp77_appearance_name")
                    or ""
                )
            except Exception:  # noqa: BLE001
                appearance = ""
            if appearance:
                targets.append((appearance, collection_objects(collection)))
        if not targets:
            if imported_collections:
                log("note: appearance names were not exposed on the imported collections; "
                    "exporting each collection as a numbered look.")
                for index, collection in enumerate(imported_collections, start=1):
                    targets.append((collection.name or f"look_{index}", collection_objects(collection)))
            else:
                log("note: importing ALL appearances produced no per-appearance collections; "
                    "exporting the whole scene as 'all_appearances'.")
                targets.append(("all_appearances", list(bpy.data.objects)))
    else:
        targets.append((requested, list(bpy.data.objects)))

    if not targets:
        payload["error"] = "Nothing to export (no appearance collections were created)."
        emit_result(payload)
        return 1

    used: set[str] = set()
    if fallback:
        for index, (name, objects) in enumerate(targets):
            extra = next(
                (items for key, items in fallback.items() if key.lower() == name.strip().lower()),
                None,
            )
            if extra:
                targets[index] = (name, list(objects) + list(extra))
    any_success = False
    for name, objects in targets:
        base = safe_filename(name)
        suffix = 2
        while base.lower() in used:
            base = f"{safe_filename(name)}_{suffix}"
            suffix += 1
        used.add(base.lower())
        look_objects = dedupe_to_primary_rig(objects)
        sourced = assign_materials_from_source(look_objects, raw)
        if sourced:
            payload.setdefault("source_materials", 0)
            payload["source_materials"] += sourced
        repaired = assign_missing_materials(look_objects)
        if repaired:
            payload.setdefault("repaired_materials", 0)
            payload["repaired_materials"] += repaired
        cloned = clone_materials_per_object(look_objects)
        if cloned:
            log(f"material clones per object: {cloned} (each mesh bakes its own texture)")
        normalized_uvs = normalize_uv_layers(look_objects)
        if normalized_uvs:
            log(f"uv layers normalised on {normalized_uvs} mesh(es)")
        look_dir = out_dir / base
        look_dir.mkdir(parents=True, exist_ok=True)
        textures_dir = look_dir / f"{base}_textures"
        baked, bake_report = bake_look_materials(
            look_objects, textures_dir, label=name,
        )
        selected = select_objects(look_objects)
        if not selected:
            payload["looks"][name] = {
                "files": {}, "errors": [f"no MESH/ARMATURE objects found for {name!r}"], "meshes": 0,
            }
            log(f"warning: nothing selectable for {name!r}")
            continue
        log(f"Exporting {name!r} ({len(selected)} object(s)) as {base}")
        entry = export_target(base, look_dir, formats, selected, baked)
        entry["selected_objects"] = len(selected)
        entry["bake"] = bake_report
        entry["look_dir"] = str(look_dir)
        entry["textures_dir"] = str(textures_dir)
        if render_proofs:
            proof_path, proof_error = render_proof(
                look_objects, out_dir / f"proof_{base}.png", label=name,
            )
            entry["proof"] = str(proof_path) if proof_path else None
            if proof_error:
                entry["proof_error"] = proof_error
        else:
            entry["proof"] = None
            entry["proof_skipped"] = True
        payload["looks"][name] = entry
        any_success = any_success or bool(entry["files"])

    payload["ok"] = any_success
    if not any_success:
        payload["error"] = "No format could be exported; see the warnings above."
    emit_result(payload)
    return 0 if any_success else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        trace = traceback.format_exc()
        print(trace, flush=True)
        emit_result({"ok": False, "error": trace.strip().splitlines()[-1], "looks": {}})
        exit_code = 1
    sys.exit(exit_code)
