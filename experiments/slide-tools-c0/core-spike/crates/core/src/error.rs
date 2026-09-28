//! Typed error contract, mirroring the Python oracle's stable error codes
//! (`kfb/errors.py::KFB_ERROR_CODES`). The Rust spike keeps the same code
//! strings so differential tests can compare failure classes.

use std::fmt;

/// Stable machine-readable error codes (1:1 with the Python contract).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorCode {
    UnsupportedKfbVariant,
    InvalidKfbHeader,
    InvalidTileIndex,
    TilePayloadOutOfBounds,
    JpegDecodeFailed,
    MetadataMissingRequired,
    ConversionOutputTooLarge,
    ConversionValidationFailed,
    /// Trait-level IO failure (native file error, or a host-provided IO
    /// adapter failing). The Python oracle raises OSError instead; code kept
    /// separate so callers can distinguish infrastructure from format.
    Io,
}

impl ErrorCode {
    pub fn stable_code(&self) -> &'static str {
        match self {
            ErrorCode::UnsupportedKfbVariant => "unsupported_kfb_variant",
            ErrorCode::InvalidKfbHeader => "invalid_kfb_header",
            ErrorCode::InvalidTileIndex => "invalid_tile_index",
            ErrorCode::TilePayloadOutOfBounds => "tile_payload_out_of_bounds",
            ErrorCode::JpegDecodeFailed => "jpeg_decode_failed",
            ErrorCode::MetadataMissingRequired => "metadata_missing_required",
            ErrorCode::ConversionOutputTooLarge => "conversion_output_too_large",
            ErrorCode::ConversionValidationFailed => "conversion_validation_failed",
            ErrorCode::Io => "io_error",
        }
    }
}

#[derive(Debug, Clone)]
pub struct CoreError {
    pub code: ErrorCode,
    pub message: String,
}

pub type CoreResult<T> = Result<T, CoreError>;

impl CoreError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        CoreError { code, message: message.into() }
    }

    pub fn variant(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::UnsupportedKfbVariant, message)
    }
    pub fn header(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::InvalidKfbHeader, message)
    }
    pub fn index(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::InvalidTileIndex, message)
    }
    pub fn oob(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::TilePayloadOutOfBounds, message)
    }
    pub fn jpeg(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::JpegDecodeFailed, message)
    }
    pub fn metadata(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::MetadataMissingRequired, message)
    }
    pub fn too_large(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::ConversionOutputTooLarge, message)
    }
    pub fn validation(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::ConversionValidationFailed, message)
    }
    pub fn io(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::Io, message)
    }
}

impl fmt::Display for CoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.stable_code(), self.message)
    }
}

impl std::error::Error for CoreError {}

impl From<std::io::Error> for CoreError {
    fn from(e: std::io::Error) -> Self {
        CoreError::io(format!("io: {e}"))
    }
}
