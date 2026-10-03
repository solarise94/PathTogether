//! Checked pre-allocation byte accounting for the MRXS adapter
//! (independent review §1: probe/convert allocations must be bounded by the
//! resource profile BEFORE memory is committed).
//!
//! The caller hands the adapter the actual resource budget of its host:
//!
//! - native CLI: `--memory-budget BYTES` (conservative default = the
//!   browser `saver` profile's 192 MiB);
//! - wasm/browser: the active resource profile's `budgetBytes`
//!   (`worker.js` passes it to `probeBundle` / `convertProfileEncodedBundle`).
//!
//! Before every large allocation (position table, image records, placement
//! sort copies, CSR bucket arrays, image cache, JPEG decode pixels) the
//! adapter computes an overflow-checked byte estimate and `charge`s it; an
//! over-budget charge is a **stable typed refusal** (stable code
//! `resource_profile_insufficient` — the code the tool page already maps)
//! raised BEFORE the allocation, never an OOM kill after it.

use crate::error::{CoreError, CoreResult};

/// The browser `saver` resource profile's budget (engine.js PROFILES.saver) —
/// also the conservative default when a host does not pass an explicit
/// budget.
pub const SAVER_BUDGET_BYTES: u64 = 192 * 1024 * 1024;

/// Reserve subtracted from the host budget before the adapter counts its own
/// working set: decoder transient slack beyond the accounted planes, allocator
/// overhead/fragmentation and the process baseline. The adapter refuses when
/// its checked working set would exceed `budget − reserve`.
pub const ADAPTER_RESERVE_BYTES: u64 = 24 * 1024 * 1024;

/// Running byte account of the adapter's working set. Charges are cumulative;
/// per-level/per-entry allocations that are freed again are `release`d so the
/// account tracks the peak, not the sum, of temporary phases.
#[derive(Debug, Clone)]
pub struct MemBudget {
    cap: u64,
    used: u64,
}

impl MemBudget {
    /// Adapter account for a host budget of `total_bytes` (the reserve is
    /// subtracted here).
    pub fn host(total_bytes: u64) -> Self {
        MemBudget { cap: total_bytes.saturating_sub(ADAPTER_RESERVE_BYTES), used: 0 }
    }

    /// Account at the saver profile's budget (the conservative default).
    pub fn saver() -> Self {
        MemBudget::host(SAVER_BUDGET_BYTES)
    }

    /// Direct cap (tests). No reserve subtracted.
    pub fn with_cap(cap: u64) -> Self {
        MemBudget { cap, used: 0 }
    }

    pub fn used(&self) -> u64 {
        self.used
    }

    pub fn cap(&self) -> u64 {
        self.cap
    }

    pub fn remaining(&self) -> u64 {
        self.cap.saturating_sub(self.used)
    }

    /// Overflow-checked estimate `count × elem_bytes`, charged to the account.
    /// Over budget ⇒ typed `resource_profile_insufficient` naming `what` and
    /// the numbers — BEFORE the caller allocates.
    pub fn charge_mul(&mut self, count: u64, elem_bytes: u64, what: &str) -> CoreResult<u64> {
        let bytes = count
            .checked_mul(elem_bytes)
            .ok_or_else(|| self.refusal(what, u64::MAX))?;
        self.charge(bytes, what)?;
        Ok(bytes)
    }

    /// Charge `bytes` against the account; refuse when the working set would
    /// exceed the cap.
    pub fn charge(&mut self, bytes: u64, what: &str) -> CoreResult<()> {
        self.used = self.used.saturating_add(bytes);
        if self.used > self.cap {
            return Err(self.refusal(what, bytes));
        }
        Ok(())
    }

    /// A previously charged estimate turned out to be `actual`: true the
    /// account up or down (called after the real size is known).
    pub fn reconcile(&mut self, estimate: u64, actual: u64, what: &str) -> CoreResult<()> {
        if actual > estimate {
            self.charge(actual - estimate, what)
        } else {
            self.used -= estimate - actual;
            Ok(())
        }
    }

    /// `bytes` of temporaries were freed (per-level structures, evicted cache
    /// entries, finished decode buffers).
    pub fn release(&mut self, bytes: u64) {
        self.used = self.used.saturating_sub(bytes);
    }

    fn refusal(&self, what: &str, bytes: u64) -> CoreError {
        CoreError::resource_limit(format!(
            "内存预算不足（预算 {} B，已计 {used} B）：{what} 还需 {bytes} B。\
             输入的元数据规模超出当前资源档位，已在其分配前拒绝",
            self.cap,
            used = self.used,
        ))
    }
}

/// Element-size constants used by the estimates (kept next to the account so
/// the reasoning lives in one place).
pub mod elem {
    /// `ImageRef` (x/y/member/offset/length) plus geometric growth slack.
    pub const IMAGE_REF: u64 = 64;
    /// `(i64, i64)` camera position.
    pub const POSITION: u64 = 16;
    /// One raw position-buffer entry (9 B) as read from the member.
    pub const POSITION_RAW: u64 = 9;
    /// One `active` mark.
    pub const ACTIVE: u64 = 1;
    /// One index-page visited-set entry (page chain loop guard).
    pub const PAGE_VISIT: u64 = 64;
}
