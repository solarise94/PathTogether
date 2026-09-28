//! Baseline JPEG constants: standard quantization tables (ITU T.81 K.1,
//! zigzag order — byte-identical to what Pillow/libjpeg emit at quality 50),
//! standard Annex K.3 Huffman tables (extracted from a Pillow-saved file so
//! the bit/val arrays are exact), libjpeg quality scaling, zigzag maps and
//! bit-category helpers.

/// Natural-order index of the k-th zigzag position (jpeg_natural_order).
pub const NATURAL_OF_ZIGZAG: [usize; 64] = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33,
    40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43,
    36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53,
    60, 61, 54, 47, 55, 62, 63,
];

/// Zigzag position of the k-th natural-order coefficient.
pub fn zigzag_of_natural() -> [u8; 64] {
    let mut z = [0u8; 64];
    for (k, &n) in NATURAL_OF_ZIGZAG.iter().enumerate() {
        z[n] = k as u8;
    }
    z
}

/// Standard luminance quantization table, zigzag order (T.81 Table K.1).
pub const STD_QUANT_LUMA_ZZ: [u16; 64] = [
    16, 11, 12, 14, 12, 10, 16, 14, 13, 14, 18, 17, 16, 19, 24, 40, 26, 24,
    22, 22, 24, 49, 35, 37, 29, 40, 58, 51, 61, 60, 57, 51, 56, 55, 64, 72,
    92, 78, 64, 68, 87, 69, 55, 56, 80, 109, 81, 87, 95, 98, 103, 104, 103,
    62, 77, 113, 121, 112, 100, 120, 92, 101, 103, 99,
];

/// Standard chrominance quantization table, zigzag order (T.81 Table K.2).
pub const STD_QUANT_CHROMA_ZZ: [u16; 64] = [
    17, 18, 18, 24, 21, 24, 47, 26, 26, 47, 99, 66, 56, 66, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
];

/// libjpeg `jpeg_quality_scaling`: map 1..=100 to a table scale factor.
pub fn quality_scaling(quality: u8) -> u32 {
    let q = quality.clamp(1, 100) as u32;
    if q < 50 {
        5000 / q
    } else {
        200 - q * 2
    }
}

/// libjpeg `jpeg_add_quant_table` with `force_baseline = TRUE`: integer
/// scaling `(base * scale + 50) / 100`, clamped to 1..=255. This reproduces
/// Pillow's table bytes exactly (verified empirically for q90/q95).
pub fn quality_table(base: &[u16; 64], quality: u8) -> [u16; 64] {
    let scale = quality_scaling(quality);
    let mut out = [0u16; 64];
    for i in 0..64 {
        let v = (base[i] as u32 * scale + 50) / 100;
        out[i] = v.clamp(1, 255) as u16;
    }
    out
}

/// Convert a zigzag-ordered table to natural (row-major) order.
pub fn natural_from_zigzag(zz: &[u16; 64]) -> [u16; 64] {
    let mut nat = [0u16; 64];
    for (k, &n) in NATURAL_OF_ZIGZAG.iter().enumerate() {
        nat[n] = zz[k];
    }
    nat
}

/// Convert a natural (row-major) table to zigzag order (DQT segment order).
pub fn zigzag_from_natural(nat: &[u16; 64]) -> [u16; 64] {
    let mut zz = [0u16; 64];
    for (k, &n) in NATURAL_OF_ZIGZAG.iter().enumerate() {
        zz[k] = nat[n];
    }
    zz
}

/// libjpeg `jpeg_add_quant_table` with `force_baseline = TRUE`, starting from
/// the **natural-order** standard table (jcparam.c layout; identical bytes to
/// Pillow). All encoder-facing quant tables in this crate are natural order —
/// the same contract as Pillow's `im.quantization` / `save(qtables=...)`.
pub fn std_luma_quality(quality: u8) -> [u16; 64] {
    quality_table(&natural_from_zigzag(&STD_QUANT_LUMA_ZZ), quality)
}

pub fn std_chroma_quality(quality: u8) -> [u16; 64] {
    quality_table(&natural_from_zigzag(&STD_QUANT_CHROMA_ZZ), quality)
}

// --------------------------------------------------------------------------- //
// Standard Huffman tables (Annex K.3; exact bytes as emitted by Pillow)
// --------------------------------------------------------------------------- //

pub const DC_LUMA_BITS: [u8; 16] =
    [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0];
pub const DC_LUMA_VALS: [u8; 12] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];

pub const DC_CHROMA_BITS: [u8; 16] =
    [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0];
pub const DC_CHROMA_VALS: [u8; 12] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];

pub const AC_LUMA_BITS: [u8; 16] =
    [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125];
pub const AC_LUMA_VALS: [u8; 162] = [
    1, 2, 3, 0, 4, 17, 5, 18, 33, 49, 65, 6, 19, 81, 97, 7, 34, 113, 20, 50,
    129, 145, 161, 8, 35, 66, 177, 193, 21, 82, 209, 240, 36, 51, 98, 114,
    130, 9, 10, 22, 23, 24, 25, 26, 37, 38, 39, 40, 41, 42, 52, 53, 54, 55,
    56, 57, 58, 67, 68, 69, 70, 71, 72, 73, 74, 83, 84, 85, 86, 87, 88, 89,
    90, 99, 100, 101, 102, 103, 104, 105, 106, 115, 116, 117, 118, 119, 120,
    121, 122, 131, 132, 133, 134, 135, 136, 137, 138, 146, 147, 148, 149,
    150, 151, 152, 153, 154, 162, 163, 164, 165, 166, 167, 168, 169, 170,
    178, 179, 180, 181, 182, 183, 184, 185, 186, 194, 195, 196, 197, 198,
    199, 200, 201, 202, 210, 211, 212, 213, 214, 215, 216, 217, 218, 225,
    226, 227, 228, 229, 230, 231, 232, 233, 234, 241, 242, 243, 244, 245,
    246, 247, 248, 249, 250,
];

pub const AC_CHROMA_BITS: [u8; 16] =
    [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 119];
pub const AC_CHROMA_VALS: [u8; 162] = [
    0, 1, 2, 3, 17, 4, 5, 33, 49, 6, 18, 65, 81, 7, 97, 113, 19, 34, 50, 129,
    8, 20, 66, 145, 161, 177, 193, 9, 35, 51, 82, 240, 21, 98, 114, 209, 10,
    22, 36, 52, 225, 37, 241, 23, 24, 25, 26, 38, 39, 40, 41, 42, 53, 54, 55,
    56, 57, 58, 67, 68, 69, 70, 71, 72, 73, 74, 83, 84, 85, 86, 87, 88, 89,
    90, 99, 100, 101, 102, 103, 104, 105, 106, 115, 116, 117, 118, 119, 120,
    121, 122, 130, 131, 132, 133, 134, 135, 136, 137, 138, 146, 147, 148,
    149, 150, 151, 152, 153, 154, 162, 163, 164, 165, 166, 167, 168, 169,
    170, 178, 179, 180, 181, 182, 183, 184, 185, 186, 194, 195, 196, 197,
    198, 199, 200, 201, 202, 210, 211, 212, 213, 214, 215, 216, 217, 218,
    226, 227, 228, 229, 230, 231, 232, 233, 234, 242, 243, 244, 245, 246,
    247, 248, 249, 250,
];

// --------------------------------------------------------------------------- //
// Derived Huffman tables (canonical codes, MSB-first)
// --------------------------------------------------------------------------- //

/// A derived Huffman decoding/encoding table.
#[derive(Clone)]
pub struct HuffTable {
    pub bits: [u8; 16],
    pub vals: Vec<u8>,
    /// `code[len-1][idx]` canonical code per length; parallel to vals.
    pub codes: Vec<u16>,
    /// `mincode`/`maxcode` per length (1..=16); i16::MIN marks empty lengths.
    pub mincode: [i32; 17],
    pub maxcode: [i32; 17],
    /// `valoffset` per length: index into vals of the first code of that
    /// length minus... (libjpeg semantics: value = vals[valptr[len] + code - mincode[len]]).
    pub valptr: [i32; 17],
}

impl HuffTable {
    pub fn build(bits: &[u8; 16], vals: &[u8]) -> Option<HuffTable> {
        let mut mincode = [0i32; 17];
        let mut maxcode = [-1i32; 17];
        let mut valptr = [0i32; 17];
        let mut codes = Vec::with_capacity(vals.len());
        let mut code: u32 = 0;
        let mut k: i32 = 0;
        for len in 1..=16usize {
            let count = bits[len - 1] as usize;
            // Over-subscribed or referencing more vals than provided → invalid.
            if code + count as u32 > (1u32 << len) {
                return None;
            }
            if k as usize + count > vals.len() {
                return None;
            }
            valptr[len] = k;
            mincode[len] = code as i32;
            for _ in 0..count {
                codes.push(code as u16);
                code += 1;
                k += 1;
            }
            maxcode[len] = code as i32 - 1;
            code <<= 1;
        }
        // Incomplete tables (unassigned codes) are tolerated, like libjpeg.
        Some(HuffTable {
            bits: *bits,
            vals: vals.to_vec(),
            codes,
            mincode,
            maxcode,
            valptr,
        })
    }
}

/// Number of bits needed for `|v|` (Huffman "size"/category): 0 → 0,
/// matching libjpeg's `while (temp) { nbits++; temp >>= 1; }`.
#[inline]
pub fn bit_category(mut v: i32) -> u8 {
    if v < 0 {
        v = -v;
    }
    let mut n = 0u8;
    while v != 0 {
        n += 1;
        v >>= 1;
    }
    n
}

/// JPEG `EXTEND(v, s)`: map the `s`-bit magnitude to the signed value.
#[inline]
pub fn extend(v: i32, s: u8) -> i32 {
    // v < 1<<(s-1)  ?  v + (-1<<s) + 1  :  v
    if s == 0 {
        return 0;
    }
    if v < (1 << (s - 1)) {
        v - (1 << s) + 1
    } else {
        v
    }
}

// --------------------------------------------------------------------------- //
// Fixed-point DCT constants (libjpeg CONST_BITS=13, PASS1_BITS=2)
// --------------------------------------------------------------------------- //

pub const CONST_BITS: u32 = 13;
pub const PASS1_BITS: u32 = 2;

pub const FIX_0_298631336: i64 = 2446;
pub const FIX_0_390180644: i64 = 3196;
pub const FIX_0_541196100: i64 = 4433;
pub const FIX_0_765366865: i64 = 6270;
pub const FIX_0_899976223: i64 = 7373;
pub const FIX_1_175875602: i64 = 9633;
pub const FIX_1_501321110: i64 = 12299;
pub const FIX_1_847759065: i64 = 15137;
pub const FIX_1_961570560: i64 = 16069;
pub const FIX_2_053119869: i64 = 16819;
pub const FIX_2_562915447: i64 = 20995;
pub const FIX_3_072711026: i64 = 25172;

/// `DESCALE(x, n)` from libjpeg: add rounding half and arithmetic-shift right.
#[inline(always)]
pub fn descale(x: i64, n: u32) -> i64 {
    (x + (1i64 << (n - 1))) >> n
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zigzag_roundtrip() {
        let z = zigzag_of_natural();
        for k in 0..64usize {
            assert_eq!(NATURAL_OF_ZIGZAG[z[k] as usize], k);
        }
    }

    #[test]
    fn std_tables_are_monotone_and_bounded() {
        for t in [STD_QUANT_LUMA_ZZ, STD_QUANT_CHROMA_ZZ] {
            assert!(t.iter().all(|&v| (1..=255).contains(&v)));
        }
        // quality 100 → all ones; quality 50 → unscaled (natural order)
        assert!(std_luma_quality(100).iter().all(|&v| v == 1));
        assert_eq!(std_luma_quality(50), natural_from_zigzag(&STD_QUANT_LUMA_ZZ));
        assert_eq!(std_chroma_quality(50), natural_from_zigzag(&STD_QUANT_CHROMA_ZZ));
    }

    #[test]
    fn huffman_std_tables_build() {
        assert!(HuffTable::build(&DC_LUMA_BITS, &DC_LUMA_VALS).is_some());
        assert!(HuffTable::build(&AC_LUMA_BITS, &AC_LUMA_VALS).is_some());
        assert!(HuffTable::build(&DC_CHROMA_BITS, &DC_CHROMA_VALS).is_some());
        assert!(HuffTable::build(&AC_CHROMA_BITS, &AC_CHROMA_VALS).is_some());
        // oversubscribed table must be rejected
        assert!(HuffTable::build(
            &[8, 8, 8, 8, 8, 8, 8, 8, 0, 0, 0, 0, 0, 0, 0, 0],
            &[0u8; 64]
        )
        .is_none());
    }

    #[test]
    fn categories() {
        assert_eq!(bit_category(0), 0);
        assert_eq!(bit_category(-1), 1);
        assert_eq!(bit_category(1), 1);
        assert_eq!(bit_category(-2), 2);
        assert_eq!(bit_category(3), 2);
        assert_eq!(bit_category(255), 8);
        assert_eq!(extend(0b0, 1), -1);
        assert_eq!(extend(0b1, 1), 1);
        assert_eq!(extend(0b00, 2), -3);
        assert_eq!(extend(0b11, 2), 3);
    }
}
