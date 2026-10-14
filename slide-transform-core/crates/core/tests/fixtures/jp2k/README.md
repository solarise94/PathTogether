# JPEG 2000 test fixtures (F2)

Committed codestreams for the Aperio SVS JPEG 2000 adapter tests
(`crates/core/tests/svs_jp2k.rs`). Every file's provenance and licence:

| file | what it is | licence |
|---|---|---|
| `synth-33003-full.j2k` | 256×256, 3 comp, **MCT=0, XRsiz (1,1)(2,1)(2,1)** (4:2:2), 5/3 reversible, LRCP, 5 decomposition levels, 1 layer, 64×64 code-blocks — the verified Aperio 33003 codestream shape over SYNTHETIC pixels (deterministic gradients + patches, no patient data). Encoded with OpenJPEG 2.5.4 (`opj_image_create` with per-component dx/dy). | repo's own (synthetic) |
| `synth-33003-full.expected.rgb` | interleaved RGB8 reference: the chroma-replicated YCbCr→RGB reconstruction computed with the OpenSlide `make-tables.c` fixed-point math (x/2 chroma indexing) | repo's own (synthetic) |
| `synth-33003-cropped.j2k` + `.expected.rgb` | the same shape at 200×256 — the codestream-smaller-than-tile-rect edge case | repo's own (synthetic) |
| `cc0-33005-l0.j2k` / `cc0-33005-l1.j2k` | one 240×240 tile codestream each from level 0 / level 1 of OpenSlide's **CMU-1-JP2K-33005.svs** (MCT=1, unsubsampled — the verified Aperio 33005 shape), cut at TIFF tile-offset boundaries | **CC0-1.0** (per OpenSlide `Aperio-index.yaml`) |
| `cc0-33005-l*.osr.rgb` | the same tile regions decoded with OpenSlide 4.0.1 `read_region` (ground-truth RGB reference) | derived from the CC0 slide |

The JP2K-33003-* OpenSlide slides are "distributable", NOT CC0 — no bytes cut
from them are committed; they stay local-only validation data
(`.testdata/openslide/`, verified against `Aperio-index.yaml` SHA-256).

Generator (run-dir tooling, not committed): ctypes driver over OpenJPEG 2.5.4
`opj_image_create(dx=(1,2,2))` + `opj_encode`, see the F2 evidence doc under
`docs/slide-tools/`.
