//! Optional `channel.json` companion absorption (C1 scope item 2).
//!
//! The companion lives next to the KFBF as `<name>_kfbf/Annotations/channel.json`
//! and carries a display window (`lower`/`upper`) per channel. It is an
//! OPTIONAL input: when present we absorb the display window into the result
//! metadata; the KFBF **body wins on conflict** for names/colors/exposures
//! (only fields the body lacks can be filled in). The file is vendor-fixed
//! (flat array, 8 known keys), so a purpose-built parser keeps serde out of
//! the wasm artifact — rationale recorded in the C1 report.

use crate::error::{CoreError, CoreResult};

#[derive(Debug, Clone)]
pub struct CompanionChannel {
    pub channel_name: String,
    /// 1-based index as written by the scanner.
    pub channel_index: u32,
    pub channel_color: Option<(u32, u32, u32)>,
    pub lower: f64,
    pub upper: f64,
    pub gamma: f64,
    pub show: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Companion {
    pub channels: Vec<CompanionChannel>,
    /// Non-fatal notes (unknown keys, clamped values…).
    pub notes: Vec<String>,
}

/// Parse the vendor channel.json. Unknown structure → typed metadata error;
/// the converter treats companion failures as ignorable (it is optional).
pub fn parse_channel_json(data: &[u8]) -> CoreResult<Companion> {
    let mut p = Parser { b: data, i: 0 };
    p.ws();
    if !p.eat(b'[') {
        return Err(CoreError::metadata("channel.json 不是数组"));
    }
    let mut out = Companion::default();
    loop {
        p.ws();
        if p.eat(b']') {
            break;
        }
        if !p.eat(b'{') {
            return Err(CoreError::metadata("channel.json 条目不是对象"));
        }
        let mut ch = CompanionChannel {
            channel_name: String::new(),
            channel_index: 0,
            channel_color: None,
            lower: f64::NAN,
            upper: f64::NAN,
            gamma: f64::NAN,
            show: true,
        };
        loop {
            p.ws();
            if p.eat(b'}') {
                break;
            }
            let key = p.string()?;
            p.ws();
            if !p.eat(b':') {
                return Err(CoreError::metadata("channel.json 缺冒号"));
            }
            p.ws();
            match key.as_str() {
                "channelName" => ch.channel_name = p.string()?,
                "channelIndex" => ch.channel_index = p.number()? as u32,
                "channelColor" => {
                    let s = p.string()?;
                    ch.channel_color = parse_hex_color(&s);
                }
                "lower" => ch.lower = p.number()?,
                "upper" => ch.upper = p.number()?,
                "gamma" => ch.gamma = p.number()?,
                "show" => ch.show = p.bool()?,
                _ => {
                    p.skip_value()?;
                    out.notes.push(format!("忽略未知键 {key:?}"));
                }
            }
            p.ws();
            if !p.eat(b',') {
                p.ws();
                if p.eat(b'}') {
                    break;
                }
                return Err(CoreError::metadata("channel.json 对象格式错"));
            }
        }
        if ch.channel_name.is_empty() || ch.lower.is_nan() || ch.upper.is_nan() {
            return Err(CoreError::metadata("channel.json 条目缺 name/lower/upper"));
        }
        if !ch.lower.is_finite() || !ch.upper.is_finite() || ch.upper < ch.lower {
            out.notes.push(format!(
                "通道 {:?} 显示窗 [{},{}] 非法，忽略",
                ch.channel_name, ch.lower, ch.upper
            ));
        } else {
            out.channels.push(ch);
        }
        p.ws();
        if p.eat(b',') {
            continue;
        }
        p.ws();
        if !p.eat(b']') {
            return Err(CoreError::metadata("channel.json 数组格式错"));
        }
        break;
    }
    Ok(out)
}

fn parse_hex_color(s: &str) -> Option<(u32, u32, u32)> {
    let s = s.strip_prefix('#')?;
    if s.len() != 6 {
        return None;
    }
    let v = u32::from_str_radix(s, 16).ok()?;
    Some(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && (self.b[self.i] as char).is_ascii_whitespace() {
            self.i += 1;
        }
    }
    fn eat(&mut self, c: u8) -> bool {
        if self.i < self.b.len() && self.b[self.i] == c {
            self.i += 1;
            true
        } else {
            false
        }
    }
    fn string(&mut self) -> CoreResult<String> {
        if !self.eat(b'"') {
            return Err(CoreError::metadata("channel.json 需字符串"));
        }
        let mut out = String::new();
        while self.i < self.b.len() {
            match self.b[self.i] {
                b'"' => {
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.i += 1;
                    if self.i >= self.b.len() {
                        break;
                    }
                    let c = match self.b[self.i] {
                        b'n' => '\n',
                        b't' => '\t',
                        b'r' => '\r',
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'u' => {
                            // \uXXXX（仅 BMP，够用）
                            if self.i + 4 >= self.b.len() {
                                return Err(CoreError::metadata("channel.json \\u 转义截断"));
                            }
                            let hex = std::str::from_utf8(
                                &self.b[self.i + 1..self.i + 5],
                            )
                            .map_err(|_| CoreError::metadata("channel.json \\u 转义非法"))?;
                            let v = u32::from_str_radix(hex, 16)
                                .map_err(|_| CoreError::metadata("channel.json \\u 转义非法"))?;
                            self.i += 4;
                            char::from_u32(v).unwrap_or('\u{FFFD}')
                        }
                        _ => '\u{FFFD}',
                    };
                    out.push(c);
                    self.i += 1;
                }
                c => {
                    // UTF-8 passthrough
                    let start = self.i;
                    let len = utf8_len(c);
                    let end = (start + len).min(self.b.len());
                    out.push_str(&String::from_utf8_lossy(&self.b[start..end]));
                    self.i = end;
                }
            }
        }
        Err(CoreError::metadata("channel.json 字符串未闭合"))
    }
    fn number(&mut self) -> CoreResult<f64> {
        let start = self.i;
        while self.i < self.b.len()
            && matches!(self.b[self.i], b'-' | b'+' | b'.' | b'0'..=b'9' | b'e' | b'E')
        {
            self.i += 1;
        }
        std::str::from_utf8(&self.b[start..self.i])
            .ok()
            .and_then(|s| s.parse::<f64>().ok())
            .ok_or_else(|| CoreError::metadata("channel.json 数值非法"))
    }
    fn bool(&mut self) -> CoreResult<bool> {
        if self.b[self.i..].starts_with(b"true") {
            self.i += 4;
            Ok(true)
        } else if self.b[self.i..].starts_with(b"false") {
            self.i += 5;
            Ok(false)
        } else {
            Err(CoreError::metadata("channel.json 需布尔值"))
        }
    }
    fn skip_value(&mut self) -> CoreResult<()> {
        self.ws();
        match self.b.get(self.i) {
            Some(b'"') => {
                self.string()?;
            }
            Some(b'{') | Some(b'[') => {
                let open = self.b[self.i];
                let close = if open == b'{' { b'}' } else { b']' };
                let mut depth = 0usize;
                while self.i < self.b.len() {
                    let c = self.b[self.i];
                    if c == b'"' {
                        self.string()?;
                        continue;
                    }
                    if c == open {
                        depth += 1;
                    } else if c == close {
                        depth -= 1;
                        if depth == 0 {
                            self.i += 1;
                            return Ok(());
                        }
                    }
                    self.i += 1;
                }
                return Err(CoreError::metadata("channel.json 嵌套值未闭合"));
            }
            _ => {
                self.number()?;
            }
        }
        Ok(())
    }
}

fn utf8_len(first: u8) -> usize {
    match first {
        0x00..=0x7F => 1,
        0xC0..=0xDF => 2,
        0xE0..=0xEF => 3,
        _ => 4,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_vendor_shape() {
        let data = b"[\n {\"channelName\":\"DAPI\",\"channelIndex\":1,\"channelColor\":\"#0000E5\",\"lower\":0,\"upper\":178,\"offsetX\":0,\"offsetY\":0,\"gamma\":1,\"show\":true},\n {\"channelName\":\"480\",\"channelIndex\":2,\"channelColor\":\"#80FFFF\",\"lower\":14,\"upper\":203,\"gamma\":1.5,\"show\":false}\n]";
        let c = parse_channel_json(data).unwrap();
        assert_eq!(c.channels.len(), 2);
        assert_eq!(c.channels[0].channel_name, "DAPI");
        assert_eq!(c.channels[0].channel_color, Some((0, 0, 229)));
        assert_eq!(c.channels[0].lower, 0.0);
        assert_eq!(c.channels[0].upper, 178.0);
        assert!(c.channels[0].show);
        assert!(!c.channels[1].show);
        assert_eq!(c.channels[1].gamma, 1.5);
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse_channel_json(b"{").is_err());
        assert!(parse_channel_json(b"[1,2]").is_err());
    }
}
