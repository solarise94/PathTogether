//! Transform plan: the versioned input of every conversion job (C1 item 5).
//! A `TransformPlan` pins identities, the output profile, the pixel policy
//! and the resource limits; the converter refuses (typed error) rather than
//! silently deviating.

use crate::CORE_VERSION;

pub const PLAN_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputProfile {
    /// Classic multi-IFD JPEG tiled BigTIFF pyramid (brightfield; byte
    /// layout of `kfb/converter.py`).
    ClassicJpegBigTiff,
    /// Multi-channel OME-BigTIFF with SubIFD pyramid (fluorescence;
    /// `kfb/converter_fl.py` structure, ExposureTime on `<Plane>`).
    OmeBigTiffSubifd,
    /// Brightfield RGB OME-BigTIFF: one interleaved 3-sample plane (the
    /// source JPEG YCbCr tiles, copied), reduced levels only as SubIFDs of
    /// the full-resolution IFD.
    OmeBigTiffRgbSubifd,
}

impl OutputProfile {
    /// Stable wire id (CLI `--profile`, browser job records, journals).
    pub fn id(self) -> &'static str {
        match self {
            OutputProfile::ClassicJpegBigTiff => "bf-classic",
            OutputProfile::OmeBigTiffRgbSubifd => "bf-ome",
            OutputProfile::OmeBigTiffSubifd => "fl-ome",
        }
    }

    pub fn from_id(id: &str) -> Option<Self> {
        match id {
            "bf-classic" => Some(OutputProfile::ClassicJpegBigTiff),
            "bf-ome" => Some(OutputProfile::OmeBigTiffRgbSubifd),
            "fl-ome" => Some(OutputProfile::OmeBigTiffSubifd),
            _ => None,
        }
    }

    pub fn is_brightfield(self) -> bool {
        !matches!(self, OutputProfile::OmeBigTiffSubifd)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PixelPolicy {
    /// Allow decode→re-encode of edge/cropped tiles exactly like the oracle
    /// (white canvas brightfield / black canvas fluorescence, source
    /// quantization tables reused when available).
    AllowEdgeReencode,
    /// Reject any input whose output would require a lossy re-encode
    /// (`pixel_policy_violation`) — full tiles are byte-copied only.
    StrictLossless,
}

#[derive(Debug, Clone)]
pub struct ResourceLimits {
    /// Wall-clock budget in seconds (checked between tiles).
    pub timeout_seconds: f64,
    /// Hard cap on emitted output bytes.
    pub max_output_bytes: u64,
    /// Free-space floor the host must verify before starting (checked by
    /// the native CLI; the core reports it in the plan echo).
    pub min_free_bytes: u64,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        ResourceLimits {
            timeout_seconds: 600.0,
            max_output_bytes: 64 * 1024 * 1024 * 1024,
            min_free_bytes: 256 * 1024 * 1024,
        }
    }
}

/// Identity of the input (echoed into the report; `sha256` may be omitted
/// by streaming callers and computed by the host).
#[derive(Debug, Clone, Default)]
pub struct InputIdentity {
    pub name: String,
    pub size: u64,
    pub sha256: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TransformPlan {
    pub plan_version: u32,
    pub core_version: String,
    pub input: InputIdentity,
    pub profile: OutputProfile,
    pub pixel_policy: PixelPolicy,
    pub limits: ResourceLimits,
}

impl TransformPlan {
    /// Plan for a brightfield conversion (classic pyramid).
    pub fn brightfield(input: InputIdentity) -> Self {
        TransformPlan {
            plan_version: PLAN_VERSION,
            core_version: CORE_VERSION.to_string(),
            input,
            profile: OutputProfile::ClassicJpegBigTiff,
            pixel_policy: PixelPolicy::AllowEdgeReencode,
            limits: ResourceLimits::default(),
        }
    }

    /// Plan for a brightfield conversion to the RGB OME-BigTIFF profile.
    pub fn brightfield_ome(input: InputIdentity) -> Self {
        TransformPlan { profile: OutputProfile::OmeBigTiffRgbSubifd, ..Self::brightfield(input) }
    }

    /// Plan for a fluorescence conversion (OME-SubIFD).
    pub fn fluorescence(input: InputIdentity) -> Self {
        TransformPlan {
            plan_version: PLAN_VERSION,
            core_version: CORE_VERSION.to_string(),
            input,
            profile: OutputProfile::OmeBigTiffSubifd,
            pixel_policy: PixelPolicy::AllowEdgeReencode,
            limits: ResourceLimits::default(),
        }
    }

    pub fn with_policy(mut self, policy: PixelPolicy) -> Self {
        self.pixel_policy = policy;
        self
    }

    pub fn with_limits(mut self, limits: ResourceLimits) -> Self {
        self.limits = limits;
        self
    }
}
