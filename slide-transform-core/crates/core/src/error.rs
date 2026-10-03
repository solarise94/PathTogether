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
    ConversionTimeout,
    ConversionDiskLow,
    /// C1 extension: the transform plan demanded pixel losslessness but the
    /// input requires an edge-tile re-encode (typed, explicit rejection —
    /// the Python oracle has no policy concept and always re-encodes).
    PixelPolicyViolation,
    /// Trait-level IO failure (native file error, or a host-provided IO
    /// adapter failing). The Python oracle raises OSError instead; code kept
    /// separate so callers can distinguish infrastructure from format.
    Io,
    /// Review §1: the input's metadata/working-set estimate exceeds the
    /// caller's resource budget (native `--memory-budget`, browser resource
    /// profile). Stable code is the one the tool page already maps
    /// (`engine.js ERROR_CODES.RESOURCE_PROFILE_INSUFFICIENT`).
    ResourceLimitExceeded,
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
            ErrorCode::ConversionTimeout => "conversion_timeout",
            ErrorCode::ConversionDiskLow => "conversion_disk_low",
            ErrorCode::PixelPolicyViolation => "pixel_policy_violation",
            ErrorCode::Io => "io_error",
            ErrorCode::ResourceLimitExceeded => "resource_profile_insufficient",
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
        Self::new(ErrorCode::ConversionValidationFailed, message.into())
    }
    pub fn timeout(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::ConversionTimeout, message.into())
    }
    pub fn disk_low(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::ConversionDiskLow, message.into())
    }
    pub fn policy(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::PixelPolicyViolation, message.into())
    }
    pub fn io(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::Io, message)
    }
    pub fn resource_limit(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::ResourceLimitExceeded, message)
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
