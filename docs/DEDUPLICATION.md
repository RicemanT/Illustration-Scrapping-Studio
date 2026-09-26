# Deduplication design

## Safety model

- SHA256 and MD5 byte-for-byte matches reuse the existing image automatically.
- Perceptual matches only create review candidates. They never delete or merge files automatically.
- A merge requires an explicit confirmation in the UI. It moves collection memberships, source records, tags, and captions to the kept image before removing the losing file.
- Every confirmed merge writes a `duplicate_resolution` audit record.

## Visual fingerprints

Each image caches four format-independent signals in SQLite:

1. 64-bit pHash for broad frequency/layout similarity.
2. 64-bit dHash for horizontal edge/gradient similarity.
3. 256-bit gradient hash for a stricter structural comparison.
4. A 4 × 4 RGB layout signature to reduce luminance-only false positives.

The displayed visual similarity combines pHash (24%), dHash (18%), 256-bit gradient hash (23%), color layout (25%), and aspect ratio (10%). The strict, balanced, and broad profiles change the candidate radii and minimum aggregate score. A scan launched from one collection fingerprints the library and compares that collection's images against every stored image.

## Czkawka inspiration

Czkawka's Similar Images implementation uses luminance-based perceptual hashes, configurable Mean/Gradient/DoubleGradient/Blockhash variants and hash sizes, Hamming thresholds, cached fingerprints, and a BK-tree for radius lookup. Its documentation also calls out two important failure modes: large uniform areas can collide, and grayscale/color versions can match because the perceptual hashes ignore color.

This app follows that architecture with cached multi-resolution gradient fingerprints and an in-memory Hamming BK-tree, while adding a color-sensitive second stage. The initial release leaves Czkawka-style mirror/rotation invariance for Phase 3 because it multiplies scan work and requires its own review controls.

Reference inspected: [Czkawka Similar Images source](https://github.com/qarmin/czkawka/tree/master/czkawka_core/src/tools/similar_images).

## XnView inspiration

XnView's user-facing percentage-based similar-picture workflow is useful, but its exact comparison formula is proprietary and is not reproduced here. “XnView-style color and layout” in the UI means this app's own transparent aggregate visual score and side-by-side review workflow.

## Review choices

- **Keep A and merge / Keep B and merge:** keep the chosen file and consolidate metadata.
- **Keep highest quality:** choose by pixel count, then format preference, then file size.
- **Keep both:** resolve the candidate while retaining both images.
- **Not the same image:** suppress this pair on later scans.
