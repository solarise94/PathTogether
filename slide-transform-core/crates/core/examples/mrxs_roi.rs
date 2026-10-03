// compose an ROI via the adapter and dump raw RGB for python comparison
use slide_transform_core::bundle::{BundleFs, DirBundle};
use slide_transform_core::convert_mirax::compose_region;
use slide_transform_core::mirax::probe_mirax;
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let dir = std::path::Path::new(&args[1]);
    let stem = &args[2];
    let level: usize = args[3].parse().unwrap();
    let (x, y, w, h): (u32, u32, u32, u32) = (
        args[4].parse().unwrap(), args[5].parse().unwrap(),
        args[6].parse().unwrap(), args[7].parse().unwrap(),
    );
    let out = &args[8];
    let fs = DirBundle::open(dir, stem).unwrap();
    let doc = probe_mirax(&fs, stem).unwrap();
    eprintln!("dims L{} = {}x{}", level, doc.levels[level].width, doc.levels[level].height);
    let rgb = compose_region(&fs, &doc, level, x, y, w, h).unwrap();
    let n = rgb.len();
    std::fs::write(out, rgb).unwrap();
    eprintln!("wrote {} bytes", n);
}
