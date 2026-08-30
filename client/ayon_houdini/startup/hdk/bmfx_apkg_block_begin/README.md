# BMFX APKG native block boundaries

These HDK LOPs provide the APKG USD dependency loader and both native Solaris
block boundaries. They replace the former hidden loader and the stock Edit
Context Options End, so the parent graph contains only APKG Begin, department
nodes, APKG End, and the publisher.

Build for each supported Houdini ABI:

```bash
cmake -S . -B build \
  -DCMAKE_PREFIX_PATH="$HFS/toolkit/cmake" \
  -DCMAKE_LIBRARY_OUTPUT_DIRECTORY="../../dso"
cmake --build build --parallel
```

The resulting DSO is ABI-specific and must be produced in CI for every
supported Houdini build/platform combination.
