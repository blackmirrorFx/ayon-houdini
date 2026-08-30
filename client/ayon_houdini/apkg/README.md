# BMFX APKG contribution workflow

APKG is a versioned department contribution, not a zip file and not a shot
workfile. AYON entities and representation IDs are authoritative; the JSON
manifest is the portable contract and the USD representation is its entrypoint.

## Houdini authoring

1. Create **BMFX APKG Block** in `/stage`. One action creates its linked orange
   Begin and End pair using Solaris' native Context Options Block hull, like a
   Houdini For-Each tool. No selectable Network Box/backdrop is created.
2. Open the modern **Shot Builder** from Begin. Task is always taken from the
   AYON context that launched Houdini and cannot be edited.
3. Use **Build Approved** for the policy-selected upstream set, or **Build
   Selected** to choose exact independent APKG products and versions.
4. Insert the department work between Begin and End.
5. End exposes **Use Split** only for FX, CFX and Crowd. With Use Split
   disabled, `main` remains the internal composition slot but the public
   product is simply `APKG_<task>`. Enable splitting for independent outputs
   such as `smoke`, `dust`, `debris` or `destruction`.
6. The block creates the dedicated **BMFX APKG Publisher** LOP HDA directly
   below End, outside the hull. Select End or this publisher and create **BMFX
   Department APKG** in AYON Publisher. AYON adopts the existing HDA as its
   publish instance; no `/out` node is created. The contribution USD, APKG
   manifest and discovered external resources are published together.

The Begin selection is stored in the HIP with exact package/version IDs and
resolved entrypoint paths. Cooking and farm rendering never query AYON.

The stable unsplit AYON product is `APKG_<task>`. For FX, CFX and Crowd, an
enabled split becomes `APKG_<task>split_<name>`, where `<name>` is the End
node's Split value. CFX and Crowd insert their classification before that
name. Published files append the AYON version as `_v###`.

## Composition rules

- Automatic selection is per product: `approved`, then `available`, then
  `pending`; the highest version inside the first matching tier wins.
- Required dependencies are resolved transitively and ordered before their
  consumers. Missing packages, cycles and overlapping exported prim ownership
  fail validation.
- **Required Dependency** (`inherit`) packages are composed into the shot and
  their exact versions become required outgoing AYON input links.
- **Context Only** (`context`) packages are visible to the artist but excluded from outgoing AYON
  input links. Their entire dependency closure is also context-only.
- A publish fails if a context-only entrypoint leaks into the exported USD.
- **Clear All** clears only the selected package lock on that Begin node. It
  never deletes published files or artist-authored nodes.

## Timing ownership

APKG intentionally contains no frame range, handles or FPS. Those values remain
owned by the AYON shot/folder context, preventing stale timing from being copied
through department packages.

## Browser behavior

BMFX Browser classifies APKG separately from generic USD. Loading an APKG in
Houdini composes it into the selected (or sole) Begin node and resolves its
exact dependency closure. **Required Dependency** creates publish-through
dependencies; **Context Only** never does. If multiple Begin nodes exist, the
artist must select the target explicitly.

## Rebuilding the assets

The checked-in HDAs are generated from known-good local
templates without a Houdini license:

```bash
python startup/otls/build_bmfx_apkg_tools.py
```

The resulting assets live in `startup/otls`, which is already on the add-on's
Houdini path.
