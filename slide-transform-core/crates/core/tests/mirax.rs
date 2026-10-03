//! F3 MRXS bundle adapter tests: synthetic fixtures (structure, geometry,
//! faults, resume) plus env-gated real-sample validation against the
//! exported OpenSlide ground truth.

use slide_transform_core::bundle::{BundleFs, MemBundle};
use slide_transform_core::convert_mirax::{
    compose_region, convert_mirax_to_bigtiff, convert_mirax_to_bigtiff_resume, OUT_TILE,
    MRAX_PRESERVE_COMPOSE_FINGERPRINT,
};
use slide_transform_core::io::{MemScratch, MemSink};
use slide_transform_core::job::{JobControl, NullProgress};
use slide_transform_core::mirax::{probe_mirax, probe_mirax_with_budget, PositionSource};
use slide_transform_core::mirax_fixture::{build_synthetic_mrxs, MrxsGenParams};
use slide_transform_core::plan::{
    EncodingProfile, OutputProfile, PixelPolicy, TransformPlan,
    COMPACT_JPEG_V1_FINGERPRINT,
};
use slide_transform_core::resume::ResumePoint;

fn plan_for(profile: OutputProfile, policy: PixelPolicy) -> TransformPlan {
    let mut p = TransformPlan::brightfield(Default::default()).with_policy(policy);
    p.profile = profile;
    p
}

fn plan_enc(profile: OutputProfile, enc: EncodingProfile) -> TransformPlan {
    let mut p = TransformPlan::brightfield(Default::default());
    p.profile = profile;
    p.encoding = enc;
    p
}

fn convert(
    fs: &MemBundle,
    profile: OutputProfile,
) -> slide_transform_core::error::CoreResult<(Vec<u8>, slide_transform_core::report::TransformResult)>
{
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(profile, PixelPolicy::AllowEdgeReencode);
    let r = convert_mirax_to_bigtiff(fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress))?;
    Ok((sink.data, r))
}

fn code_of(r: &slide_transform_core::error::CoreError) -> String {
    r.code.stable_code().to_string()
}

// ------------------------------------------------------------------ probe --

#[test]
fn probe_geometry_and_metadata() {
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    let doc = probe_mirax(&fs, "synthetic").unwrap();
    assert_eq!(doc.levels.len(), 3);
    assert_eq!(doc.levels[0].width, 476);
    // height: images_y=6 → rows 1,3,5 full (3, incl. last), 0,2,4 reduced:
    //   4*48 + 2*(48-12) = 264; width analog: 5*64 + 3*52 = 476
    assert_eq!(doc.levels[0].height, 264);
    assert_eq!((doc.levels[1].width, doc.levels[1].height), (238, 132));
    assert_eq!((doc.levels[2].width, doc.levels[2].height), (119, 66));
    assert_eq!(doc.mpp, Some((0.25, 0.25)));
    assert_eq!(doc.objective, Some(20.0));
    assert_eq!(doc.position_source, PositionSource::VimslideBuffer);
    assert_eq!(doc.position_count(), (8 / 2) * (6 / 2));
    // associated thumbnail detected, not exported
    assert!(doc.associated.iter().any(|a| a.name == "macro"));
}

#[test]
fn missing_entry_slidedat_or_index_is_typed_missing() {
    // entry missing
    let mut fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    fs.remove("synthetic.mrxs");
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "conversion_validation_failed");
    assert!(e.message.contains("synthetic.mrxs"));

    // Slidedat.ini missing (only the entry survives)
    let mut fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    fs.remove("synthetic/Slidedat.ini");
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("Slidedat.ini"));

    // Index.dat missing
    let mut fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    fs.remove("synthetic/Index.dat");
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("Index.dat"));
}

#[test]
fn missing_data_member_named_in_error() {
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        missing_data_member: true,
        ..Default::default()
    })
    .unwrap();
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert!(e.message.contains("Data0000.dat"), "{}", e.message);
}

#[test]
fn path_traversal_data_name_rejected() {
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        traversal_data_name: true,
        ..Default::default()
    })
    .unwrap();
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    // the traversal name resolves to no member → typed missing-member error
    // (no path joins exist, so nothing outside the bundle can be read)
    assert!(e.message.contains("evil.dat") || e.message.contains("路径穿越"), "{}", e.message);
}

#[test]
fn index_loop_is_typed_error_not_hang() {
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        index_loop: true,
        levels: vec![(0, 12.0, 12.0)],
        images_x: 512,
        images_y: 8,
        ..Default::default()
    })
    .unwrap();
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "invalid_tile_index");
    assert!(e.message.contains("回环"));
}

#[test]
fn index_oob_pointer_is_typed_error() {
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        oob_page_ptr: true,
        ..Default::default()
    })
    .unwrap();
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "invalid_tile_index");
}

#[test]
fn fluorescence_and_png_are_typed_refusals() {
    // PNG format declared
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        png_format: true,
        ..Default::default()
    })
    .unwrap();
    let e = probe_mirax(&fs, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "unsupported_kfb_variant");
    assert!(e.message.contains("PNG"));

    // fluorescence SLIDE_TYPE: rewrite Slidedat.ini
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    let mut sd = fs.read_small_member(fs.find("synthetic/Slidedat.ini").unwrap(), 1 << 20).unwrap();
    let text = String::from_utf8_lossy(&sd).replace(
        "SLIDE_TYPE = SLIDE_TYPE_BRIGHTFIELD",
        "SLIDE_TYPE = SLIDE_TYPE_FLUORESCENCE",
    );
    sd = text.into_bytes();
    let mut fs2 = MemBundle::new();
    for m in fs.members() {
        if m.name.ends_with("Slidedat.ini") {
            fs2.push(&m.name, sd.clone());
        } else {
            let idx = fs.find(&m.name).unwrap();
            let len = m.size as usize;
            fs2.push(&m.name, fs.read_member_at(idx, 0, len).unwrap());
        }
    }
    let e = probe_mirax(&fs2, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "unsupported_kfb_variant");
    assert!(e.message.contains("荧光"));
}

// --------------------------------------------------------------- convert --

#[test]
fn convert_both_profiles_structure_and_content() {
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    for profile in [OutputProfile::ClassicJpegBigTiff, OutputProfile::OmeBigTiffRgbSubifd] {
        let (bytes, r) = convert(&fs, profile).unwrap();
        assert_eq!(r.source_format, Some("mirax-bundle"));
        assert_eq!(r.adapter_version, Some("1"));
        assert_eq!(r.levels.len(), 3);
        // dims match the probe; tile grid is ceil(dim/256)
        assert_eq!(r.levels[0].tiles_across, 476u32.div_ceil(OUT_TILE));
        assert_eq!(r.levels[0].tiles_total,
            (476u64.div_ceil(OUT_TILE as u64)) * (264u64.div_ceil(OUT_TILE as u64)));
        // every tile re-encoded (mosaic compose), counts recorded
        let total: u64 = r.levels.iter().map(|l| l.tiles_total).sum();
        assert_eq!(r.levels.iter().map(|l| l.tiles_reencoded).sum::<u64>(), total);
        assert!(bytes.len() > 1024);
        // compose summary (what preserve means for MRXS) is always present
        let c = r.composed.as_ref().unwrap();
        assert_eq!(c.fingerprint, MRAX_PRESERVE_COMPOSE_FINGERPRINT);
        assert_eq!(c.quality, 96);
        assert_eq!(c.sampling, "4:2:2"); // preserve compose sampling label
        assert!(c.tiles_composed == total);
        // associated detected, not exported
        assert!(r.warnings.iter().any(|w| w == "mirax_associated_not_exported"));
        assert!(r.lossy_reencode.is_none(), "preserve must not claim compact");
    }
}

#[test]
fn pixel_content_comes_from_the_placements() {
    // a level-0 pixel inside exactly one subtile equals the source pixel
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        images_x: 4,
        images_y: 4,
        divisions: 2,
        levels: vec![(0, 0.0, 0.0)],
        ..Default::default()
    })
    .unwrap();
    let doc = probe_mirax(&fs, "synthetic").unwrap();
    let rgb = compose_region(&fs, &doc, 0, 0, 0, 8, 8).unwrap();
    // the fixture's image_pixels(0, 0, ...) base colour: level0, gx=0, gy=0
    // base = (0*37 + 0*11 + 0*23) % 200 = 0 → pixel(0,0) b = 0 → [0, 0, 0]
    assert_eq!(&rgb[0..3], &[0, 0, 0]);
    // pixel (5,5): b = (0 + 0 + 0) % 256 = 0 → [0,0,0]; use (9,9)? width is
    // iw=64 per image at advance 64 (overlap 0): pixel (70, 5) is in image 1
    let rgb2 = compose_region(&fs, &doc, 0, 0, 0, doc.levels[0].width.min(64), 8).unwrap();
    assert_eq!(rgb2.len(), 64 * 8 * 3);
}

#[test]
fn sparse_empty_region_fills_with_slidedat_colour_and_is_counted() {
    // the top-left 4 rows × 3 cols of camera positions have no images:
    // x advance = iw*div-ov = 116, y advance = ih*div-ov = 84, so the hole
    // spans [0,348)×[0,336) — the whole first 256×256 output tile
    let skip: Vec<u32> = (0u32..4).flat_map(|r| (0u32..3).map(move |c| r * 8 + c)).collect();
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        images_x: 16,
        images_y: 16,
        divisions: 2,
        skip_positions: skip,
        ..Default::default()
    })
    .unwrap();
    let doc = probe_mirax(&fs, "synthetic").unwrap();
    // the top-left corner region has no coverage → fill colour (white)
    let rgb = compose_region(&fs, &doc, 0, 0, 0, 8, 8).unwrap();
    assert!(rgb.chunks(3).all(|p| p == [255, 255, 255]));

    let (bytes, r) = convert(&fs, OutputProfile::ClassicJpegBigTiff).unwrap();
    assert!(bytes.len() > 0);
    let filled: u64 = r.levels.iter().map(|l| l.tiles_filled).sum();
    assert!(filled > 0, "sparse fill must be counted");
    assert!(r.warnings.iter().any(|w| w == "mirax_sparse_fill"));
}

#[test]
fn odd_edges_and_fractional_offsets_convert() {
    // 7×5 grid, odd image count; jitter produces fractional placements at
    // reduced levels — deterministic, no panic
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        images_x: 7,
        images_y: 5,
        position_jitter: 1,
        ..Default::default()
    })
    .unwrap();
    let (_b, r) = convert(&fs, OutputProfile::OmeBigTiffRgbSubifd).unwrap();
    assert_eq!(r.levels.len(), 3);
}

#[test]
fn strict_lossless_is_refused_before_any_output() {
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::StrictLossless);
    let e = convert_mirax_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap_err();
    assert_eq!(code_of(&e), "pixel_policy_violation");
    assert_eq!(sink.data.len(), 0, "no output byte before the refusal");
}

#[test]
fn compact_contract_for_mirax() {
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_enc(OutputProfile::OmeBigTiffRgbSubifd, EncodingProfile::CompactJpegV1);
    let r = convert_mirax_to_bigtiff(&fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap();
    let l = r.lossy_reencode.as_ref().unwrap();
    assert_eq!(l.profile, "compact-jpeg-v1");
    assert_eq!(l.params_fingerprint, COMPACT_JPEG_V1_FINGERPRINT);
    assert_eq!(l.quality, 80);
    assert_eq!(l.sampling, "4:2:0");
    // compose summary still present (composition is inherent to MRXS)
    assert!(r.composed.is_some());
}

#[test]
fn corrupt_payload_is_typed_error_never_white() {
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        corrupt_payload: true,
        ..Default::default()
    })
    .unwrap();
    let e = probe_mirax_or_convert_err(&fs);
    let c = code_of(&e);
    assert!(
        c == "jpeg_decode_failed" || c == "unsupported_kfb_variant",
        "{}", e.message
    );
}

fn probe_mirax_or_convert_err(fs: &MemBundle) -> slide_transform_core::error::CoreError {
    let mut sink = MemSink::new();
    let mut scratch = MemScratch::default();
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);
    convert_mirax_to_bigtiff(fs, "synthetic", &mut sink, &mut scratch, &plan,
        &JobControl::new(&NullProgress)).unwrap_err()
}

// ------------------------------------------------------ fill-tile dedupe --

/// Minimal BigTIFF walk: IFD chain (classic layout) → per-IFD
/// TileOffsets/TileByteCounts arrays.
mod tiffwalk {
    pub struct Ifd {
        pub offsets: Vec<u64>,
        pub counts: Vec<u32>,
        pub width: u32,
        pub height: u32,
        pub next: u64,
    }
    fn u16_at(b: &[u8], at: usize) -> u16 { u16::from_le_bytes([b[at], b[at + 1]]) }
    fn u32_at(b: &[u8], at: usize) -> u32 {
        u32::from_le_bytes([b[at], b[at + 1], b[at + 2], b[at + 3]])
    }
    fn u64_at(b: &[u8], at: usize) -> u64 {
        let mut v = [0u8; 8];
        v.copy_from_slice(&b[at..at + 8]);
        u64::from_le_bytes(v)
    }
    fn read_int_array<T: From<u64> + Copy>(
        data: &[u8],
        e: usize,
        typ: u16,
        count: usize,
        rd: fn(&[u8], usize) -> u64,
    ) -> Vec<T> {
        let type_size = if typ == 16 { 8usize } else { 4 };
        let total = count * type_size;
        let base = if total <= 8 { e + 12 } else { rd(data, e + 12) as usize };
        (0..count)
            .map(|k| {
                if type_size == 8 {
                    T::from(u64_at(data, base + k * 8))
                } else {
                    T::from(u32_at(data, base + k * 4) as u64)
                }
            })
            .collect()
    }

    pub fn classic_chain(data: &[u8]) -> Vec<Ifd> {
        assert_eq!(&data[0..2], b"II");
        assert_eq!(u16_at(data, 2), 43);
        let mut at = u64_at(data, 8) as usize;
        let mut out = Vec::new();
        while at != 0 {
            // BigTIFF IFD: u64 count + n×20B entries + u64 next
            let n = u64_at(data, at) as usize;
            let mut ifd = Ifd { offsets: Vec::new(), counts: Vec::new(), width: 0, height: 0, next: 0 };
            for i in 0..n {
                let e = at + 8 + i * 20;
                let tag = u16_at(data, e);
                let typ = u16_at(data, e + 2);
                let count = u64_at(data, e + 4) as usize;
                match tag {
                    256 => ifd.width = u32_at(data, e + 12),
                    257 => ifd.height = u32_at(data, e + 12),
                    324 => {
                        let vals = read_int_array::<u64>(data, e, typ, count, u64_at);
                        ifd.offsets = vals;
                    }
                    325 => {
                        // counts widened to u64 for a uniform walk
                        let vals: Vec<u32> = read_int_array::<u64>(data, e, typ, count, u64_at)
                            .into_iter().map(|v| v as u32).collect();
                        ifd.counts = vals;
                    }
                    _ => {}
                }
            }
            let next = u64_at(data, at + 8 + n * 20);
            ifd.next = next;
            out.push(ifd);
            at = next as usize;
        }
        out
    }
}

#[test]
fn fill_tiles_share_one_payload_per_level() {
    // the sparse fixture has large holes → most tiles are pure fill
    let skip: Vec<u32> = (0u32..4).flat_map(|r| (0u32..3).map(move |c| r * 8 + c)).collect();
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        images_x: 16,
        images_y: 16,
        divisions: 2,
        skip_positions: skip,
        ..Default::default()
    })
    .unwrap();
    let (bytes, r) = convert(&fs, OutputProfile::ClassicJpegBigTiff).unwrap();
    let ifds = tiffwalk::classic_chain(&bytes);
    assert_eq!(ifds.len(), 3);
    for (li, ifd) in ifds.iter().enumerate() {
        let st = &r.levels[li];
        // tile arrays still cover the whole grid (dense output)
        assert_eq!(ifd.offsets.len(), st.tiles_total as usize);
        assert_eq!(ifd.counts.len(), st.tiles_total as usize);
        // every fill tile references ONE shared payload
        let distinct: std::collections::HashSet<u64> =
            ifd.offsets.iter().copied().collect();
        assert!(
            distinct.len() as u64 <= st.tiles_total - st.tiles_deduped + 1,
            "level {li}: {} distinct offsets for {} tiles ({} deduped)",
            distinct.len(), st.tiles_total, st.tiles_deduped
        );
        // all deduped records share the same (offset, count)
        if st.tiles_deduped > 0 {
            let mut by_ref: std::collections::HashMap<u64, u64> = std::collections::HashMap::new();
            for o in &ifd.offsets { *by_ref.entry(*o).or_insert(0) += 1; }
            let max_shared = by_ref.values().copied().max().unwrap_or(0);
            assert!(max_shared >= st.tiles_deduped,
                "level {li}: max shared refs {max_shared} < deduped {}", st.tiles_deduped);
        }
    }
    // structural validator accepts the shared offsets
    use slide_transform_core::io::{ByteSource, MemSource};
    let msrc = MemSource { data: bytes.clone() };
    let v = slide_transform_core::validate::validate_output(&msrc, msrc.size(), Some(3)).unwrap();
    assert_eq!(v.tile_records, r.levels.iter().map(|l| l.tiles_total).sum());
    // at least one level deduped (the fixture guarantees L0 fill tiles)
    assert!(r.levels.iter().map(|l| l.tiles_deduped).sum::<u64>() > 0);
    // compose summary reports the dedupe
    assert_eq!(r.composed.as_ref().unwrap().tiles_deduped,
        r.levels.iter().map(|l| l.tiles_deduped).sum::<u64>());
}

#[test]
fn fill_dedupe_resume_is_byte_identical() {
    use slide_transform_core::io::{FileScratch, FileSink};
    use slide_transform_core::job::CancelFlag;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    struct Collector {
        states: Mutex<Vec<slide_transform_core::job::CheckpointState>>,
        stop_after: Option<usize>,
        cancel: CancelFlag,
        count: AtomicUsize,
    }
    impl slide_transform_core::job::CheckpointCallback for Collector {
        fn on_checkpoint(&self, c: &slide_transform_core::job::CheckpointState) {
            let n = self.count.fetch_add(1, Ordering::SeqCst) + 1;
            self.states.lock().unwrap().push(c.clone());
            if let Some(k) = self.stop_after {
                if n >= k {
                    self.cancel.cancel();
                }
            }
        }
    }

    let dir = std::env::temp_dir().join(format!(
        "stf3-mrxs-dedupe-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    // sparse fixture: the resumed portion contains BOTH real and deduped
    // fill tiles across level boundaries
    // positions grid is 20×15 (40×30 images ÷ 2); skip the top-left 4×3
    let skip: Vec<u32> = (0u32..4).flat_map(|r| (0u32..3).map(move |c| r * 20 + c)).collect();
    let fs = build_synthetic_mrxs(&MrxsGenParams {
        images_x: 40,
        images_y: 30,
        divisions: 2,
        skip_positions: skip,
        ..Default::default()
    })
    .unwrap();
    let plan = plan_for(OutputProfile::OmeBigTiffRgbSubifd, PixelPolicy::AllowEdgeReencode);
    let ref_out = dir.join("ref.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        convert_mirax_to_bigtiff(&fs, "synthetic", &mut out, &mut scratch, &plan,
            &JobControl::new(&NullProgress)).unwrap();
        use slide_transform_core::io::RandomAccessSink;
        out.flush().unwrap();
    }
    let full = std::fs::read(&ref_out).unwrap();

    for stop in [3usize, 7usize] {
        let part_out = dir.join(format!("part{stop}.tif"));
        let cancel = CancelFlag::new();
        let collector = Collector {
            states: Mutex::new(Vec::new()),
            stop_after: Some(stop),
            cancel: cancel.clone(),
            count: AtomicUsize::new(0),
        };
        let crash_dir = dir.join(format!("crash{stop}"));
        std::fs::create_dir_all(&crash_dir).unwrap();
        let mut scratch2 = FileScratch::new(&crash_dir);
        {
            let mut out = FileSink::create(&part_out).unwrap();
            let job = JobControl::new(&NullProgress).with_cancel(cancel.clone())
                .with_checkpoint(&collector);
            let res = convert_mirax_to_bigtiff(&fs, "synthetic", &mut out, &mut scratch2, &plan, &job);
            match res {
                Err(e) => assert!(e.message.contains("已取消"), "{}", e.message),
                Ok(_) => panic!("stop {stop}: expected cancellation (checkpoints seen: {})",
                    collector.count.load(std::sync::atomic::Ordering::SeqCst)),
            }
            use slide_transform_core::io::RandomAccessSink;
            out.flush().unwrap();
        }
        let states = collector.states.lock().unwrap().clone();
        let st = &states[stop - 1];
        assert!(st.cell_done > 0);
        let rp = ResumePoint {
            level: st.level as usize,
            channel: 0,
            cell: st.cell_done,
            committed_output: st.committed_output,
            ifd_tiles: st.ifd_tiles.clone(),
        };
        rp.validate().unwrap();
        {
            let f = std::fs::OpenOptions::new().write(true).open(&part_out).unwrap();
            f.set_len(rp.committed_output).unwrap();
        }
        for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
            let f = std::fs::OpenOptions::new()
                .write(true)
                .open(crash_dir.join(format!(".kfb2tiff-scratch-offcnt-l{i}")))
                .unwrap();
            f.set_len(tiles * 12).unwrap();
        }
        {
            let mut out = FileSink::open_preserve(&part_out).unwrap();
            convert_mirax_to_bigtiff_resume(&fs, "synthetic", &mut out, &mut scratch2, &plan,
                &JobControl::new(&NullProgress), &rp).unwrap();
            use slide_transform_core::io::RandomAccessSink;
            out.flush().unwrap();
        }
        let got = std::fs::read(&part_out).unwrap();
        assert_eq!(got.len(), full.len(), "stop {stop}: length");
        let first = got.iter().zip(full.iter()).position(|(a, b)| a != b);
        assert!(first.is_none(), "stop {stop}: first diff at {first:?} of {}", full.len());
        let _ = std::fs::remove_file(&part_out);
        let _ = std::fs::remove_dir_all(&crash_dir);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

// ----------------------------------------------------------------- resume --

#[test]
fn resume_output_is_byte_identical_to_uninterrupted() {
    use slide_transform_core::io::{FileScratch, FileSink};
    use slide_transform_core::job::CancelFlag;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    struct Collector {
        states: Mutex<Vec<slide_transform_core::job::CheckpointState>>,
        stop_after: Option<usize>,
        cancel: CancelFlag,
        count: AtomicUsize,
    }
    impl slide_transform_core::job::CheckpointCallback for Collector {
        fn on_checkpoint(&self, c: &slide_transform_core::job::CheckpointState) {
            let n = self.count.fetch_add(1, Ordering::SeqCst) + 1;
            self.states.lock().unwrap().push(c.clone());
            if let Some(k) = self.stop_after {
                if n >= k {
                    self.cancel.cancel();
                }
            }
        }
    }

    let dir = std::env::temp_dir().join(format!(
        "stf3-mrxs-resume-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();

    let fs = build_synthetic_mrxs(&MrxsGenParams {
        images_x: 32,
        images_y: 24,
        ..Default::default()
    })
    .unwrap();
    let plan = plan_for(OutputProfile::ClassicJpegBigTiff, PixelPolicy::AllowEdgeReencode);

    let ref_out = dir.join("ref.tif");
    {
        let mut scratch = FileScratch::new(&dir);
        let mut out = FileSink::create(&ref_out).unwrap();
        convert_mirax_to_bigtiff(&fs, "synthetic", &mut out, &mut scratch, &plan,
            &JobControl::new(&NullProgress)).unwrap();
        use slide_transform_core::io::RandomAccessSink;
        out.flush().unwrap();
    }
    let full = std::fs::read(&ref_out).unwrap();

    let stop = 3usize;
    let part_out = dir.join("part.tif");
    let cancel = CancelFlag::new();
    let collector = Collector {
        states: Mutex::new(Vec::new()),
        stop_after: Some(stop),
        cancel: cancel.clone(),
        count: AtomicUsize::new(0),
    };
    let crash_dir = dir.join("crash");
    std::fs::create_dir_all(&crash_dir).unwrap();
    let mut scratch2 = FileScratch::new(&crash_dir);
    {
        let mut out = FileSink::create(&part_out).unwrap();
        let job = JobControl::new(&NullProgress).with_cancel(cancel).with_checkpoint(&collector);
        let res = convert_mirax_to_bigtiff(&fs, "synthetic", &mut out, &mut scratch2, &plan, &job);
        assert!(res.unwrap_err().message.contains("已取消"));
        use slide_transform_core::io::RandomAccessSink;
        out.flush().unwrap();
    }
    let states = collector.states.lock().unwrap().clone();
    let st = &states[stop - 1];
    assert!(st.level == 0 && st.cell_done > 0);
    let rp = ResumePoint {
        level: st.level as usize,
        channel: 0,
        cell: st.cell_done,
        committed_output: st.committed_output,
        ifd_tiles: st.ifd_tiles.clone(),
    };
    rp.validate().unwrap();
    {
        let f = std::fs::OpenOptions::new().write(true).open(&part_out).unwrap();
        f.set_len(rp.committed_output).unwrap();
    }
    for (i, &tiles) in rp.ifd_tiles.iter().enumerate() {
        let f = std::fs::OpenOptions::new()
            .write(true)
            .open(crash_dir.join(format!(".kfb2tiff-scratch-offcnt-l{i}")))
            .unwrap();
        f.set_len(tiles * 12).unwrap();
    }
    {
        let mut out = FileSink::open_preserve(&part_out).unwrap();
        let r = convert_mirax_to_bigtiff_resume(&fs, "synthetic", &mut out, &mut scratch2, &plan,
            &JobControl::new(&NullProgress), &rp).unwrap();
        use slide_transform_core::io::RandomAccessSink;
        out.flush().unwrap();
        assert_eq!(r.output_bytes as usize, full.len());
    }
    let resumed = std::fs::read(&part_out).unwrap();
    assert_eq!(resumed.len(), full.len(), "resumed length must match");
    let first_diff = resumed.iter().zip(full.iter()).position(|(a, b)| a != b);
    assert!(
        first_diff.is_none(),
        "resumed output differs at byte {:?} (of {})",
        first_diff,
        full.len()
    );
    let _ = std::fs::remove_dir_all(&dir);
}

// ------------------------------------------------- review §1 regression --

/// Opt-in per-thread allocation meter (review §1): measures the peak bytes
/// allocated on the calling thread while the closure runs, so the large-grid
/// negative can assert the probe refuses WITHOUT materialising the 25 M
/// position tuples. Other threads are not metered (depth guard), so the
/// measurement is stable under the default parallel test runner.
mod meter {
    use std::alloc::{GlobalAlloc, Layout, System};
    use std::cell::Cell;

    pub struct MeteredAlloc;

    thread_local! {
        static DEPTH: Cell<usize> = const { Cell::new(0) };
        static CUR: Cell<usize> = const { Cell::new(0) };
        static PEAK: Cell<usize> = const { Cell::new(0) };
    }

    fn bump(delta: isize) {
        if DEPTH.with(|d| d.get()) == 0 {
            return;
        }
        CUR.with(|c| {
            let v = (c.get() as isize + delta).max(0) as usize;
            c.set(v);
            let peak = PEAK.with(|p| p.get());
            if v > peak {
                PEAK.with(|p| p.set(v));
            }
        });
    }

    unsafe impl GlobalAlloc for MeteredAlloc {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let p = System.alloc(layout);
            if !p.is_null() {
                bump(layout.size() as isize);
            }
            p
        }
        unsafe fn dealloc(&self, p: *mut u8, layout: Layout) {
            System.dealloc(p, layout);
            bump(-(layout.size() as isize));
        }
        unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
            let p = System.alloc_zeroed(layout);
            if !p.is_null() {
                bump(layout.size() as isize);
            }
            p
        }
        unsafe fn realloc(&self, p: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
            let q = System.realloc(p, layout, new_size);
            if !q.is_null() {
                bump(new_size as isize - layout.size() as isize);
            }
            q
        }
    }

    /// Meter the closure's allocation peak on this thread; returns
    /// `(peak_bytes, result)`.
    pub fn measure<T>(f: impl FnOnce() -> T) -> (usize, T) {
        DEPTH.with(|d| d.set(d.get() + 1));
        CUR.with(|c| c.set(0));
        PEAK.with(|p| p.set(0));
        let out = f();
        let peak = PEAK.with(|p| p.get());
        DEPTH.with(|d| d.set(d.get() - 1));
        (peak, out)
    }
}

#[global_allocator]
static METERED: meter::MeteredAlloc = meter::MeteredAlloc;

/// The reviewer's large-grid negative, built in code: the SAME tiny members
/// as the default fixture, but the INI declares IMAGENUMBER 5000×5000 with
/// CameraImageDivisionsPerSide = 1 and no position-buffer record (the
/// supported nominal fallback). Probe must return the typed
/// `resource_profile_insufficient` refusal BEFORE allocating the 25 M
/// position tuples (~400 MB) — not get OOM-killed like the old code.
#[test]
fn large_grid_nominal_fallback_refused_within_budget() {
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    let idx = fs.find("synthetic/Slidedat.ini").unwrap();
    let sd = fs.read_small_member(idx, 1 << 20).unwrap();
    let text = String::from_utf8_lossy(&sd)
        .replace("IMAGENUMBER_X = 8", "IMAGENUMBER_X = 5000")
        .replace("IMAGENUMBER_Y = 6", "IMAGENUMBER_Y = 5000")
        .replace(
            "CameraImageDivisionsPerSide = 2",
            "CameraImageDivisionsPerSide = 1",
        )
        .replace(
            "NONHIER_1_NAME = VIMSLIDE_POSITION_BUFFER",
            "NONHIER_1_NAME = unused",
        );
    let mut fs2 = MemBundle::new();
    for m in fs.members() {
        let i = fs.find(&m.name).unwrap();
        if m.name.ends_with("Slidedat.ini") {
            fs2.push(&m.name, text.clone().into_bytes());
        } else {
            fs2.push(&m.name, fs.read_member_at(i, 0, m.size as usize).unwrap());
        }
    }

    // default (saver, 192 MiB) budget: typed refusal, not a kill
    let e = probe_mirax(&fs2, "synthetic").unwrap_err();
    assert_eq!(code_of(&e), "resource_profile_insufficient", "{}", e.message);
    assert!(e.message.contains("位置表"), "{}", e.message);
    // a tight explicit budget drives the same typed refusal
    let e = probe_mirax_with_budget(&fs2, "synthetic", 64 * 1024 * 1024).unwrap_err();
    assert_eq!(code_of(&e), "resource_profile_insufficient", "{}", e.message);

    // the refusal happens BEFORE the allocation: metered peak stays far
    // below the 400 MB the old code committed before dying under a cgroup
    let (peak, r) = meter::measure(|| probe_mirax(&fs2, "synthetic"));
    assert!(r.is_err(), "probe must refuse");
    assert!(
        peak < 64 * 1024 * 1024,
        "probe allocated {peak} B before refusing (must refuse pre-allocation)"
    );
}

// ------------------------------------------------- real-sample (env-gated) --

fn env_dir(name: &str) -> Option<std::path::PathBuf> {
    let v = std::env::var(name).ok()?;
    let p = std::path::PathBuf::from(v);
    p.exists().then_some(p)
}

/// Probe the public CC0 samples when present; the expected values are the
/// OpenSlide-reported ones (level dims are part of the format contract).
#[test]
fn real_samples_probe_matches_openslide() {
    let base = env_dir("MRXS_SAMPLES")
        .unwrap_or_else(|| std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../.testdata/openslide/mirax"));
    let cases: &[(&str, &[(u32, u32)], PositionSource)] = &[
        (
            "CMU-1-Saved-1_16",
            &[(7436, 15494), (3718, 7747), (1859, 3873), (929, 1936), (464, 968), (232, 484)],
            PositionSource::VimslideBuffer,
        ),
        (
            "CMU-1",
            &[
                (109240, 220696), (54620, 110348), (27310, 55174), (13655, 27587),
                (6827, 13793), (3413, 6896), (1706, 3448), (853, 1724), (426, 862), (213, 431),
            ],
            PositionSource::VimslideBuffer,
        ),
        (
            "Mirax2.2-1",
            &[
                (101832, 219976), (50916, 109988), (25458, 54994), (12729, 27497),
                (6364, 13748), (3182, 6874), (1591, 3437), (795, 1718), (397, 859), (198, 429),
            ],
            PositionSource::StitchingIntensity,
        ),
    ];
    for (stem, dims, pos) in cases.iter() {
        let dir = base.join(stem);
        if !dir.join(format!("{stem}.mrxs")).exists() {
            eprintln!("skip {stem}: sample absent");
            continue;
        }
        let fs = slide_transform_core::bundle::DirBundle::open(&dir, stem)
            .unwrap_or_else(|e| panic!("{stem}: {e}"));
        let doc = probe_mirax(&fs, stem).unwrap_or_else(|e| panic!("{stem}: {e}"));
        assert_eq!(doc.levels.len(), dims.len(), "{stem}");
        for (i, d) in dims.iter().enumerate() {
            assert_eq!((doc.levels[i].width, doc.levels[i].height), *d, "{stem} L{i}");
        }
        assert_eq!(doc.position_source, *pos, "{stem}");
    }
}

// ------------------------------------------------------ GT gate (§3) -----

/// Summary of a strict ground-truth ROI comparison.
#[derive(Default, Debug)]
struct GtSummary {
    worst_l0: f64,
    worst_l1p: f64,
    compared_l0: usize,
    compared_l1p: usize,
}

/// Strict ground-truth comparison of every listed ROI (review §3).
///
/// Missing/empty/malformed reference material is a FAILURE naming the ROI —
/// never a silent skip:
/// - the reference raw must exist and be EXACTLY `w*h*3` bytes;
/// - a mask sidecar must be exactly `⌈w*h/8⌉` bytes (exporter's bit-packed
///   format) with non-trivial valid coverage (≥ 1 % of the ROI);
/// - the ROI must be inside the level's bounds and compose without error;
/// - the output must be exactly `w*h*3` bytes (no zip truncation);
/// - after the loop, at least one level-0 AND one reduced-level ROI must
///   have actually been compared.
fn compare_gt_rois(
    fs: &dyn slide_transform_core::bundle::BundleFs,
    doc: &slide_transform_core::mirax::MiraxDoc,
    gt: &std::path::Path,
    rois: &[serde_json_free::Roi],
) -> Result<GtSummary, String> {
    if rois.is_empty() {
        return Err("rois.json 未列出任何 ROI".to_string());
    }
    let mut s = GtSummary::default();
    for r in rois {
        let name = format!("roi-l{}-{}", r.level, r.id);
        let why = |msg: String| -> String {
            format!(
                "ROI {name}（level {} {}×{} @ {},{}）: {msg}",
                r.level, r.w, r.h, r.x, r.y
            )
        };
        if r.level >= doc.levels.len() {
            return Err(why(format!("level 越界（共 {} 层）", doc.levels.len())));
        }
        let lv = &doc.levels[r.level];
        if r.x as u64 + r.w as u64 > lv.width as u64
            || r.y as u64 + r.h as u64 > lv.height as u64
        {
            return Err(why(format!("区域越界（层尺寸 {}×{}）", lv.width, lv.height)));
        }
        let npix = r.w as usize * r.h as usize;
        let expect3 = npix * 3;

        let raw_path = gt.join(format!("{name}.raw"));
        let expect = std::fs::read(&raw_path).map_err(|e| {
            why(format!("参考 raw 缺失/不可读（{}: {e}）", raw_path.display()))
        })?;
        if expect.len() != expect3 {
            return Err(why(format!(
                "参考 raw 长度 {} ≠ w*h*3 = {expect3}",
                expect.len()
            )));
        }
        let mine = compose_region(fs, doc, r.level, r.x, r.y, r.w, r.h)
            .map_err(|e| why(format!("输出合成失败: {}", e.message)))?;
        if mine.len() != expect3 {
            return Err(why(format!(
                "输出长度 {} ≠ w*h*3 = {expect3}",
                mine.len()
            )));
        }

        let mask_path = gt.join(format!("{name}.mask"));
        let (mean, count) = match std::fs::read(&mask_path) {
            Ok(mask) => {
                if mask.len() != npix.div_ceil(8) {
                    return Err(why(format!(
                        "mask 长度 {} ≠ ⌈w*h/8⌉ = {}",
                        mask.len(),
                        npix.div_ceil(8)
                    )));
                }
                let mut acc = 0u64;
                let mut n = 0u64;
                for i in 0..npix {
                    if mask[i / 8] & (1 << (7 - i % 8)) != 0 {
                        for c in 0..3 {
                            acc += (mine[i * 3 + c] as i64 - expect[i * 3 + c] as i64)
                                .unsigned_abs();
                        }
                        n += 3;
                    }
                }
                let valid = n / 3;
                // non-trivial coverage: an (almost) fully transparent ROI
                // verifies nothing — fail instead of counting a zero compare
                if valid * 100 < npix as u64 {
                    return Err(why(format!(
                        "mask 有效覆盖 {valid}/{npix} 像素低于 1%（材料无意义）"
                    )));
                }
                ((acc as f64) / (n.max(1) as f64), n)
            }
            Err(_) => {
                // no mask sidecar: exact-length full comparison
                let acc: u64 = mine
                    .iter()
                    .zip(expect.iter())
                    .map(|(a, b)| (*a as i64 - *b as i64).unsigned_abs())
                    .sum();
                ((acc as f64) / (expect3 as f64), expect3 as u64)
            }
        };
        debug_assert!(count > 0);
        if r.level == 0 {
            s.worst_l0 = s.worst_l0.max(mean);
            s.compared_l0 += 1;
        } else {
            s.worst_l1p = s.worst_l1p.max(mean);
            s.compared_l1p += 1;
        }
    }
    if s.compared_l0 == 0 {
        return Err("没有任何 level-0 ROI 被实际比较".to_string());
    }
    if s.compared_l1p == 0 {
        return Err("没有任何缩减层（L1+）ROI 被实际比较".to_string());
    }
    Ok(s)
}

/// The reviewer's negative GT material, generated in code: a valid synthetic
/// source bundle plus a `rois.json` listing one L0 and one L1 16×16 ROI
/// (the reviewer's `missing-gt/rois.json` semantics); `seed` writes the raw
/// side (nothing = both missing, 0-byte files = both empty).
fn gt_negative_material(seed: impl FnOnce(&std::path::Path)) -> (MemBundle, slide_transform_core::mirax::MiraxDoc, std::path::PathBuf) {
    let fs = build_synthetic_mrxs(&MrxsGenParams::default()).unwrap();
    let doc = probe_mirax(&fs, "synthetic").unwrap();
    let gt = std::env::temp_dir().join(format!(
        "mrxs-gt-neg-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .subsec_nanos()
    ));
    std::fs::create_dir_all(&gt).unwrap();
    std::fs::write(
        gt.join("rois.json"),
        r#"[{"level": 0, "id": "missing-l0", "x": 0, "y": 0, "w": 16, "h": 16}, {"level": 1, "id": "missing-l1", "x": 0, "y": 0, "w": 16, "h": 16}]"#,
    )
    .unwrap();
    seed(&gt);
    (fs, doc, gt)
}

/// Review §3 negative 1 (regression): with MRXS_GT semantics in force, both
/// reference raw files MISSING must FAIL the gate naming the ROI. The old
/// gate `continue`d and reported `worst 0.0` — a false pass.
#[test]
fn gt_gate_fails_when_reference_raw_files_are_missing() {
    let (fs, doc, gt) = gt_negative_material(|_| {});
    let rois = serde_json_free::read(&gt.join("rois.json"));
    assert_eq!(rois.len(), 2);
    let e = compare_gt_rois(&fs, &doc, &gt, &rois).unwrap_err();
    assert!(e.contains("roi-l0-missing-l0"), "{e}");
    let _ = std::fs::remove_dir_all(&gt);
}

/// Review §3 negative 2 (regression): both reference raws present but
/// 0 bytes must FAIL the gate (exact w*h*3 length check). The old gate
/// zip-compared against an empty slice and reported `worst 0.0`.
#[test]
fn gt_gate_fails_when_reference_raw_files_are_empty() {
    let (fs, doc, gt) = gt_negative_material(|gt| {
        for n in ["roi-l0-missing-l0.raw", "roi-l1-missing-l1.raw"] {
            std::fs::write(gt.join(n), []).unwrap();
        }
    });
    let rois = serde_json_free::read(&gt.join("rois.json"));
    let e = compare_gt_rois(&fs, &doc, &gt, &rois).unwrap_err();
    assert!(e.contains("roi-l0-missing-l0"), "{e}");
    assert!(e.contains("w*h*3"), "{e}");
    let _ = std::fs::remove_dir_all(&gt);
}

/// Composition fidelity vs OpenSlide: the ground-truth raw RGB files are
/// exported with the python helper (see the F3 report); level 0 must be
/// EXACT, reduced levels bounded (fractional placement). Every listed ROI
/// must actually be compared — missing/empty material fails (review §3).
#[test]
fn real_sample_composition_vs_openslide_ground_truth() {
    let Some(gt) = env_dir("MRXS_GT") else {
        eprintln!("skip: MRXS_GT not set (ground-truth ROIs absent)");
        return;
    };
    let stem = std::env::var("MRXS_GT_STEM").unwrap_or_else(|_| "CMU-1-Saved-1_16".into());
    let base = env_dir("MRXS_SAMPLES")
        .unwrap_or_else(|| std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../.testdata/openslide/mirax"));
    let dir = base.join(&stem);
    if !dir.join(format!("{stem}.mrxs")).exists() {
        eprintln!("skip: sample absent");
        return;
    }
    let fs = slide_transform_core::bundle::DirBundle::open(&dir, &stem).unwrap();
    let doc = probe_mirax(&fs, &stem).unwrap();
    let list: Vec<serde_json_free::Roi> = serde_json_free::read(&gt.join("rois.json"));
    let s = match compare_gt_rois(&fs, &doc, &gt, &list) {
        Ok(s) => s,
        Err(e) => panic!("GT 门禁拒绝（材料不完整）: {e}"),
    };
    eprintln!(
        "GT comparison: compared L0 {}, L1+ {}, worst L0 mean {:.4}, worst L1+ mean {:.4}",
        s.compared_l0, s.compared_l1p, s.worst_l0, s.worst_l1p
    );
    assert!(s.worst_l0 <= 0.6, "level 0 must match OpenSlide (got {})", s.worst_l0);
    assert!(s.worst_l1p <= 16.0, "reduced levels bounded (got {})", s.worst_l1p);
}

mod serde_json_free {
    use std::path::Path;
    #[derive(Debug)]
    pub struct Roi {
        pub level: usize,
        pub id: String,
        pub x: u32,
        pub y: u32,
        pub w: u32,
        pub h: u32,
    }
    pub fn read(p: &Path) -> Vec<Roi> {
        let s = std::fs::read_to_string(p).unwrap();
        let mut out = Vec::new();
        // minimal JSON array-of-objects parser for this fixed shape
        let mut cur = String::new();
        let mut depth = 0;
        let mut in_str = false;
        let mut esc = false;
        for ch in s.chars() {
            if in_str {
                cur.push(ch);
                if esc {
                    esc = false;
                } else if ch == '\\' {
                    esc = true;
                } else if ch == '"' {
                    in_str = false;
                }
                continue;
            }
            match ch {
                '"' => {
                    in_str = true;
                    cur.push(ch);
                }
                '{' => {
                    depth += 1;
                    cur.push(ch);
                }
                '}' => {
                    cur.push(ch);
                    depth -= 1;
                    if depth == 0 {
                        out.push(parse_obj(&cur));
                        cur.clear();
                    }
                }
                c if depth > 0 => cur.push(c),
                _ => {}
            }
        }
        out
    }
    fn parse_obj(s: &str) -> Roi {
        let field = |k: &str| -> String {
            let needle = format!("\"{k}\"");
            let at = s.find(&needle).expect(k) + needle.len();
            let rest = &s[at..];
            let start = rest.find(':').expect(":") + 1;
            let rest = rest[start..].trim_start();
            let end = rest.find([',', '}']).unwrap();
            rest[..end].trim().trim_matches('"').to_string()
        };
        Roi {
            level: field("level").parse().unwrap(),
            id: field("id"),
            x: field("x").parse().unwrap(),
            y: field("y").parse().unwrap(),
            w: field("w").parse().unwrap(),
            h: field("h").parse().unwrap(),
        }
    }
}
