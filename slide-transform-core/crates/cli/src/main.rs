//! `slide-transform` — native CLI over the shared transform core.
//!
//!   slide-transform probe <input> [--sha256]
//!   slide-transform convert <input> <output> [--profile auto|bf-classic|bf-ome|fl-ome]
//!                           [--encoding preserve|compact]
//!                           [--policy allow-edge|strict-lossless]
//!                           [--channel-json PATH] [--timeout SECONDS]
//!                           [--max-output-bytes N] [--min-free-bytes N]
//!                           [--overwrite]
//!
//! `--profile auto` keeps the historical mapping (KFB → bf-classic, KFBF →
//! fl-ome) because unattended callers (the Baidu import plugin worker) name
//! their outputs from it; the browser tool chooses bf-ome explicitly.
//! `--encoding` (U3) selects the tile-payload strategy independently of the
//! container: `preserve` (default, pre-U3 behaviour) or `compact`
//! (brightfield-only whole-slide re-encode at the locked compact-jpeg-v1
//! parameters; refused for fluorescence and with --policy strict-lossless).
//!
//! Both commands print a single JSON object to stdout; errors print
//! {"error":{"code","message"}} and exit 1. The converter writes
//! `<output>.part` first and renames only after success (no-clobber unless
//! --overwrite), mirroring the oracle's atomic promote.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use slide_transform_core::error::{CoreError, ErrorCode};
use slide_transform_core::io::{ByteSource, FileScratch, FileSink, FileSource, RandomAccessSink};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::kfb::MAGIC as KFB_MAGIC;
use slide_transform_core::kfbf::KFBF_MAGIC;
use slide_transform_core::plan::{
    InputIdentity, OutputProfile, PixelPolicy, ResourceLimits, TransformPlan,
};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        eprintln!("usage: slide-transform <probe|convert> ...");
        return ExitCode::from(2);
    }
    let result = match args[0].as_str() {
        "probe" => cmd_probe(&args[1..]),
        "convert" => cmd_convert(&args[1..]),
        "validate" => cmd_validate(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-kfb" => cmd_gen_kfb(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-kfbf" => cmd_gen_kfbf(&args[1..]),
        #[cfg(feature = "synth-gen")]
        "gen-svs" => cmd_gen_svs(&args[1..]),
        _ => Err(CoreError::validation(format!("未知子命令 {}", args[0]))),
    };
    match result {
        Ok(json) => {
            println!("{json}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            println!(
                "{{\"error\":{{\"code\":\"{}\",\"message\":{}}}}}",
                e.code.stable_code(),
                json_str(&e.message)
            );
            ExitCode::from(1)
        }
    }
}

// --------------------------------------------------------------------------- //
// tiny JSON writer
// --------------------------------------------------------------------------- //

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn jstr(k: &str, v: &str) -> String {
    format!("{}:{}", json_str(k), json_str(v))
}
fn jraw(k: &str, v: &str) -> String {
    format!("{}:{}", json_str(k), v)
}
fn ju(k: &str, v: u64) -> String {
    jraw(k, &v.to_string())
}
fn jf(k: &str, v: f64) -> String {
    if v.is_finite() {
        jraw(k, &format!("{v}"))
    } else {
        jraw(k, "null")
    }
}
fn jb(k: &str, v: bool) -> String {
    jraw(k, if v { "true" } else { "false" })
}
fn jarr(k: &str, items: &[String]) -> String {
    jraw(k, &format!("[{}]", items.join(",")))
}
fn obj(fields: &[String]) -> String {
    format!("{{{}}}", fields.join(","))
}

fn opt_u(v: Option<u64>) -> String {
    v.map(|x| x.to_string()).unwrap_or_else(|| "null".into())
}

// --------------------------------------------------------------------------- //
// shared helpers
// --------------------------------------------------------------------------- //

fn detect(src: &FileSource) -> Result<[u8; 8], CoreError> {
    let head = src.read_at(0, 8)?;
    let mut m = [0u8; 8];
    m.copy_from_slice(&head);
    Ok(m)
}

fn sha256_file(path: &Path) -> Result<String, CoreError> {
    let mut f = std::fs::File::open(path)
        .map_err(|e| CoreError::io(format!("打开失败: {e}")))?;
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).map_err(|e| CoreError::io(format!("读取失败: {e}")))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn scratch_under(path: &Path) -> PathBuf {
    path.parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| Path::new(".").to_path_buf())
}

// --------------------------------------------------------------------------- //
// probe
// --------------------------------------------------------------------------- //

/// TIFF/BigTIFF header check (classic 42 / BigTIFF 43, either byte order).
fn is_tiff_magic(m: &[u8; 8]) -> bool {
    let bo = &m[0..2];
    if bo != b"II" && bo != b"MM" {
        return false;
    }
    let v = if bo == b"II" {
        u16::from_le_bytes([m[2], m[3]])
    } else {
        u16::from_be_bytes([m[2], m[3]])
    };
    v == 42 || v == 43
}

/// Capability report for an SVS input (F1): recognised, convertible variant
/// or typed reason, levels, tile shape, codec, colorspace, MPP source.
fn svs_doc_json(doc: &slide_transform_core::svs::SvsDoc) -> String {
    let levels: Vec<String> = doc
        .levels
        .iter()
        .map(|lv| {
            obj(&[
                ju("level", lv.ifd_index as u64),
                ju("width", lv.width as u64),
                ju("height", lv.height as u64),
                ju("tile_w", lv.tile_w as u64),
                ju("tile_h", lv.tile_h as u64),
                ju("tiles_across", lv.tiles_across as u64),
                ju("tiles_down", lv.tiles_down as u64),
                jstr(
                    "color",
                    match lv.color {
                        slide_transform_core::svs::PayloadColor::Rgb => "rgb",
                        slide_transform_core::svs::PayloadColor::YCbCr => "ycbcr",
                    },
                ),
                jarr(
                    "sof_sampling",
                    &[
                        lv.sampling.0.to_string(),
                        lv.sampling.1.to_string(),
                        lv.sampling.2.to_string(),
                        lv.sampling.3.to_string(),
                        lv.sampling.4.to_string(),
                        lv.sampling.5.to_string(),
                    ],
                ),
                jb("jpeg_tables", lv.jpeg_tables.is_some()),
            ])
        })
        .collect();
    let assoc: Vec<String> = doc
        .associated
        .iter()
        .map(|a| obj(&[jstr("name", &a.name), ju("width", a.width as u64), ju("height", a.height as u64)]))
        .collect();
    obj(&[
        jstr("format", slide_transform_core::svs::SOURCE_FORMAT),
        jstr("adapter_version", slide_transform_core::svs::ADAPTER_VERSION),
        jstr("modality", "brightfield"),
        jstr(
            "tiff_kind",
            match doc.kind {
                slide_transform_core::tiff_read::TiffKind::Classic => "classic",
                slide_transform_core::tiff_read::TiffKind::BigTiff => "bigtiff",
            },
        ),
        ju("width", doc.levels[0].width as u64),
        ju("height", doc.levels[0].height as u64),
        jraw("mpp_x", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("mpp_y", &doc.mpp.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jraw("objective", &doc.appmag.map(|v| v.to_string()).unwrap_or_else(|| "null".into())),
        jstr(
            "mpp_source",
            if doc.mpp.is_some() { "aperio-description" } else { "unknown" },
        ),
        jstr(
            "objective_source",
            if doc.appmag.is_some() { "aperio-description-AppMag" } else { "unknown" },
        ),
        jarr("levels", &levels),
        jarr("associated", &assoc),
        jb("icc_profile", doc.icc.is_some()),
        jstr("codec", "jpeg-baseline-passthrough"),
    ])
}

fn cmd_probe(args: &[String]) -> Result<String, CoreError> {
    let mut path: Option<&String> = None;
    let mut want_hash = false;
    for a in args {
        match a.as_str() {
            "--sha256" => want_hash = true,
            _ => path = Some(a),
        }
    }
    let path = path.ok_or_else(|| CoreError::validation("probe 需要 <input>"))?;
    let src = FileSource::open(Path::new(path))?;
    let magic = detect(&src)?;
    let mut scratch = FileScratch::new(&scratch_under(Path::new(path)));
    let doc_json = if is_tiff_magic(&magic) {
        // F1: bounded TIFF walk + Aperio detection (typed rejection inside)
        svs_doc_json(&slide_transform_core::svs::probe_svs(&src)?)
    } else if magic == KFBF_MAGIC {
        let doc = slide_transform_core::kfbf::parse_kfbf(&src, &mut scratch)?;
        let channels: Vec<String> = doc
            .channels
            .iter()
            .map(|c| {
                obj(&[
                    ju("index", c.index as u64),
                    jstr("name", &c.name),
                    jarr(
                        "color_rgb",
                        &[
                            c.color_rgb.0.to_string(),
                            c.color_rgb.1.to_string(),
                            c.color_rgb.2.to_string(),
                        ],
                    ),
                    jf("exposure", c.exposure),
                    jstr("exposure_unit", "ms(assumed)"),
                    jf("gamma", c.gamma),
                ])
            })
            .collect();
        let levels: Vec<String> = doc
            .levels
            .iter()
            .map(|lv| {
                let present = doc.cells.present_count(lv.level).unwrap_or(0);
                obj(&[
                    ju("level", lv.level as u64),
                    ju("width", lv.width as u64),
                    ju("height", lv.height as u64),
                    ju("tiles_across", lv.tiles_across() as u64),
                    ju("tiles_down", lv.tiles_down() as u64),
                    ju("tiles_present", present as u64),
                ])
            })
            .collect();
        let assoc: Vec<String> = doc
            .associated
            .iter()
            .map(|a| {
                obj(&[
                    jstr("name", &a.name),
                    ju("width", a.width as u64),
                    ju("height", a.height as u64),
                ])
            })
            .collect();
        obj(&[
            jstr("format", "kfbf_kfbio_jpeg"),
            jstr("modality", "fluorescence"),
            ju("width", doc.header.width_px as u64),
            ju("height", doc.header.height_px as u64),
            jf("mpp", doc.header.mpp),
            jf("objective", doc.header.objective),
            jstr("scanner_id", &doc.header.scanner_id),
            ju("scanned_at", doc.header.scanned_at as u64),
            ju("channel_count", doc.header.channel_count as u64),
            jarr("channels", &channels),
            jarr("levels", &levels),
            jarr("associated", &assoc),
        ])
    } else {
        let doc = slide_transform_core::kfb::parse_kfb(&src, &mut scratch)?;
        let levels: Vec<String> = doc
            .levels
            .iter()
            .map(|lv| {
                obj(&[
                    ju("level", lv.level as u64),
                    ju("width", lv.width as u64),
                    ju("height", lv.height as u64),
                    ju("tiles_across", lv.tiles_across() as u64),
                    ju("tiles_down", lv.tiles_down() as u64),
                    ju("tiles_present", doc.grids.present_count(lv.level) as u64),
                ])
            })
            .collect();
        let assoc: Vec<String> = doc
            .associated
            .iter()
            .map(|a| {
                obj(&[
                    jstr("name", &a.name),
                    ju("width", a.width as u64),
                    ju("height", a.height as u64),
                ])
            })
            .collect();
        obj(&[
            jstr(
                "format",
                if doc.header.version != 1 { "kfb_kfbio_jpeg" } else { "kfb_bf_v1" },
            ),
            jstr("modality", "brightfield"),
            ju("width", doc.header.width_px as u64),
            ju("height", doc.header.height_px as u64),
            jf("mpp_x", doc.header.mpp_x),
            jf("mpp_y", doc.header.mpp_y),
            jf("objective", doc.header.objective),
            jstr("scanner_id", &doc.header.scanner_id),
            jarr("levels", &levels),
            jarr("associated", &assoc),
        ])
    };
    let hash = if want_hash {
        json_str(&sha256_file(Path::new(path))?)
    } else {
        "null".to_string()
    };
    // disk-precheck estimate (same shape as the wasm probe's)
    let estimate = if is_tiff_magic(&magic) {
        slide_transform_core::svs::estimate_svs(&slide_transform_core::svs::probe_svs(&src)?)
    } else if magic == KFBF_MAGIC {
        let doc = slide_transform_core::kfbf::parse_kfbf(&src, &mut scratch)?;
        slide_transform_core::estimate::estimate_fl(&doc, src.size())
    } else {
        let doc = slide_transform_core::kfb::parse_kfb(&src, &mut scratch)?;
        slide_transform_core::estimate::estimate_bf(&doc, src.size())?
    };
    let est_json = obj(&[
        ju("payload_bytes", estimate.payload_bytes),
        ju("tiles_present", estimate.tiles_present),
        ju("cells_total", estimate.cells_total),
        ju("cells_missing", estimate.cells_missing),
        ju("edge_tiles", estimate.edge_tiles),
        ju("ifds", estimate.ifds),
        ju("output_upper_bound_bytes", estimate.output_upper_bound_bytes),
        ju("compact_upper_bound_bytes", estimate.compact_upper_bound_bytes),
    ]);
    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        jstr("path", path),
        ju("size", src.size()),
        jraw("document", &doc_json),
        jraw("estimate", &est_json),
        jraw("sha256", &hash),
    ]))
}

// --------------------------------------------------------------------------- //
// validate (C2): streamed sha256 + structural BigTIFF walk over a finished
// output; mirrors the wasm finalizeValidate path for evidence parity.
// --------------------------------------------------------------------------- //

fn cmd_validate(args: &[String]) -> Result<String, CoreError> {
    let mut expect_ifd: Option<u32> = None;
    let mut path: Option<&String> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--expect-ifd" => {
                i += 1;
                expect_ifd = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--expect-ifd 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--expect-ifd 非数值"))?,
                );
            }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("validate 需要 <input>"))?;
    let src = FileSource::open(Path::new(path))?;
    let out = slide_transform_core::validate::validate_output(&src, src.size(), expect_ifd)?;
    Ok(obj(&[
        jb("ok", true),
        jstr("sha256", &out.sha256),
        ju("size", out.size),
        ju("ifd_count", out.ifd_count as u64),
        ju("main_ifds", out.main_ifds as u64),
        ju("sub_ifds", out.sub_ifds as u64),
        ju("tile_records", out.tile_records),
        jarr(
            "checks",
            &out.checks.iter().map(|c| json_str(c)).collect::<Vec<_>>(),
        ),
    ]))
}

// --------------------------------------------------------------------------- //
// convert
// --------------------------------------------------------------------------- //

fn cmd_convert(args: &[String]) -> Result<String, CoreError> {
    let mut positional: Vec<&String> = Vec::new();
    let mut profile = "auto".to_string();
    let mut policy = "allow-edge".to_string();
    let mut encoding = "preserve".to_string();
    let mut channel_json: Option<PathBuf> = None;
    let mut timeout: Option<f64> = None;
    let mut max_out: Option<u64> = None;
    let mut overwrite = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--profile" => {
                i += 1;
                profile = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--profile 缺值"))?
                    .clone();
            }
            "--encoding" => {
                i += 1;
                encoding = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--encoding 缺值"))?
                    .clone();
            }
            "--policy" => {
                i += 1;
                policy = args
                    .get(i)
                    .ok_or_else(|| CoreError::validation("--policy 缺值"))?
                    .clone();
            }
            "--channel-json" => {
                i += 1;
                channel_json = Some(PathBuf::from(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--channel-json 缺值"))?,
                ));
            }
            "--timeout" => {
                i += 1;
                timeout = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--timeout 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--timeout 非数值"))?,
                );
            }
            "--max-output-bytes" => {
                i += 1;
                max_out = Some(
                    args.get(i)
                        .ok_or_else(|| CoreError::validation("--max-output-bytes 缺值"))?
                        .parse()
                        .map_err(|_| CoreError::validation("--max-output-bytes 非数值"))?,
                );
            }
            "--overwrite" => overwrite = true,
            _ => positional.push(&args[i]),
        }
        i += 1;
    }
    if positional.len() != 2 {
        return Err(CoreError::validation("convert 需要 <input> <output>"));
    }
    let input = Path::new(positional[0]);
    let output = Path::new(positional[1]);
    if output.exists() && !overwrite {
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    let src = FileSource::open(input)?;
    let magic = detect(&src)?;

    let identity = InputIdentity {
        name: input
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        size: src.size(),
        sha256: None,
    };
    let limits = ResourceLimits {
        timeout_seconds: timeout.unwrap_or(600.0),
        max_output_bytes: max_out.unwrap_or(64 * 1024 * 1024 * 1024),
        min_free_bytes: 256 * 1024 * 1024,
    };
    // 磁盘剩余空间（宿主侧 statvfs；核心 crate 无 stat 依赖）
    {
        let dir = scratch_under(output);
        let free = free_bytes(&dir);
        if free < limits.min_free_bytes {
            return Err(CoreError::disk_low(format!(
                "目标盘剩余 {free} < {}",
                limits.min_free_bytes
            )));
        }
    }

    let out_profile = match profile.as_str() {
        // auto keeps the pre-profile default per input kind: KFB → classic
        // (unattended Baidu import consumes classic), SVS → classic as well
        "auto" if magic == KFBF_MAGIC => OutputProfile::OmeBigTiffSubifd,
        "auto" => OutputProfile::ClassicJpegBigTiff,
        id => OutputProfile::from_id(id)
            .ok_or_else(|| CoreError::validation(format!("未知 profile {profile}")))?,
    };
    let is_fl = !out_profile.is_brightfield();
    let is_svs = is_tiff_magic(&magic);
    let enc_profile = match encoding.as_str() {
        "preserve" => slide_transform_core::plan::EncodingProfile::PreserveSource,
        "compact" => slide_transform_core::plan::EncodingProfile::CompactJpegV1,
        _ => {
            return Err(CoreError::validation(format!(
                "未知 encoding {encoding}（preserve|compact）"
            )))
        }
    };
    if enc_profile == slide_transform_core::plan::EncodingProfile::CompactJpegV1 && is_fl {
        return Err(CoreError::variant(
            "compact-jpeg-v1 编码仅适用于明场；荧光不支持有损重编码",
        ));
    }
    if is_svs && is_fl {
        return Err(CoreError::variant("荧光 OME profile 不适用于明场 SVS 输入"));
    }
    let pixel_policy = match policy.as_str() {
        "allow-edge" => PixelPolicy::AllowEdgeReencode,
        "strict-lossless" => PixelPolicy::StrictLossless,
        _ => return Err(CoreError::validation(format!("未知 policy {policy}"))),
    };
    if pixel_policy == PixelPolicy::StrictLossless
        && enc_profile == slide_transform_core::plan::EncodingProfile::CompactJpegV1
    {
        return Err(CoreError::policy(
            "compact-jpeg-v1 与 strict-lossless 互斥：逐 tile 重编码必然有损",
        ));
    }

    // companion channel.json（可选；失败仅告警）
    let companion = match &channel_json {
        Some(p) => match std::fs::read(p) {
            Ok(data) => slide_transform_core::companion::parse_channel_json(&data)
                .map_err(|e| {
                    eprintln!("warning: channel.json 解析失败（忽略）：{}", e.message);
                })
                .ok(),
            Err(e) => {
                eprintln!("warning: channel.json 读取失败（忽略）：{e}");
                None
            }
        },
        None => None,
    };

    let part = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        scratch_under(output).join(format!("{name}.part"))
    };
    let mut scratch = FileScratch::new(&scratch_under(output));
    let mut sink = FileSink::create(&part)?;
    let null = NullProgress;
    let job = JobControl::new(&null).with_timeout(limits.timeout_seconds);

    let result = if is_fl {
        let plan = TransformPlan::fluorescence(identity)
            .with_policy(pixel_policy)
            .with_limits(limits);
        slide_transform_core::convert_fl::convert_kfbf_to_ome(
            &src,
            &mut sink,
            &mut scratch,
            &plan,
            &job,
            companion.as_ref(),
        )
    } else if is_svs {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_svs::convert_svs_to_bigtiff(
            &src, &mut sink, &mut scratch, &plan, &job,
        )
    } else {
        let mut plan = TransformPlan::brightfield(identity)
            .with_policy(pixel_policy)
            .with_limits(limits)
            .with_encoding(enc_profile);
        plan.profile = out_profile;
        slide_transform_core::convert_bf::convert_kfb_to_bigtiff(
            &src,
            &mut sink,
            &mut scratch,
            &plan,
            &job,
        )
    };
    let mut result = result?;
    sink.flush()?;
    drop(sink);

    // associated sidecars（<output>.associated/<name>.jpg）。KFB readers
    // record real payload offsets/lengths; the SVS adapter reports detected
    // label/macro/thumbnail without exporting them (main-image conversion,
    // not a source archive — payload length 0), so it gets no sidecars.
    let exportable = result
        .associated
        .iter()
        .filter(|a| a.source_length > 0)
        .count();
    let assoc_dir = {
        let name = output.file_name().map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        scratch_under(output).join(format!("{name}.associated"))
    };
    if exportable > 0 {
        std::fs::create_dir_all(&assoc_dir)
            .map_err(|e| CoreError::io(format!("创建关联图目录失败: {e}")))?;
        for a in &result.associated {
            if a.source_length == 0 {
                continue;
            }
            let bytes = src.read_at(a.source_offset, a.source_length as usize)?;
            let p = assoc_dir.join(format!("{}.jpg", a.name));
            std::fs::write(&p, &bytes)
                .map_err(|e| CoreError::io(format!("写关联图失败: {e}")))?;
        }
    }

    if output.exists() && !overwrite {
        let _ = std::fs::remove_file(&part);
        return Err(CoreError::validation(format!(
            "输出已存在：{}",
            output.display()
        )));
    }
    std::fs::rename(&part, output)
        .map_err(|e| CoreError::io(format!("转正失败: {e}")))?;
    result.output_sha256 = Some(sha256_file(output)?);

    let levels: Vec<String> = result
        .levels
        .iter()
        .map(|l| {
            obj(&[
                ju("level", l.level as u64),
                jraw("channel", &opt_u(l.channel.map(|c| c as u64))),
                ju("width", l.width as u64),
                ju("height", l.height as u64),
                ju("tiles_across", l.tiles_across as u64),
                ju("tiles_down", l.tiles_down as u64),
                ju("tiles_total", l.tiles_total),
                ju("tiles_raw_copied", l.tiles_raw_copied),
                ju("tiles_reencoded", l.tiles_reencoded),
                ju("cells_filled_black", l.cells_filled_black),
            ])
        })
        .collect();
    let edges: Vec<String> = result
        .edge_regions
        .iter()
        .map(|e| {
            obj(&[
                ju("level", e.level as u64),
                jraw("channel", &opt_u(e.channel.map(|c| c as u64))),
                ju("x", e.x as u64),
                ju("y", e.y as u64),
                ju("source_w", e.source_w as u64),
                ju("source_h", e.source_h as u64),
                ju("canvas_w", e.canvas_w as u64),
                ju("canvas_h", e.canvas_h as u64),
                jb("reused_qtables", e.reused_qtables),
            ])
        })
        .collect();
    let channels: Vec<String> = result
        .channels
        .iter()
        .map(|c| {
            let dw = c
                .display_window
                .map(|(lo, hi)| format!("[{lo},{hi}]"))
                .unwrap_or_else(|| "null".into());
            obj(&[
                ju("index", c.index as u64),
                jstr("name", &c.name),
                jarr(
                    "color_rgb",
                    &[
                        c.color_rgb.0.to_string(),
                        c.color_rgb.1.to_string(),
                        c.color_rgb.2.to_string(),
                    ],
                ),
                jf("exposure", c.exposure),
                jstr("exposure_unit", "ms(assumed)"),
                jf("gamma", c.gamma),
                jraw("display_window", &dw),
            ])
        })
        .collect();
    let warnings: Vec<String> =
        result.warnings.iter().map(|w| json_str(w)).collect();
    let assoc: Vec<String> = result
        .associated
        .iter()
        .map(|a| {
            obj(&[
                jstr("name", &a.name),
                ju("width", a.width as u64),
                ju("height", a.height as u64),
            ])
        })
        .collect();
    let ifds: Vec<String> = result
        .ifd_chain
        .iter()
        .map(|(lv, ch)| {
            format!(
                "[{},{}]",
                lv,
                ch.map(|c| c.to_string()).unwrap_or_else(|| "null".into())
            )
        })
        .collect();
    // U3 encoding summary: preserve reports the id and no lossy flag; compact
    // reports the locked parameters (and never claims losslessness).
    let (lossy_flag, lossy_obj) = match &result.lossy_reencode {
        Some(l) => (
            true,
            obj(&[
                jstr("profile", l.profile),
                jstr("params_fingerprint", &l.params_fingerprint),
                ju("quality", l.quality as u64),
                jstr("sampling", l.sampling),
                jstr("huffman", l.huffman),
                ju("tiles_reencoded", l.tiles_reencoded),
                ju("tiles_padded", l.tiles_padded),
            ]),
        ),
        None => (false, "null".to_string()),
    };

    Ok(obj(&[
        jstr("tool", "slide-transform"),
        jstr("core_version", slide_transform_core::CORE_VERSION),
        ju("plan_version", result.plan_version as u64),
        jraw("source_format", &result
            .source_format
            .map(json_str)
            .unwrap_or_else(|| "null".into())),
        jraw("adapter_version", &result
            .adapter_version
            .map(json_str)
            .unwrap_or_else(|| "null".into())),
        jstr("output_profile", out_profile.id()),
        jstr("encoding", enc_profile.id()),
        jb("lossy_reencode", lossy_flag),
        jraw("lossy_reencode_params", &lossy_obj),
        jstr("format", result.format),
        jstr("output", &output.display().to_string()),
        ju("output_bytes", result.output_bytes),
        jraw(
            "output_sha256",
            &json_str(result.output_sha256.as_deref().unwrap_or("")),
        ),
        ju("width", result.width as u64),
        ju("height", result.height as u64),
        jarr("levels", &levels),
        jarr("edge_regions", &edges),
        jarr("channels", &channels),
        jarr("warnings", &warnings),
        jarr("associated", &assoc),
        jarr("ifd_chain", &ifds),
        ju("tiles_raw_copied", result.count_raw_copied()),
        ju("tiles_reencoded", result.count_reencoded()),
        jf("elapsed_seconds", result.elapsed_seconds),
        jraw(
            "validation",
            &obj(&[
                ju("ifd_count", result.validation.ifd_count as u64),
                ju("tile_records_emitted", result.validation.tile_records_emitted),
                ju("output_bytes", result.validation.output_bytes),
            ]),
        ),
    ]))
}

#[cfg(unix)]
fn free_bytes(dir: &Path) -> u64 {
    // statvfs(3) via a tiny FFI shim（无 libc crate 依赖）
    #[repr(C)]
    struct StatVfs {
        f_bsize: i64,
        f_frsize: i64,
        f_blocks: u64,
        f_bfree: u64,
        f_bavail: u64,
        f_files: u64,
        f_ffree: u64,
        f_favail: u64,
        f_sid: [i32; 2],
        f_namemax: i64,
    }
    extern "C" {
        fn statvfs(path: *const std::os::raw::c_char, buf: *mut StatVfs) -> i32;
    }
    use std::os::unix::ffi::OsStrExt;
    let mut path = dir.as_os_str().as_bytes().to_vec();
    path.push(0);
    let mut st;
    let rc = unsafe {
        st = std::mem::MaybeUninit::<StatVfs>::zeroed().assume_init();
        statvfs(path.as_ptr() as *const _, &mut st)
    };
    if rc != 0 {
        return u64::MAX;
    }
    st.f_bavail.saturating_mul(st.f_frsize.max(0) as u64)
}

#[cfg(not(unix))]
fn free_bytes(_dir: &Path) -> u64 {
    u64::MAX
}

// --------------------------------------------------------------------------- //
// synthetic generators（>4GiB 证明与测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_kfb(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 580u32;
    let mut height = 300u32;
    let mut sampling = "422".to_string();
    let mut quality = 90u8;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--sampling" => { i += 1; sampling = args.get(i).cloned().ok_or_else(|| CoreError::validation("--sampling"))?; }
            "--quality" => { i += 1; quality = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--quality"))?; }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-kfb 需要 <out>"))?;
    let p = slide_transform_core::synth_gen::GenParams {
        width,
        height,
        quality,
        sampling: match sampling.as_str() {
            "444" => "444",
            "420" => "420",
            _ => "422",
        },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let mut scratch = FileScratch::new(&scratch_under(Path::new(path)));
    let _ = &mut scratch;
    let n = slide_transform_core::synth_gen::build_synthetic_kfb(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

#[cfg(feature = "synth-gen")]
fn cmd_gen_kfbf(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 600u32;
    let mut height = 400u32;
    let mut channels = 2usize;
    let mut noisy = false;
    let mut cache = true;
    let mut trim = 44u32;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--channels" => { i += 1; channels = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--channels"))?; }
            "--noisy" => noisy = true,
            "--no-cache" => cache = false,
            "--trim-bottom" => { i += 1; trim = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--trim-bottom"))?; }
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-kfbf 需要 <out>"))?;
    let names = ["DAPI", "520", "480", "570", "620", "690", "780", "CY5", "CY7", "FITC", "TRITC", "DAPI2", "CH12", "CH13", "CH14", "CH15"];
    let colors = [(0,0,229),(0,255,0),(128,255,255),(0,229,0),(229,0,0),(160,0,120),(80,0,160),(0,160,160),
                  (160,80,0),(0,120,255),(255,80,0),(90,90,90),(30,30,30),(200,200,0),(0,200,200),(200,0,200)];
    let channels_cfg: Vec<(String, (u32,u32,u32), f64, f64)> = (0..channels)
        .map(|c| (names[c].to_string(), colors[c], 1.0 + c as f64, 1.0))
        .collect();
    let p = slide_transform_core::kfbf::fixture::KfbfGenParams {
        width,
        height,
        channels: channels_cfg,
        noisy,
        cache_payloads: cache,
        trim_level0_bottom: trim,
        missing_cells: if width >= 512 { vec![(0, 1)] } else { vec![] },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::kfbf::fixture::build_synthetic_kfbf(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}

// --------------------------------------------------------------------------- //
// synthetic SVS generator（F1 测试数据；无患者数据）
// --------------------------------------------------------------------------- //

#[cfg(feature = "synth-gen")]
fn cmd_gen_svs(args: &[String]) -> Result<String, CoreError> {
    let mut path = None;
    let mut width = 580u32;
    let mut height = 300u32;
    let mut tile = 256u32;
    let mut bigtiff = false;
    let mut big_endian = false;
    let mut downsample = 4u32;
    let mut associated = false;
    let mut crop_tail = false;
    let mut color = "rgb".to_string();
    let mut mpp: Option<f64> = Some(0.4990);
    let mut appmag: Option<f64> = Some(20.0);
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--width" => { i += 1; width = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--width"))?; }
            "--height" => { i += 1; height = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--height"))?; }
            "--tile" => { i += 1; tile = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--tile"))?; }
            "--bigtiff" => bigtiff = true,
            "--big-endian" => big_endian = true,
            "--downsample" => { i += 1; downsample = args.get(i).and_then(|v| v.parse().ok()).ok_or_else(|| CoreError::validation("--downsample"))?; }
            "--associated" => associated = true,
            "--crop-tail" => crop_tail = true,
            "--color" => { i += 1; color = args.get(i).cloned().ok_or_else(|| CoreError::validation("--color"))?; }
            "--no-mpp" => mpp = None,
            "--no-appmag" => appmag = None,
            _ => path = Some(&args[i]),
        }
        i += 1;
    }
    let path = path.ok_or_else(|| CoreError::validation("gen-svs 需要 <out>"))?;
    let p = slide_transform_core::svs_fixture::SvsGenParams {
        width,
        height,
        tile,
        bigtiff,
        big_endian,
        downsample,
        include_associated: associated,
        mpp,
        appmag,
        crop_tail_tiles: crop_tail,
        color: match color.as_str() {
            "ycbcr" => slide_transform_core::svs_fixture::FixtureColor::YCbCr,
            _ => slide_transform_core::svs_fixture::FixtureColor::Rgb,
        },
        ..Default::default()
    };
    let mut sink = FileSink::create(Path::new(path))?;
    let n = slide_transform_core::svs_fixture::build_synthetic_svs(&mut sink, &p)?;
    sink.flush()?;
    Ok(obj(&[jstr("path", path), ju("bytes", n)]))
}
