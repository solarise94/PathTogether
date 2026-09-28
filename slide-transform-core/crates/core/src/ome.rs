//! OME-XML construction for the fluorescence writer (structure mirrors
//! `kfb/converter_fl.py::_build_ome_xml`, with the C1 change required by the
//! acceptance review: ExposureTime lives on `<Plane>` (per-plane), not on
//! `<Channel>`; the unit stays marked as assumed milliseconds).

/// Python-style `repr()` for f64 (shortest round-trip, exponent form for
/// very small/large magnitudes). Used for byte-parity of metadata strings.
pub fn py_repr_f64(v: f64) -> String {
    if !v.is_finite() {
        return if v.is_nan() { "NaN".into() } else if v > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    if v == 0.0 {
        return if v.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    // Python repr switches to exponent notation when exp10 < -4 or >= 17.
    let abs = v.abs();
    let exp = abs.log10().floor() as i32;
    if !(-4..17).contains(&exp) {
        // e.g. 1e-05 / 1.5e+20
        let s = format!("{:e}", v); // Rust: "1e-5"/"1.5e20"
        return fix_exp(&s);
    }
    let s = format!("{v}");
    if s.contains('.') || s.contains('e') || s.contains("inf") || s.contains("NaN") {
        s
    } else {
        format!("{s}.0")
    }
}

fn fix_exp(s: &str) -> String {
    // Rust "1e-5" → Python "1e-05"; "1.5e20" → "1.5e+20"
    if let Some(pos) = s.find(['e', 'E']) {
        let (mant, exp) = s.split_at(pos);
        let e = &exp[1..];
        let (sign, digits) = if let Some(d) = e.strip_prefix('-') {
            ("-", d)
        } else if let Some(d) = e.strip_prefix('+') {
            ("+", d)
        } else {
            ("+", e)
        };
        if digits.len() < 2 {
            return format!("{mant}e{sign}0{digits}");
        }
        return format!("{mant}e{sign}{digits}");
    }
    s.to_string()
}

/// Python `format(x, ".17g")` (17 significant digits, %g-style trimming).
pub fn py_g17(v: f64) -> String {
    if v == v.trunc() && v.abs() < 1e15 {
        // %.17g prints integral values without a decimal point
        return format!("{}", v as i64);
    }
    let s = format!("{:.*e}", 16, v);
    let (mant, exp) = s.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    if !(-4..17).contains(&exp) {
        let mant = mant.trim_end_matches('0').trim_end_matches('.');
        let (sign, digits) =
            if exp < 0 { ("-", (-(exp as i64)) as u64) } else { ("+", exp as u64) };
        if digits < 10 {
            return format!("{mant}e{sign}0{digits}");
        }
        return format!("{mant}e{sign}{digits}");
    }
    // fixed notation with 17 significant digits, trailing zeros trimmed
    let decimals = (16 - exp).max(0) as usize;
    let mut f = format!("{:.*}", decimals, v);
    if f.contains('.') {
        f = f.trim_end_matches('0').trim_end_matches('.').to_string();
    }
    f
}

fn xml_escape(s: &str) -> String {
    // xml.sax.saxutils.escape default: &, <, >
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(c),
        }
    }
    out
}

/// Channel metadata for OME-XML.
pub struct OmeChannel {
    pub index: usize,
    pub name: String,
    pub color_rgb: (u32, u32, u32),
    pub exposure: f64,
}

/// Build the OME-XML description bytes (trailing NUL included, UTF-8).
#[allow(clippy::too_many_arguments)]
pub fn build_ome_xml(
    objective: f64,
    scanner_id: &str,
    width: u32,
    height: u32,
    mpp: f64,
    channels: &[OmeChannel],
) -> Vec<u8> {
    let mut ch_xml = String::new();
    let mut td_xml = String::new();
    let mut plane_xml = String::new();
    for ch in channels {
        let (r, g, b) = ch.color_rgb;
        let argb = (255u64 << 24) | ((r as u64) << 16) | ((g as u64) << 8) | b as u64;
        let argb = if argb >= 1 << 31 { argb as i64 - (1 << 32) } else { argb as i64 };
        // C1: ExposureTime on <Plane>, not <Channel>
        ch_xml.push_str(&format!(
            "<Channel ID=\"Channel:0:{}\" Name=\"{}\" Color=\"{}\" SamplesPerPixel=\"1\"/>",
            ch.index,
            xml_escape(&ch.name),
            argb
        ));
        td_xml.push_str(&format!(
            "<TiffData FirstC=\"{}\" FirstT=\"0\" FirstZ=\"0\" IFD=\"{}\" PlaneCount=\"1\"/>",
            ch.index, ch.index
        ));
        plane_xml.push_str(&format!(
            "<Plane TheZ=\"0\" TheC=\"{}\" TheT=\"0\" ExposureTime=\"{}\" ExposureTimeUnit=\"ms\"/>",
            ch.index,
            py_g17(ch.exposure)
        ));
    }
    let xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><OME xmlns=\"http://www.openmicroscopy.org/Schemas/OME/2016-06\">\
<Instrument ID=\"Instrument:0\"><Objective ID=\"Objective:0:0\" NominalMagnification=\"{}\"/></Instrument>\
<Image ID=\"Image:0\" Name=\"{}\">\
<Pixels ID=\"Pixels:0\" DimensionOrder=\"XYZCT\" Type=\"uint8\" SizeX=\"{}\" SizeY=\"{}\" SizeC=\"{}\" SizeZ=\"1\" SizeT=\"1\" Interleaved=\"false\" PhysicalSizeX=\"{}\" PhysicalSizeXUnit=\"µm\" PhysicalSizeY=\"{}\" PhysicalSizeYUnit=\"µm\">{}{}{}</Pixels></Image></OME>",
        py_g17(objective),
        xml_escape(if scanner_id.is_empty() { "KFBF" } else { scanner_id }),
        width,
        height,
        channels.len(),
        py_repr_f64(mpp),
        py_repr_f64(mpp),
        ch_xml,
        td_xml,
        plane_xml,
    );
    let mut out = xml.into_bytes();
    out.push(0);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn py_repr_basics() {
        assert_eq!(py_repr_f64(0.4841049), "0.4841049");
        assert_eq!(py_repr_f64(20.0), "20.0");
        assert_eq!(py_repr_f64(0.25), "0.25");
        assert_eq!(py_repr_f64(0.2506266), "0.2506266");
        assert_eq!(py_repr_f64(0.0), "0.0");
        assert_eq!(py_repr_f64(1e-5), "1e-05");
        assert_eq!(py_repr_f64(1.5e20), "1.5e+20");
    }

    #[test]
    fn py_g17_basics() {
        assert_eq!(py_g17(40.0), "40");
        assert_eq!(py_g17(6.0), "6");
        assert_eq!(py_g17(2.5), "2.5");
    }

    #[test]
    fn ome_has_plane_exposure() {
        let ch = [OmeChannel {
            index: 0,
            name: "DAPI".into(),
            color_rgb: (0, 0, 229),
            exposure: 6.0,
        }];
        let xml = build_ome_xml(40.0, "SC", 600, 400, 0.25, &ch);
        let s = String::from_utf8_lossy(&xml[..xml.len() - 1]);
        assert!(s.contains("<Plane TheZ=\"0\" TheC=\"0\" TheT=\"0\" ExposureTime=\"6\""));
        assert!(!s.contains("Channel ID=\"Channel:0:0\" Name=\"DAPI\" Color=\"-16776317\" SamplesPerPixel=\"1\" ExposureTime="));
        assert!(s.contains("ExposureTimeUnit=\"ms\""));
        assert!(s.contains("NominalMagnification=\"40\""));
    }
}
