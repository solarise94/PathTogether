//! Codec parity experiment driver (used by the C1 evidence scripts, not
//! shipped). Encodes/decodes raw buffers so Python (Pillow) can compare
//! byte-for-byte.
//!
//! Usage:
//!   parity encode-rgb <raw> <w> <h> <quality> <444|422|420> <out.jpg>
//!   parity encode-rgb-q <raw> <w> <h> <sampling> <yq.hex> <cq.hex> <out.jpg>
//!   parity encode-gray <raw> <w> <h> <quality> <out.jpg>
//!   parity encode-gray-q <raw> <w> <h> <q.hex> <out.jpg>
//!   parity decode-rgb <in.jpg> <out.raw>
//!   parity decode-gray <in.jpg> <out.raw>
//!   parity qtables <in.jpg>            (stdout: table count then hex tables)

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: parity <cmd> ...");
        return ExitCode::from(2);
    }
    let run = || -> Result<(), String> {
        match args[1].as_str() {
            "encode-rgb" => {
                let raw = std::fs::read(&args[2]).map_err(|e| e.to_string())?;
                let w: u32 = args[3].parse().map_err(|_| "w")?;
                let h: u32 = args[4].parse().map_err(|_| "h")?;
                let q: u8 = args[5].parse().map_err(|_| "q")?;
                let s = parse_sampling(&args[6])?;
                let cfg = slide_transform_core::jpeg::EncoderCfg::with_quality(q, s);
                let out = slide_transform_core::jpeg::encode_rgb(&raw, w, h, &cfg)
                    .map_err(|e| e.to_string())?;
                std::fs::write(&args[7], out).map_err(|e| e.to_string())
            }
            "encode-rgb-q" => {
                let raw = std::fs::read(&args[2]).map_err(|e| e.to_string())?;
                let w: u32 = args[3].parse().map_err(|_| "w")?;
                let h: u32 = args[4].parse().map_err(|_| "h")?;
                let s = parse_sampling(&args[5])?;
                let yq = parse_table(&args[6])?;
                let cq = parse_table(&args[7])?;
                let cfg = slide_transform_core::jpeg::EncoderCfg {
                    y_q: yq,
                    c_q: cq,
                    sampling: s,
                 rgb: false, };
                let out = slide_transform_core::jpeg::encode_rgb(&raw, w, h, &cfg)
                    .map_err(|e| e.to_string())?;
                std::fs::write(&args[8], out).map_err(|e| e.to_string())
            }
            "encode-gray" => {
                let raw = std::fs::read(&args[2]).map_err(|e| e.to_string())?;
                let w: u32 = args[3].parse().map_err(|_| "w")?;
                let h: u32 = args[4].parse().map_err(|_| "h")?;
                let q: u8 = args[5].parse().map_err(|_| "q")?;
                let t = slide_transform_core::jpeg::tables::std_luma_quality(q);
                let out =
                    slide_transform_core::jpeg::encode_gray(&raw, w, h, &t)
                        .map_err(|e| e.to_string())?;
                std::fs::write(&args[6], out).map_err(|e| e.to_string())
            }
            "encode-gray-q" => {
                let raw = std::fs::read(&args[2]).map_err(|e| e.to_string())?;
                let w: u32 = args[3].parse().map_err(|_| "w")?;
                let h: u32 = args[4].parse().map_err(|_| "h")?;
                let t = parse_table(&args[5])?;
                let out =
                    slide_transform_core::jpeg::encode_gray(&raw, w, h, &t)
                        .map_err(|e| e.to_string())?;
                std::fs::write(&args[6], out).map_err(|e| e.to_string())
            }
            "decode-rgb" | "decode-gray" => {
                let jpg = std::fs::read(&args[2]).map_err(|e| e.to_string())?;
                let img =
                    slide_transform_core::jpeg::decode(&jpg, 1 << 22).map_err(|e| e.to_string())?;
                let want_gray = args[1] == "decode-gray";
                if want_gray && img.kind != slide_transform_core::jpeg::ColorKind::Gray {
                    return Err("not gray".into());
                }
                if !want_gray && img.kind != slide_transform_core::jpeg::ColorKind::Rgb {
                    return Err("not rgb".into());
                }
                std::fs::write(&args[3], img.data).map_err(|e| e.to_string())
            }
            "qtables" => {
                let jpg = std::fs::read(&args[2]).map_err(|e| e.to_string())?;
                match slide_transform_core::jpeg::qtables_pillow_style(&jpg) {
                    Some(tabs) => {
                        println!("{}", tabs.len());
                        for t in &tabs {
                            println!("{}", t.iter().map(|v| format!("{v:02x}")).collect::<String>());
                        }
                        Ok(())
                    }
                    None => Err("no qtables".into()),
                }
            }
            _ => Err(format!("unknown cmd {}", args[1])),
        }
    };
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("parity: {e}");
            ExitCode::from(1)
        }
    }
}

fn parse_sampling(s: &str) -> Result<slide_transform_core::jpeg::Sampling, String> {
    match s {
        "444" => Ok(slide_transform_core::jpeg::Sampling::S444),
        "422" => Ok(slide_transform_core::jpeg::Sampling::S422),
        "420" => Ok(slide_transform_core::jpeg::Sampling::S420),
        _ => Err(format!("sampling {s}")),
    }
}

fn parse_table(hex: &str) -> Result<[u16; 64], String> {
    let b = hex::decode(hex).map_err(|e| e.to_string())?;
    if b.len() != 64 {
        return Err(format!("table hex len {}", b.len()));
    }
    let mut t = [0u16; 64];
    for (i, v) in b.iter().enumerate() {
        t[i] = *v as u16;
    }
    Ok(t)
}

mod hex {
    pub fn decode(s: &str) -> Result<Vec<u8>, String> {
        if s.len() % 2 != 0 {
            return Err("odd hex".into());
        }
        (0..s.len() / 2)
            .map(|i| {
                u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).map_err(|e| e.to_string())
            })
            .collect()
    }
}
