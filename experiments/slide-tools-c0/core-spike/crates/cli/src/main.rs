//! kfb2tiff — native CLI over the core spike: brightfield KFB → tiled JPEG
//! BigTIFF pyramid, file-backed ByteSource/Sink.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use slide_transform_core_spike::convert::ConvertOptions;
use slide_transform_core_spike::ByteSource;
use slide_transform_core_spike::io::{FileScratch, FileSink, FileSource};
use slide_transform_core_spike::kfb::parse_kfb;
#[cfg(feature = "edge-reencode")]
use slide_transform_core_spike::synth_gen::{build_synthetic_kfb, GenParams};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(msg) => {
            println!("{msg}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("error[{}]: {}", e.code.stable_code(), e.message);
            ExitCode::FAILURE
        }
    }
}

fn usage() -> String {
    "usage:
  kfb2tiff convert <in.kfb> <out.tif> [--overwrite] [--max-output-bytes N]
  kfb2tiff probe <in.kfb>
  kfb2tiff gen-synth <out.kfb> --width N --height N [--quality Q] [--sampling 420|422|444] [--seed N]"
        .to_string()
}

fn run(args: &[String]) -> Result<String, slide_transform_core_spike::CoreError> {
    match args.first().map(String::as_str) {
        Some("convert") => cmd_convert(&args[1..]),
        Some("probe") => cmd_probe(&args[1..]),
        #[cfg(feature = "edge-reencode")]
        Some("gen-synth") => cmd_gen_synth(&args[1..]),
        _ => Err(slide_transform_core_spike::CoreError::io(usage())),
    }
}

fn cmd_convert(args: &[String]) -> Result<String, slide_transform_core_spike::CoreError> {
    let mut in_path: Option<&str> = None;
    let mut out_path: Option<&str> = None;
    let mut overwrite = false;
    let mut max_output_bytes = ConvertOptions::default().max_output_bytes;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--overwrite" => overwrite = true,
            "--max-output-bytes" => {
                i += 1;
                max_output_bytes = args
                    .get(i)
                    .and_then(|v| v.parse().ok())
                    .ok_or_else(|| slide_transform_core_spike::CoreError::io("--max-output-bytes N"))?;
            }
            p if in_path.is_none() => in_path = Some(p),
            p if out_path.is_none() => out_path = Some(p),
            p => {
                return Err(slide_transform_core_spike::CoreError::io(format!("多余参数 {p}")));
            }
        }
        i += 1;
    }
    let (in_path, out_path) = (
        PathBuf::from(in_path.ok_or_else(|| slide_transform_core_spike::CoreError::io(usage()))?),
        PathBuf::from(out_path.ok_or_else(|| slide_transform_core_spike::CoreError::io(usage()))?),
    );
    if out_path.exists() && !overwrite {
        return Err(slide_transform_core_spike::CoreError::validation(format!(
            "输出已存在：{}",
            out_path.display()
        )));
    }

    let part_path = out_path.with_file_name(format!(
        "{}.part",
        out_path.file_name().unwrap().to_string_lossy()
    ));
    let src = FileSource::open(&in_path)?;
    let mut scratch = FileScratch::new(out_path.parent().unwrap_or(Path::new(".")));
    {
        let doc = parse_kfb(&src, &mut scratch)?;
        let mut sink = FileSink::create(&part_path)?;
        let stats = slide_transform_core_spike::convert::convert_document(
            &doc,
            &src,
            &mut sink,
            &mut scratch,
            &ConvertOptions { max_output_bytes },
        )?;
        print_stats(&stats, &in_path, &out_path);

        // associated: <out>.associated/<name>.jpg（与 Python oracle 相同）
        if !doc.associated.is_empty() {
            let assoc_dir = out_path.with_file_name(format!(
                "{}.associated",
                out_path.file_name().unwrap().to_string_lossy()
            ));
            std::fs::create_dir_all(&assoc_dir)
                .map_err(slide_transform_core_spike::CoreError::from)?;
            for item in &doc.associated {
                let bytes = src.read_at(item.payload_offset, item.payload_length as usize)?;
                let p = assoc_dir.join(format!("{}.jpg", item.name));
                std::fs::write(&p, &bytes)
                    .map_err(slide_transform_core_spike::CoreError::from)?;
            }
        }
    } // FileScratch drop 清理 scratch 文件
    std::fs::rename(&part_path, &out_path)
        .map_err(|e| slide_transform_core_spike::CoreError::io(format!("rename 失败: {e}")))?;
    Ok(format!("done: {}", out_path.display()))
}

fn print_stats(
    stats: &slide_transform_core_spike::ConvertStats,
    in_path: &Path,
    out_path: &Path,
) {
    println!(
        "levels={} output_bytes={} warnings={:?}",
        stats.levels.len(),
        stats.output_bytes,
        stats.warnings
    );
    for lv in &stats.levels {
        println!(
            "level {} {}x{} grid {}x{} tiles={} raw={} reencoded={}",
            lv.level,
            lv.width,
            lv.height,
            lv.tiles_across,
            lv.tiles_down,
            lv.tiles_total,
            lv.tiles_raw_copied,
            lv.tiles_reencoded
        );
    }
    println!(
        "source_format={} scanner={:?} mpp=({}, {}) objective={}",
        stats.source_format, stats.scanner_id, stats.mpp_x, stats.mpp_y, stats.objective
    );
    let _ = (in_path, out_path);
}

fn cmd_probe(args: &[String]) -> Result<String, slide_transform_core_spike::CoreError> {
    let in_path = args
        .first()
        .ok_or_else(|| slide_transform_core_spike::CoreError::io(usage()))?;
    let src = FileSource::open(Path::new(in_path))?;
    let mut scratch = FileScratch::new(std::env::temp_dir().as_path());
    let doc = parse_kfb(&src, &mut scratch)?;
    println!(
        "version={} {}x{} levels={} tiles={} associated={} scanner={:?} mpp=({}, {}) objective={}",
        doc.header.version,
        doc.header.width_px,
        doc.header.height_px,
        doc.header.level_count,
        doc.header.tile_count,
        doc.header.associated_count,
        doc.header.scanner_id,
        doc.header.mpp_x,
        doc.header.mpp_y,
        doc.header.objective
    );
    for lv in &doc.levels {
        println!(
            "level {} {}x{} grid={}x{} present={}",
            lv.level,
            lv.width,
            lv.height,
            lv.tiles_across(),
            lv.tiles_down(),
            doc.grids.present_count(lv.level)
        );
    }
    Ok("probe ok".into())
}

#[cfg(feature = "edge-reencode")]
fn cmd_gen_synth(args: &[String]) -> Result<String, slide_transform_core_spike::CoreError> {
    let mut out_path: Option<PathBuf> = None;
    let mut p = GenParams::default();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        match a.as_str() {
            "--width" => {
                i += 1;
                p.width = args.get(i).and_then(|v| v.parse().ok()).unwrap_or(p.width);
            }
            "--height" => {
                i += 1;
                p.height = args.get(i).and_then(|v| v.parse().ok()).unwrap_or(p.height);
            }
            "--quality" => {
                i += 1;
                p.quality = args.get(i).and_then(|v| v.parse().ok()).unwrap_or(p.quality);
            }
            "--sampling" => {
                i += 1;
                let s = args.get(i).cloned().unwrap_or_default();
                p.sampling = match s.as_str() {
                    "420" => "420",
                    "444" => "444",
                    _ => "422",
                };
            }
            "--seed" => {
                i += 1;
                p.seed = args.get(i).and_then(|v| v.parse().ok()).unwrap_or(p.seed);
            }
            other => out_path = Some(PathBuf::from(other)),
        }
        i += 1;
    }
    let out_path =
        out_path.ok_or_else(|| slide_transform_core_spike::CoreError::io(usage()))?;
    let mut sink = FileSink::create(&out_path)?;
    let size = build_synthetic_kfb(&mut sink, &p)?;
    Ok(format!("gen-synth: {} bytes -> {}", size, out_path.display()))
}
