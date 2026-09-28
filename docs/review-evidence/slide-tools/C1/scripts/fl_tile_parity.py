import sys, subprocess, hashlib, json, tifffile
from pathlib import Path
S = Path("/home/solarise/ZCodeProject/histopilot-suite/切片文件夹")
CLI = "slide-transform-core/target/release/slide-transform"
R = Path(sys.argv[1])
def pages(t):
    out = []
    for s in t.series:
        for li, lvl in enumerate(s.levels):
            for pi, p in enumerate(lvl.pages):
                out.append(((s.name, li, pi), p))
    return out
def tiles(fh, p):
    for o, n in zip(p.dataoffsets, p.databytecounts):
        fh.seek(o); yield fh.read(n)
res = {}
for i, src in enumerate(sorted(S.glob("ref/*.kfbf"))[:4]):
    a = "KFBF-" + "ABCD"[i]
    mine = R / f"{a}-mine.tif"
    subprocess.run([CLI, "convert", str(src), str(mine), "--overwrite"], check=True, capture_output=True)
    om = Path(f".gate-tmp/slide-tools-c0/inventory/oracle/{a}.ome.tif")
    with tifffile.TiffFile(mine) as tm, tifffile.TiffFile(om) as to:
        pm, po = pages(tm), pages(to)
        eq = diff = 0; diffs = []
        struct = [(k[1:], p.shape, p.dtype.str, p.tile, p.compression) for k, p in pm] == [(k[1:], p.shape, p.dtype.str, p.tile, p.compression) for k, p in po]
        for (km, a_), (ko, b_) in zip(pm, po):
            for j, (x, y) in enumerate(zip(tiles(tm.filehandle, a_), tiles(to.filehandle, b_))):
                if x == y: eq += 1
                else: diff += 1; diffs.append((km[1], km[2], j)) if len(diffs) < 5 else None
        res[a] = dict(pages_mine=len(pm), pages_oracle=len(po), struct_equal=struct, tiles_equal=eq, tiles_differ=diff, first_diffs=diffs,
                      axes=tm.series[0].axes, shape=list(tm.series[0].shape), axes_o=to.series[0].axes, shape_o=list(to.series[0].shape))
    print(a, json.dumps(res[a]), flush=True)
    mine.unlink()
