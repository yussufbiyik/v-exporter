# V Exporter - Research Notes (2026-11-21)

Research for a standalone tool that finds ACM (Appearance Creator Mod) NPV folders, unpacks them,
and exports each saved look as GLB (+FBX/OBJ) via WolvenKit.CLI + headless Blender with the
Cyberpunk Blender IO Suite. Target: Cyberpunk 2077 **2.5**, ACM **2.0+** (current: 2.8.0).

Verified sources used: wiki.redmodding.org (cyberpunk-2077-modding + wolvenkit wikis),
github.com/WolvenKit/WolvenKit (main branch source), github.com/WolvenKit/Wolvenkit-Resources,
github.com/WolvenKit/Cyberpunk-Blender-add-on (main branch source), ACM Nexus page,
plus a read-only inspection of a local game install.

---

## 1. ACM NPV output structure

### Verified facts

- NPV folder location (ACM 2.x):
  `Cyberpunk 2077\bin\x64\plugins\cyber_engine_tweaks\mods\AppearanceCreatorMod\NPV\<your_NPV_name>`
  - https://wiki.redmodding.org/cyberpunk-2077-modding/modding-guides/npcs/npv-v-as-custom-npc.md
  and ACM Nexus FAQ #1 ("Everything lives in ...\AppearanceCreatorMod: saved appearances in
  **Appearances**, outfits in **Outfits** and each NPV in **NPV\<Name>**") -
  https://www.nexusmods.com/cyberpunk2077/mods/10795
- The NPV folder contains **at minimum a `.archive` and a `.archive.xl`**, and they must be from the
  same "Create NPV" run (ACM Nexus FAQ #3: "Make sure its **.archive** and its **.archive.xl** come
  from the same Create NPV. An old .xl left behind by a repack crashes it").
- Optional user-supplied `icon.png` or `icon.jpg` (100×100 px) in the NPV folder; ACM's "Capture Icon"
  writes it (wiki guide, step 7).
- "Share" creates a ready-to-send `.zip` **in the NPV folder** (wiki step 10; Nexus description).
- The NPV is **self-contained**: "Every look you save becomes one of the NPV's appearances, and every
  file those looks use is packed inside, so whoever you share it with does not need the clothing,
  body or character-creator mods your looks use." (Nexus description, NPV section).
- Appearances/looks are named by the user in ACM's **"Look Name"** box when clicking "Save Look"
  (wiki steps 4-5); ACM 2.71+ required for the in-game NPV flow.
- **Gotcha:** ACM's Settings option **"Protected"** "makes the archive inside [the share zip] much
  harder to open" (Nexus description + "A Note on Sharing NPVs"). A protected NPV archive may resist
  WolvenKit uncooking - the tool should detect and warn. *(severity of protection unverified)*
- No public ACM GitHub repository exists - **the exact internal NPV structure is unpublished.**
  MaximiliumM's GitHub has 15 repos and none is ACM; `MaximiliumM/appearancemenumod` is only a
  compiled mirror of **AMM** (Appearance Menu Mod), not ACM
  (https://github.com/MaximiliumM?tab=repositories, https://github.com/MaximiliumM/appearancemenumod).
  The wiki explicitly does not document a file-by-file listing of an ACM NPV folder (confirmed via
  the wiki's documentation query interface).
- Local inspection (read-only): the game install and ACM's mod folder were inspected
  without modifying them. **The exact on-disk layout could not be confirmed from a
  published sample.** The tool therefore *discovers* files by globbing rather than
  assuming names.

### (unverified)

- Exact archive file name inside `NPV\<Name>` (presumably `<Name>.archive` + `<Name>.archive.xl`,
  but unconfirmed).
- Which `.json` manifests (if any) ACM writes into the NPV folder alongside the archive.
- The in-archive depot paths and file names of the `.ent`/`.app` ACM generates, and how the
  user-typed look names are sanitized into appearance names inside the `.ent`/`.app`.
- Contents/naming of the Share `.zip`.

**Practical consequence for the tool:** glob `NPV\<Name>\*.archive` (ignore `*.archive.xl` for
export purposes) and after uncooking, glob the output tree for `*.ent.json`. NPV appearance names =
the look names the user typed in ACM; read them from the `.ent.json` (see §5 for the exact JSON
fields) or let the Blender entity importer list them.

---

## 2. WolvenKit.CLI (game 2.5 → WolvenKit 10.x)

### Version facts (verified)

- Latest stable release: **9.1.1** (2026-11-08), which "Add[ed] game version compatibility check with
  popup to ensure **WKit >= 10.x with 2.5**". **10.0.0-beta.1** (2026-10-24) has "Early support for
  game version 2.5". So for game 2.5 you need **WolvenKit 10.0.0-beta.1 or newer**; all 10.x releases
  require the **.NET 10 runtime** (https://github.com/WolvenKit/WolvenKit/releases).
- CLI download asset: **`WolvenKit.Console-<version>.zip`** (e.g.
  `WolvenKit.Console-10.0.0-beta.1.zip`, 42.9 MB; there is also a Linux build). Binary inside is
  `WolvenKit.CLI.exe`. (Asset names verified on the release page; exe name inside the zip is
  *(unverified - check after download)*.)
- Official CLI docs: https://wiki.redmodding.org/wolvenkit/wolvenkit-cli/usage/command-list.md
- The Blender add-on side needs **WolvenKit ≥ 9.1.0 for game 2.31 / ≥ 10.0 for game 2.5** -
  https://wiki.redmodding.org/wolvenkit/modding-community/exporting-to-blender.md

### Command syntax (verified from source: `WolvenKit.CLI/Commands/UncookCommand.cs`, main branch)

`uncook` (aliases: `unbundle-and-export`, `extract-and-export`):

```
uncook <path(s)> [options]
```

| Option | Meaning (from source) |
|---|---|
| `path` (positional) | Input paths to `.archive` files **or folders**. `-p/--path` is deprecated. |
| `--outpath, -o <dir>` | Output directory (required - errors "Please fill in an output path."). |
| `--gamepath, -gp <dir>` | **Do not pass for mod archives** - loads the *base game* archives too, and uncook then processes every loaded archive. |
| `--raw, -or <dir>` | Optional separate raw output dir; **defaults to `-o`** (leave it off so everything lands in one tree). |
| `--pattern, -w <pattern>` | Search pattern, e.g. `*.mesh` (pattern wins over regex). |
| `--regex, -r <regex>` | Regex filter. |
| `--uext <fmt>` | Texture format: `tga, bmp, jpg, png, dds` (also applied to mesh material textures). Use `png` for the Blender add-on's default. |
| `--hash <hash>` | Extract a single file by hash. |
| `--unbundle, -u` | Also unbundle files. |
| `--forcebuffers, -fb <ext>` | Force uncook to buffers for given extension, e.g. `mesh`. |
| `--serialize, -s` | Serialize cooked files to JSON. Output file is `{archive-relative-path}{filename}.json` - e.g. `foo.ent` → `foo.ent.json` (verified in `WolvenKit.Modkit/RED4/Uncook.cs`: `Path.Combine(outDir.FullName, $"{relFileFullName}.json")`). |
| `--mesh-exporter-type <t>` | `Default` or `Experimental` (`REDmod` unsupported - errors out). |
| `--mesh-export-type <t>` | **`MeshOnly, WithMaterials (Default), WithRig, Multimesh`** (exact enum + help string in UncookCommand.cs). |
| `--mesh-export-material-repo <dir>` | Material repo location; defaults to `-o` outpath. |
| `--mesh-export-lod-filter` | "Filter out lod models." - turn ON to match the documented GUI workflow ("LOD Filter is on"). |
| `--mesh-export-experimental-merged-export` | Experimental merged glb export; supports only Default/WithMaterials; no re-import. |
| `--opus-hashes` | Wwise opus export hashes (`*` for all). |

**Targeting a single mod archive (verified in `WolvenKit.Modkit/RED4/Tasks/UncookTask.cs`):** pass the
`.archive` file as the positional argument and **omit `--gamepath`** - then
`_archiveManager.LoadModArchive(file.FullName, false)` loads *only that archive* and nothing else is
processed. Passing a folder loads all `.archive` files in it (`LoadAdditionalModArchives`).

`unbundle` (alias `extract`): same targeting rules, extracts cooked files without buffers.
Documented usage: `unbundle -p "PATH TO ARCHIVE" -o "OUTPUT PATH" -w *.mesh`
(https://wiki.redmodding.org/wolvenkit/wolvenkit-cli/usage/command-list.md - note the wiki's `-p`
is the deprecated flag; the positional path form is current).

`export` (per-file): `export --uext png -p "PATH TO FILE"` - converts a single REDengine file
(.mesh/.xbm/.mlmask...) to raw (glb/dds). Useful for targeted re-exports.

`pack`: `pack -o <output> <folder>` - packs a folder into `.archive` (ignores `.xl`); not needed by
this tool, listed for completeness.

### Ready-to-embed command (single NPV archive → full raw tree)

```bat
WolvenKit.CLI.exe uncook "D:\...\NPV\MyV\MyV.archive" ^
  -o "D:\Work\MyV\raw" ^
  -s --uext png --mesh-export-type WithMaterials --mesh-export-lod-filter
```

- `-s` produces `.ent.json`, `.app.json`, `.rig.json`, `.mesh.json`, etc. (every cooked file type in
  the archive - NPV archives are self-contained, so this yields everything the Blender entity import
  needs; see §5).
- Meshes export as `.glb` with materials (`WithMaterials` = GUI default: "by default materials but
  no rig are included", WolvenKit.Common `ExportArgs.cs`), plus a `<mesh>.Material.json` and uncooked
  `.png` textures into the material repo (= outpath when `--mesh-export-material-repo` is omitted).
- LOD filter drops `_lod` variants.
- Do not pass `-or` unless you want glb/png split from the jsons - the Blender add-on is happiest with
  one tree.
- `--mesh-export-type WithRig` is available if you ever need the skeleton inside the mesh glb; the
  documented entity workflow does **not** need it (the rig comes from `.rig.json`, see §5).
- *(unverified: nothing - this option set is taken verbatim from current source.)*

---

## 3. Cyberpunk Blender IO Suite - headless usage

Add-on repo: https://github.com/WolvenKit/Cyberpunk-Blender-add-on (module/folder name when
installed: **`i_scene_cp77_gltf`**; the wiki's install page confirms "the Wolvenkit Blender IO Suite
will be installed to the subfolder `i_scene_cp77_gltf`").
Docs: https://wiki.redmodding.org/cyberpunk-2077-modding/for-mod-creators-theory/modding-tools/wolvenkit-blender-io-suite/wkit-blender-plugin-import-export

### Requirements (verified)

- **Blender 5.0** strictly (README: "Blender versions below 4.5 will not work … The current release
  will *not* work with versions other than 5.0"); the entity-export wiki guide (Nov 2026) uses
  **Blender 5.3 + add-on 2.5.0 + WolvenKit ≥10.0 for game 2.5**.
  https://wiki.redmodding.org/wolvenkit/modding-community/exporting-to-blender.md
- Add-on release 2.5.0: https://github.com/WolvenKit/Cyberpunk-Blender-add-on/releases

### Operator idnames (verified from `i_scene_cp77_gltf/importers/operators.py`, main branch)

| Operator | bl_idname | Purpose / file filter |
|---|---|---|
| CP77Import | **`io_scene_gltf.cp77`** | "Import Cyberpunk glTF / GLB" - `*.glb;*.gltf` mesh import |
| CP77EntityImport | **`io_scene_gltf.cp77entity`** | "Import Ent from JSON" - `*.ent.json` entity/appearance assembly |
| CP77StreamingSectorImport | `io_scene_gltf.cp77sector` | `*.cpmodproj` streaming sectors |
| CP77ImportRig | `import_scene.rig` | `*.rig.json` (bind_pose: `A-Pose`/`cp77_t_pose`) |
| CP77AnimGraphImport | `import_scene.cp77_animgraph` | `*.animgraph.json` |
| CP77MocapImport | `import_scene.cp77_mocap` | BVH/rlMotion retargeting |

Key properties of `io_scene_gltf.cp77` (from source):
`filepath`, `files`, `directory`, `appearance_list` (collection), `all_appearances` (bool),
`image_format` (`png|dds|jpg|tga|bmp|jpeg`, default png), `hide_armatures` (default True),
`import_garmentsupport` (default True), `import_tracks`, `refresh_asset_index`, `animation_target`
(`AUTO|SELECTED|NEW`). Materials toggle lives on the scene:
**`bpy.context.scene.cp77_panel_props.with_materials`** (must be set for scripted imports).

Key properties of `io_scene_gltf.cp77entity`:
`filepath`, `appearance_list` (collection of `{name, selected}`), `all_appearances` (bool),
`materials` (bool, default True), `proxies`, `occluders`, `shadow_meshes`, `refresh_index`,
`exclude_meshes`, `inColl` (parent collection name), plus per-category include toggles.

**Appearance selection (verified in `importers/common/appearance_list.py`):**
`appearance_request(selected, all_appearances, resolved_default)` returns
- `("ALL",)` when `all_appearances` is True,
- the ticked names, or
- `("default",)` when nothing is ticked (default resolves to the entity's defaultAppearance).
The import dialog's "Enter ALL for all appearances" (add-on README) maps to this.

### Headless (`blender --background --python`) usage

- bpy.ops calls from Python run in `EXEC_DEFAULT`, i.e. **`execute()` directly - no file browser**,
  so both operators work headless. `import_entity` itself is pure bpy + file IO.
- Operator call, GLB mesh import:
  ```python
  import bpy, addon_utils
  addon_utils.enable("i_scene_cp77_gltf", default_set=True)   # defensive; normally auto-loads
  bpy.context.scene.cp77_panel_props.with_materials = True
  bpy.ops.io_scene_gltf.cp77(filepath=r"D:\Work\mesh.glb", image_format='png')
  ```
- Operator call, entity import (all appearances):
  ```python
  bpy.ops.io_scene_gltf.cp77entity(filepath=r"D:\Work\raw\base\...\myent.ent.json",
                                   all_appearances=True, refresh_index=True)
  ```
  `refresh_index=True` forces a rescan of the raw tree on first run (otherwise the asset index may
  reuse a stale scan - verified property description in source: "Rescan the source/raw tree before
  import").
- **Selecting one specific appearance via bpy.ops is not possible** - `appearance_list` is a
  CollectionProperty and cannot be passed through operator kwargs. Scripted `bpy.ops` calls with no
  ticks resolve to `("default",)` (source comment: "A file chosen without a check … or a scripted
  call gets its rows here"). For a *specific* appearance, call the internal API directly (below),
  or pass `all_appearances=True` and export the resulting per-appearance collections separately.

### Direct internal API (verified: `importers/entity/options.py` + `importers/entity/execution.py`)

```python
from i_scene_cp77_gltf.importers.entity.options import EntityImportRequest
from i_scene_cp77_gltf.importers.entity.execution import import_entity

result = import_entity(
    EntityImportRequest(
        with_materials=True,
        filepath=r"D:\Work\raw\base\...\myent.ent.json",
        appearances=("MyLookName",),          # ("ALL",) or ("default",) or ("name1","name2")
        include_animations=False,             # static look export
        include_shadow_meshes=True,           # default True (matches GUI)
        parent_collection_name="",             # optional collection name
        depot_root=r"D:\Work\raw",            # root of the uncooked tree
        index_policy=IndexPolicy.REFRESH,     # from i_scene_cp77_gltf.assetio.index
    )
)
# result: ImportResult(created_items=<tuple of collections>, warnings=(...), failures=(...))
# ImportEntityRequest.__post_init__ normalizes appearances: str->(str,), None->("",), empty->("",)
# "" resolves like "default" in normalize_appearance_requests (seeded default resolution is
# deterministic per entity+request - verified in execution.py).
```

Full `EntityImportRequest` fields (from source): `with_materials`, `filepath`, `appearances`,
`excluded_meshes`, `include_collisions`, `include_phys`, `include_entity_colliders`,
`include_mesh_colliders`, `include_occluders`, `include_proxies`, `include_lights`,
`include_light_channels`, `link_lights`, `keep_light_channel_volumes`, `include_shadow_meshes`,
`include_inkwidgets`, `include_facial_setup`, `include_facial_animations`,
`parent_collection_name`, `animation_files`, `include_animations`, `parsed_entity`,
`imported_collections_out`, `imported_rigs_out`, `existing_rig`, `asset_index`, `depot_root`,
`documents`, `material_resources`, `report_owner` (only used as a palette-batching owner; the
operator passes itself - `None` appears acceptable but is *(unverified)*), `manage_master_visibility`,
`transactional`, `defer_child_of_inverse`, `index_policy`, `import_id`.

The rig is built **exclusively from JSON rigs** (`execution.py` comment: "The entity rig is always
constructed exclusively from JSON rigs") → `.rig.json` files must exist in the raw tree (§5).

Each imported appearance becomes a collection named `<ent>_<appearanceName>` with properties
`appearanceName`, `cp77_entity_source`, etc. (`_appearance_collection` in execution.py) - handy for
selecting what to export to FBX/OBJ.

### Headless caveats / (unverified)

- **Materials in `--background` mode: (unverified).** The material build sets Vulkan/Cycles via the
  *operator* (`SetVulkanBackend`/`SetCyclesRenderer` are only invoked in the operator's `execute`,
  so the direct `import_entity` path skips them), and the code has an `opengl_shader_warning()` path.
  Node/material/texture setup is file IO and should work headless, but no official statement or test
  of full headless material baking was found. **First action for the implementing session: run one
  headless import of a known-good WolvenKit export and check for shader/GPU errors.**
- Blender headless add-on loading: do **not** use `--factory-startup` (it disables user-enabled
  add-ons); either rely on the user's normal startup or call
  `addon_utils.enable("i_scene_cp77_gltf", default_set=True)` as above. *(pattern verified generally;
  add-on-specific behaviour unverified)*

---

## 4. FBX / OBJ export from the assembled scene (bpy)

### FBX - `bpy.ops.export_scene.fbx` (built-in)

```python
bpy.ops.export_scene.fbx(
    filepath=r"D:\Out\MyV_MyLook.fbx",
    use_selection=True,          # select the appearance collection's objects + armature first
    object_types={'ARMATURE', 'MESH'},
    add_leaf_bones=False,        # default True adds fake leaf bones to every bone
    export_apply=False,         # CRITICAL - see below
    use_custom_props=False,      # add-on adds many custom props (depotPath etc.) - skip
    global_scale=1.0,
)
```

Verified gotchas (Blender docs + issue tracker):
- **`export_apply` ("Apply Modifiers") "prevents exporting shape keys"** - official API doc warning
  (https://docs.blender.org/api/current/bpy.ops.export_scene.html). CP77 imports carry facial shape
  keys, so keep it `False`. (Armature modifier is never applied by this option anyway.)
- **Triangulate + shape keys bug**: enabling triangulation drops shape keys
  (https://projects.blender.org/blender/blender-addons/issues/104714). Don't triangulate on export
  if shape keys matter.
- `add_leaf_bones` defaults to True and appends extra end bones - set False for a clean rig.
- Skinned multi-mesh scenes are supported: one armature + N meshes each with an Armature modifier
  exports fine; **select armature + all mesh objects** when using `use_selection=True`. FBX is the
  only one of the three formats that keeps skinning + shape keys.
- Materials: only a subset of the add-on's complex node graphs survives FBX; procedural nodes
  (mlsetup-built multi-layer groups) export as approximations. Expect usable-but-not-identical
  materials. *(general FBX limitation; CP77-specific fidelity unverified)*
- Non-uniform object scale on meshes deforms skinning in the target app - apply/clear transforms
  before export. *(standard practice; not CP77-specific)*

### OBJ - `bpy.ops.wm.obj_export` (Blender ≥ 4.0; the old `export_scene.obj` was removed)

```python
bpy.ops.wm.obj_export(
    filepath=r"D:\Out\MyV_MyLook.obj",
    export_selected_objects=True,
    export_materials=True,
    export_eval_mode='RENDER',   # exports the evaluated (deformed) mesh
    export_animation=False,
)
```

Verified gotchas:
- **OBJ has no support for armatures, skinning, parenting or shape keys** - official manual:
  "There is no support for armatures, lights, cameras, empty objects, parenting, or
  transformations." (https://docs.blender.org/api/4.0/bpy.ops.wm.html and the OBJ manual page).
  Skinned meshes come out in **rest pose** unless you pose the armature and export evaluated data
  (`export_eval_mode='RENDER'` grabs the depsgraph-evaluated mesh - the parameter exists in the
  official operator docs; whether a posed CP77 rig fully evaluates headless is *(unverified)*).
- The C++ exporter's parameter for evaluation mode is `export_eval_mode` (NOT `evaluation_mode` -
  passing the wrong name raises TypeError; verified in the Blender developer docs skill listing).
- Multi-mesh scenes: all selected objects land in one `.obj` + `.mtl`.

### GLB for the "assembled look"

For maximum fidelity, export with Blender's standard glTF exporter
(`bpy.ops.export_scene.gltf(filepath=..., use_selection=True, export_materials='EXPORT' ...)`) -
it bakes textures/materials the importer already set up. The add-on's own
"Export Selection to GLB for Cyberpunk" (`io_scene_gltf.cp77_glb` family - *(exact exporter idname
not yet confirmed; the exporters live in `i_scene_cp77_gltf/exporters/` and the README documents the
menu entry)*) produces *game-reimportable* glb, which is not what an external tool wants.

---

## 5. Entity export prerequisite: "find used files" - automatable?

### What the GUI does (verified)

WolvenKit's documented character workflow: add the `.ent` to a WolvenKit project, then run
**`Export_Ent.wscript`** from Tools → Script Manager ("Entity export script for import with the
Wolvenkit Blender IO Suite", v2.0). Source:
https://github.com/WolvenKit/Wolvenkit-Resources/blob/main/Scripts/Export_Ent.wscript
(auto-synced into WolvenKit at launch). It:

1. Collects every `.ent` in the project (`wkit.GetProjectFiles('archive')`).
2. `FindEntRigs`: reads `Data.RootChunk.components[].rig.DepotPath` and, for every
   `appearances[].appearanceResource.DepotPath` (.app file), reads
   `appjson.RootChunk.appearances[].Data.components[].rig.DepotPath` → collects rigs (skips
   `base_rig`).
3. `ParseFile` recursion: parses `.ent/.app/.mesh/.anims` JSON (`wkit.GameFileToJson`) and walks every
   `ResourcePath` recursively (so it finds all transitively referenced meshes, incl. embedded files).
4. Serializes **`.app`, `.ent`, `.mesh`, `.rig`** to JSON in the project **raw** folder with naming
   `.app.json` / `.ent.json` / `.rig.json` / `.mesh.json`.
5. `wkit.ExportFiles([...])` exports `.mesh` (+ `.anims`) to **glb with the default export settings**
   (GUI default = WithMaterials + LOD filter + png per the exporting guide's "What does the script
   do by hand" section).

So the Blender entity import consumes: **`.ent.json` + `.app.json` + `.rig.json` + `.mesh.json` +
mesh `.glb` + `<mesh>.Material.json` + uncooked textures (.png)** - confirmed also by the vehicles
guide ("Importing vehicles to blender needs the `.ent`, `.app` & `.rig` files in .json format, along
with all the meshes…",
https://wiki.redmodding.org/wolvenkit/modding-community/exporting-vehicles.md).
The add-on additionally reads appearance names from the entity and reads a material sidecar
(`<glb>.Material.json` - `material_sidecar_path` in operators.py; naming per the WolvenKit glossary:
"the json will be named after the mesh file then suffixed with .Material.json", e.g.
`judy_01.Material.json`).

### Is there a CLI/automatable equivalent? (verified: no script command - but uncook covers it)

- **WolvenKit.CLI has no wscript/script command.** The CLI command set (verified from
  `WolvenKit.CLI/Commands/` folder listing + docs) is: `pack, unbundle, uncook, archive, cr2w,
  convert, hash, import, export, conflicts, build, oodle, settings, wwise`. There is **no "find used
  files"/"export with dependencies"** command - the recursive dependency resolution exists only as
  the GUI wscript (JS, `wkit` API) or the GUI right-click "Find used files".
- Therefore the tool must **either** parse the `.ent.json`/`.app.json` itself (the wscript above shows
  exactly which fields: `RootChunk.components[].rig.DepotPath`,
  `appearances[].appearanceResource.DepotPath`, and generic ResourcePath walking) **or** avoid the
  problem entirely:
- **ACM NPV archives are self-contained** (verified, Nexus: "Every look you save becomes one of the
  NPV's appearances, and every file those looks use is packed inside"). So simply uncooking **the
  entire NPV archive** with `-s --uext png --mesh-export-type WithMaterials --mesh-export-lod-filter`
  (§2) produces the same file set the wscript would have produced: all `.ent.json/.app.json/
  .rig.json/.mesh.json` + all mesh glbs + textures + Material.jsons, in archive-relative depot paths.
  This is the recommended approach. No dependency resolution needed.
- Note: the old WolvenKit-8 wiki warns "Wscript cant export files from other mods currently, if
  you're exporting an NPV which uses other [mods' files]" - a GUI limitation that does **not** apply
  to the CLI-uncook-everything approach, because ACM already packed all referenced files (including
  other mods' meshes/textures) into the NPV archive.
- After uncooking, locate the entity by globbing `**/*.ent.json` in the output tree. *(The depot path
  ACM uses inside its archive is unpublished - see §1.)*

---

## 6. Recommended pipeline

Concrete step-by-step for the tool. Detect the game root generally by locating
`bin\x64\Cyberpunk2077.exe` or the CET mods dir; also support Vortex
deployed archives - but **never write** into game/Vortex folders.

### Step 0 - Locate & inventory (read-only)

```
NPV_ROOT = <game>\bin\x64\plugins\cyber_engine_tweaks\mods\AppearanceCreatorMod\NPV
for each dir <NPV_ROOT>\<Name>:
    archives = glob("<NPV_ROOT>\<Name>\*.archive")      # export source
    xl       = glob("<NPV_ROOT>\<Name>\*.archive.xl")   # informational only
    if not archives: report "no .archive (not yet created, or Protected?)"
```

### Step 1 - Uncook the NPV archive (WolvenKit.CLI 10.0.0-beta.1+, .NET 10)

```bat
WolvenKit.CLI.exe uncook "<NPV_ROOT>\<Name>\<Name>.archive" ^
  -o "<WORK>\<Name>\raw" ^
  -s --uext png --mesh-export-type WithMaterials --mesh-export-lod-filter
```

- No `-or`, no `-gp` → everything in ONE tree `raw\` mirroring depot paths.
- Everything the entity import needs appears here: `*.ent.json`, `*.app.json`, `*.rig.json`,
  `*.mesh.json`, `*.mesh.glb`, `*.Material.json`, `*.png`.
- Expect long runtime on first run (textures/mlmasks); optionally cache per archive mtime.

### Step 2 - Find the entity and its appearance names

```python
ent_jsons = glob(r"<WORK>\<Name>\raw\**\*.ent.json", recursive=True)
# pick the ACM entity (expect exactly one per NPV; if several, ask the user)
appearances = [a["name"] for a in ent["Data"]["RootChunk"]["appearances"]]  # CName values = ACM look names
```

### Step 3 - Headless Blender import (Blender 5.x + Cyberpunk Blender IO Suite 2.5.0)

```bat
blender --background --python v_export.py -- "<WORK>\<Name>\raw" "<ent.json>" "<LookName>" "<OUT>"
```

`v_export.py` core:

```python
import bpy, addon_utils, sys
raw_root, ent_json, look_name, out_dir = sys.argv[sys.argv.index("--")+1:]
addon_utils.enable("i_scene_cp77_gltf", default_set=True)
bpy.context.scene.cp77_panel_props.with_materials = True

from i_scene_cp77_gltf.assetio.index import IndexPolicy
from i_scene_cp77_gltf.importers.entity.options import EntityImportRequest
from i_scene_cp77_gltf.importers.entity.execution import import_entity

result = import_entity(EntityImportRequest(
    with_materials=True,
    filepath=ent_json,
    appearances=(look_name,),        # one look per run; ("ALL",) also valid
    include_animations=False,
    depot_root=raw_root,
    index_policy=IndexPolicy.REFRESH,
))
assert not result.failures, result.failures

# select everything the import created for this look
import bpy.ops as _
def select_all():
    for ob in bpy.data.objects: ob.select_set(True)
bpy.ops.object.select_all(action='SELECT')
```

Per-look run (or one run with `appearances=("ALL",)` and export each `<ent>_<look>` collection
separately). Caveat from §3: **verify headless material baking on the first test**; if shader errors
appear, fall back to `with_materials=False` for FBX/OBJ and accept plain textures.

### Step 4 - Export models

```python
bpy.ops.export_scene.gltf(filepath=rf"{out_dir}\{look}.glb", use_selection=True,
                          export_materials='EXPORT', export_skins=True, export_morph=True)
bpy.ops.export_scene.fbx(filepath=rf"{out_dir}\{look}.fbx", use_selection=True,
                         object_types={'ARMATURE','MESH'}, add_leaf_bones=False,
                         export_apply=False, use_custom_props=False)
bpy.ops.wm.obj_export(filepath=rf"{out_dir}\{look}.obj", export_selected_objects=True,
                      export_materials=True, export_eval_mode='RENDER')
```

Naming: `<NPVName>\<LookName>\<LookName>.glb/.fbx/.obj` with `<LookName>_textures\`
inside the look folder. OBJ will be in rest pose unless the rig is posed
first (§4) - document that to users.

### Fallbacks / risk register

1. **Headless materials unverified** - test first; fallback `with_materials=False`.
2. **Protected NPVs** may not uncook (ACM "Protected" setting) - detect CLI failure and inform user.
3. **NPV archive naming/structure unpublished** - glob, don't assume; validate on the first real NPV.
4. **Game 2.5 requires WolvenKit ≥10.0** (currently beta); pin the CLI download
   (`WolvenKit.Console-<ver>.zip`) and check for .NET 10 runtime.
5. Blender must be **5.0+** for add-on 2.5.0 - refuse older Blenders with a clear message.
