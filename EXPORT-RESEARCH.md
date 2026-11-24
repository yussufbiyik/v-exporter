# V-Exporter Export Research

This document explains how V-Exporter turns an Appearance Creator Mod (ACM) NPV into
GLB, FBX and OBJ files. It focuses on the parts that were not obvious from the public
documentation, the quirks that shaped the implementation, and the practical lessons that
may help another developer building a similar tool or integrating with this one.

The shorter source and CLI research notes are in [research-notes.md](research-notes.md).
The first-run checks are covered by `--selftest`, `--list` and `--scan`.

## 1. Scope and assumptions

The pipeline targets:

- Cyberpunk 2077 2.5
- Appearance Creator Mod 2.71 or newer
- WolvenKit CLI 10.x with the .NET 10 runtime
- Blender 5.x
- Cyberpunk Blender IO Suite 2.5.0

The exporter is intentionally a file-based pipeline. It does not inject code into the
game, hook the renderer or read the game at runtime. It works from the NPV archive that
ACM already created.

## 2. High-level data flow

```text
ACM NPV archive
  |
  |  WolvenKit uncook
  v
work/<NPV>/source/raw
  |  depot tree: .ent.json, .app.json, .rig.json, .mesh.json,
  |  .glb, .Material.json, .png
  |
  |  dependency heal + material remap
  v
patched raw tree
  |
  |  Blender --background, one process per look
  v
imported scene
  |  entity import
  |  missing-mesh fallback
  |  rig dedupe
  |  material assignment
  |  Cycles base-colour bake
  v
<output>/<NPV>/<look>/
  <look>.glb
  <look>.fbx
  <look>.obj
  <look>.mtl
  <look>_textures/*.png
  |
  +--> <output>/<NPV>/proof_<look>.png
```

The GUI and CLI are only orchestration layers. Both call `pipeline.py`, which in turn
runs WolvenKit and `blender_export.py`.

## 3. ACM NPV structure

Public ACM documentation describes the user-facing workflow but not the full internal
archive layout. The practical rules V-Exporter relies on are:

- An NPV is a folder under
  `<game>\bin\x64\plugins\cyber_engine_tweaks\mods\AppearanceCreatorMod\NPV\<Name>`.
- A packed NPV has at least a `.archive` and a `.archive.xl`. Both should come from the
  same `Create NPV` run.
- An unpacked ACM draft has `.acm\draft\*.pkg`, `.refs`, `.parts`, `.garment` and
  `.glamdoc` files but no `.archive`. Drafts cannot be uncooked.
- The archive is self-contained. Meshes, textures and material definitions used by the
  looks are packed inside it.
- ACM's `Protected` option makes the archive harder to open and can prevent uncooking.
- Look names are stored as appearance names in the generated `.ent.json` or `.app.json`.
- The in-archive depot path is prefixed with `base/acm_npv/<slug>/`. The slug is not
  necessarily the user-visible NPV name.

Because the layout is not a stable public contract, V-Exporter globs for archives,
discovers `.ent.json` and `.app.json` files in the uncooked tree, and parses the JSON
defensively.

## 4. WolvenKit uncook

The first stage is a normal WolvenKit uncook:

```bat
WolvenKit.CLI.exe uncook "<NPV>.archive" -o "<work>\<NPV>\source\raw" -s --uext png
```

Important details:

- The literal `source/raw` path segment matters. The Blender add-on's depot resolver
  expects it. A cache at `work/<NPV>/raw` was moved to `work/<NPV>/source/raw` for this
  reason.
- Do not add `-gp` to an `uncook` call. With some WolvenKit builds it causes the tool to
  ignore the supplied archive and scan the whole game install.
- The first uncook is slow because it writes textures, material sidecars and mesh GLBs.
- The cache is keyed by archive size, archive mtime and CLI mtime. Upgrading WolvenKit
  invalidates it automatically.
- Older CLI versions may reject newer options. The pipeline retries with a reduced
  option set if the first attempt fails with an option error.

After the main uncook, the tool may run targeted WolvenKit work:

- `extract` to pull a file that `uncook` skipped.
- `convert serialize` to turn extracted `.mi`, `.mlsetup` or `.rig` files into the JSON
  the add-on reads.
- `export` to create a missing mesh GLB with materials.

The per-mesh `export` step uses `-gp <game root>` and a material repo configured through
WolvenKit's `appsettings.json`. This is separate from the `uncook` path.

## 5. Dependency healing

An NPV archive is self-contained for the look, but the material chain is not always
inside the entity closure. A mesh's `Material.json` points at a `.mi` instance, which
points at a `.mlsetup`, `.mltemplate`, mask and texture files. A plain uncook may leave
those out.

V-Exporter heals them in phases:

1. Walk the cached material JSONs and collect depot references.
2. Mirror files that already exist between the vanilla depot path and the
   `base/acm_npv/<slug>/` prefixed path the add-on resolves.
3. For missing files, search the game and mod archives with WolvenKit `extract`.
4. Serialize `.mi`-family files with `convert serialize`.
5. Uncook textures with `uncook --uext png`.
6. Repeat for up to three passes, because each healed `.mi.json` can reveal more files.

This step is why an export can take longer than the initial uncook. Missing references
are logged; the exporter does not fail the whole look because one texture is absent.

## 6. Material remap before Blender

WolvenKit writes its own `Albedo`, `BaseColor` and similar stand-ins into
`Material.json`. The add-on reads those fields, so simply healing the `.mi` graph does
not change what Blender sees.

`material_remap.py` walks the same `Material.json` files the add-on imports and:

- follows the `$type` chain through `.mi`, `.mlsetup`, `.mlmask` and `.mltemplate`
- finds the first layer that names a real texture
- rewrites `BaseColor`, `Albedo`, `Normal` and `Roughness`
- normalizes odd UV scales and offsets that would tile a placeholder off-image
- resets colour scales that would multiply the real texture to black or magenta
- backs up each file once as `*.orig.json`

For multilayer materials, it also:

- finds the matching `.mesh.json` and its multilayer setup
- walks layers in order
- picks the first layer with a cached, non-default `colorTexture`
- forces a flat metal-base template so the add-on's MetalBase handler can evaluate it
- writes the winning layer's `colorScale` tint into `remap_tints.json`

Eye, head and plastic material families are blocked from flattening. An earlier
ungated flatten turned every iris into green nylon. The blocked entries are restored
from their `*.orig.json` backup before each run.

## 7. Mod garment material resolution

Modded clothing often has a `Material.json` whose `BaseColor` is still
`makigai\uvchecker`, because the mod's `.mi` chain is not in the cache or does not use
the vanilla material structure.

The exporter tries several fallbacks:

1. Follow the mod's own `.mi`, `.mlsetup` or `.mltemplate` definition.
2. Match the mod's texture folder by material name.
3. Match by a mesh-part token and pick a numbered variant folder.
4. Fill albedo, normal and roughness from the chosen texture set.
5. Let trim materials borrow the garment albedo.

Some mods use a flat `white.xbm` tint mask as the albedo. That bakes white and renders
light grey. The tint sidecar carries a dark multiplier for those names, so the exported
pixels become the intended colour.

This is heuristic work. A mod with an unusual material graph may still fall back to a
flat colour or a traced albedo.

## 8. Blender entity import

The add-on's file browser operator cannot select a specific appearance from a script.
V-Exporter therefore uses the add-on's internal API:

```python
import_entity(
    EntityImportRequest(
        appearances=("LookName",),
        depot_root=raw_dir,
        with_materials=True,
        ...
    )
)
```

The request keyword arguments are filtered against the installed add-on dataclass, so
small schema differences do not crash the run.

Two scene choices matter:

- Always use `blender --background`. This is what makes the pipeline headless.
- Never use `--factory-startup`. Factory startup disables user add-ons, so the CP77
  importer would be missing.

One Blender process is used per look. This avoids material, armature and collection
state leaking between looks, keeps failures attributable to one look, and survives a
crash on a single look. The only exception is the fallback where no look names can be
parsed: Blender imports `ALL` appearances once and exports each collection separately.

## 9. Missing meshes and head fallback

The entity sometimes references components that have no usable mesh path:

- A skinned mesh component with an empty `mesh` field but a cached
  `<component>.glb`.
- A morph-target-only component with a `morphResource`. The morphed body and head live
  in `baked/<hash>.morphtarget.glb`, not in a file named after the component.
- Head and eye parts that are not referenced by any component at all.

The exporter resolves these by name, by morph resource stem, and by a head-family pass
that imports cached `*basehead*.glb` siblings. Duplicate parts are skipped by bounding
box and vertex count. A part that cannot bind to the imported skin is rigidly bound to
the head bone.

This is why the torso and face can appear even though the add-on logged a warning about
a missing mesh resource.

## 10. Rig deduplication

The add-on imports each asset under its own armature and also under the merged entity
rig. Exporting both trees duplicates geometry and is a common cause of "exploded" FBX
imports.

`dedupe_to_primary_rig()` keeps the primary merged rig and the meshes reachable from it,
drops secondary armature subtrees, skips objects that are not in the current view layer,
and removes helper objects such as `BoneCustomShape*` and `Icosphere*`.

If no mesh survives the filter, the function returns the original object list instead of
handing the exporter an empty selection.

## 11. Base-colour bake

The add-on builds a shader node graph with placeholder masks, colour attributes, layer
groups and tints. No file exporter reproduces that graph. glTF drops the attribute node,
and FBX/MTL writers only follow one image into Base Color.

The pipeline therefore bakes a flattened diffuse image per material:

- Cycles, CPU
- 1024x1024
- 8 samples
- direct and indirect light disabled
- margin 8
- 20 minute total budget

The bake happens inside the imported scene, so it captures the colour the add-on
resolves, including textures and tints that no exporter can serialize directly.

Each baked material is swapped for a simple stand-in during export:

```text
Image Texture -> Principled Base Color
```

The originals are restored in a `finally` block. If the bake fails, the material keeps a
traced albedo image or a flat colour. A bake whose mean RGB is below 0.002 is considered
blank; its PNG is deleted so a black texture is not shipped.

## 12. Why glTF needs baked pixels

A node-based tint cannot survive glTF export. If the colour is applied with a Mix node,
the exporter drops it. The tint has to be multiplied into the image pixels before the
PNG is written.

This is why `remap_tints.json` exists. `material_remap.py` records the layer colour,
and `blender_export.py` applies it to the baked image with `_tint_image()` before saving.

The same limitation is why multilayer garments are flattened to a simple base-colour
texture rather than exported as their original layer stack.

## 13. Formats and their quirks

### GLB

- Textures are embedded in the binary.
- Materials use a simple glTF PBR graph.
- Skins and shape keys are exported.
- Modifiers are not applied, so shape keys survive.
- glTF is Y-up. The exporter uses `export_yup=True`.
- glTF cannot represent the add-on's full node graph, so the baked base colour is what
  the file carries.

### FBX

- Textures are embedded by copying them during export and using `embed_textures=True`.
- `use_armature_deform_only` and `FBX_SCALE_UNITS` are used to keep some viewers from
  scattering the skinned meshes.
- `add_leaf_bones=False`.
- Shape-key-safe modifier options are used instead of `export_apply`.
- A third-party viewer may still mishandle bones or units. Re-import in Blender or use
  the GLB if the FBX looks wrong.

### OBJ

- OBJ has no armature or skinning. It is exported from the evaluated render mesh.
- The `.mtl` points at `<look>_textures/` with relative paths.
- Traced fallback textures are copied into `<look>_textures/` so the OBJ stays
  self-contained.
- One material per used slot is written, without leftover `__cp77` names.

### Proof images

The proof render is optional. It uses Cycles on the CPU with a 1024x1024 orthographic
camera and a three-point area light rig. The camera is a three-quarter front view. The
proof uses the imported Blender materials, not a game screenshot, so effects that the
add-on cannot import will not appear.

## 14. Quirks and failure modes

These are the issues that most often surprise developers:

- **Hair is often missing.** CCXL hair can use an animated `.rig` and a multi-buffer
  `hair_shadow.mesh`. WolvenKit can find the mesh, but the GLB exporter needs
  single-buffer meshes and produces no GLB. The add-on then cannot import it.
- **Rigs can be unavailable.** Some NPV-local `.rig` files are not in the game archives.
  When the rig cannot be resolved, the meshes are imported unrigged in a static bind
  pose.
- **Corrupt NPV meshes happen.** A mesh can be truncated or unreadable. The pipeline
  tries to unbundle a healthy copy from the base game or mod archives and export that.
- **Entity selection is heuristic.** An archive can contain several `.ent.json` files,
  including rig helper entities. The tool scores candidates by name, path, appearance
  count and plausible character size. `entity_overrides` is the manual escape hatch.
- **Drafts are common.** A saved look is not an NPV. ACM must pack the archive with
  `Create NPV` first.
- **Protected archives can fail.** Recreate with Protected off.
- **Look names are sanitized.** Characters such as `<>:"/\|?*` become `_` in file names.
  The original names remain in `export-summary.json`.
- **Add-on versions drift.** The internal import API is not a public stable interface.
  Keyword filtering helps, but a future add-on may require code changes.
- **Material chains are incomplete.** A missing `.mi` or texture is logged and the
  material falls back. This is expected for mod resources that ACM did not pack.
- **UV islands can overlap.** Meshes that share a repaired skin material bake into one
  1024x1024 atlas. Overlapping islands from different meshes can bleed into each other.
- **The bake is not a PBR recovery tool.** It captures base colour only. Normals,
  roughness, metalness and parallax are not exported from the add-on graph.
- **Uncook cache size grows.** A full NPV can require several GB. Delete `work/<NPV>`
  or use Force re-uncook to rebuild.
- **WolvenKit version matters.** Game 2.5 archives need WolvenKit 10.x and .NET 10. A
  9.x CLI may start but is not reliable with 2.5 archives.

## 15. Practical advice for similar tools

If you are building another ACM or Cyberpunk export tool, these choices saved a lot of
time:

1. Glob, do not assume. ACM's internal folder and archive layout is unpublished.
2. Treat the NPV as self-contained, but expect missing material chains outside the
   entity closure.
3. Keep the literal `source/raw` path segment for the Blender add-on.
4. Use WolvenKit 10.x for game 2.5 and do not pass `-gp` to `uncook`.
5. Mirror mod files to both the vanilla depot path and the
   `base/acm_npv/<slug>/` prefixed path.
6. Parse JSON defensively. Appearance entries can be strings, dicts, `$value` wrappers
   or `Data` wrappers.
7. Import one look per Blender process, and never use `--factory-startup`.
8. Use the add-on's internal import API when you need a specific appearance.
9. Dedupe to one armature before exporting FBX.
10. Bake base colour into pixels if the target format cannot represent the node graph.
11. Treat every material, rig and texture step as best-effort. A missing asset should
    degrade one material, not fail the whole look.
12. Write a machine-readable summary next to the output. It makes debugging and
    automation much easier.
13. Keep user paths, NPV names and character names out of committed files.

## 16. Testing checklist

A minimal verification pass for a new look:

1. Run `python v_exporter.py --selftest`.
2. Run `python v_exporter.py --list` and confirm the NPV is packed and the looks are
   listed.
3. Export one look.
4. Confirm `<look>.glb`, `<look>.fbx`, `<look>.obj`, `<look>.mtl` and
   `<look>_textures/` exist.
5. Re-import the GLB into a fresh Blender scene. Check meshes, armature and textures.
6. Re-import the FBX. Check for one armature and no extra leaf bones.
7. Open the OBJ with its `.mtl` and `<look>_textures/` folder next to it.
8. Check `export-summary.json` for errors, bake failures and fallback materials.
9. If proofs are enabled, confirm `proof_<look>.png` is 1024x1024 and shows the look.

## 17. References

- Appearance Creator Mod: <https://www.nexusmods.com/cyberpunk2077/mods/10795>
- ACM NPV guide:
  <https://wiki.redmodding.org/cyberpunk-2077-modding/modding-guides/npcs/npv-v-as-custom-npc.md>
- WolvenKit: <https://github.com/WolvenKit/WolvenKit>
- Cyberpunk Blender IO Suite:
  <https://github.com/WolvenKit/Cyberpunk-Blender-add-on>
- WolvenKit Blender import/export docs:
  <https://wiki.redmodding.org/cyberpunk-2077-modding/for-mod-creators-theory/modding-tools/wolvenkit-blender-io-suite/wkit-blender-plugin-import-export>
- Repo-specific source notes: [research-notes.md](research-notes.md)
