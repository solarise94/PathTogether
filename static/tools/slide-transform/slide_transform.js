/* @ts-self-types="./slide_transform.d.ts" */

/**
 * Host capability flags (call before convert): bit 0 = stHostReadInto.
 * @param {boolean} read_into
 */
export function configure(read_into) {
    wasm.configure(read_into);
}

/**
 * Run a conversion writing to the host sink. `strict_lossless` toggles the
 * pixel policy; `channel_json` may be empty (no companion).
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @returns {string}
 */
export function convert(strict_lossless, channel_json) {
    let deferred2_0;
    let deferred2_1;
    try {
        const ptr0 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ret = wasm.convert(strict_lossless, ptr0, len0);
        deferred2_0 = ret[0];
        deferred2_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred2_0, deferred2_1, 1);
    }
}

/**
 * Run a conversion with an explicit output profile id (`bf-classic`,
 * `bf-ome`, `fl-ome`; empty = the input's pre-profile default). Encoding is
 * preserve-source-v1 (pre-U3 behaviour kept bit-for-bit).
 * @param {string} profile
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @returns {string}
 */
export function convertProfile(profile, strict_lossless, channel_json) {
    let deferred3_0;
    let deferred3_1;
    try {
        const ptr0 = passStringToWasm0(profile, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ret = wasm.convertProfile(ptr0, len0, strict_lossless, ptr1, len1);
        deferred3_0 = ret[0];
        deferred3_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred3_0, deferred3_1, 1);
    }
}

/**
 * Run a conversion with explicit output AND encoding profile ids (U3).
 * `encoding`: `preserve-source-v1` (default) or `compact-jpeg-v1`
 * (brightfield only).
 * @param {string} profile
 * @param {string} encoding
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @returns {string}
 */
export function convertProfileEncoded(profile, encoding, strict_lossless, channel_json) {
    let deferred4_0;
    let deferred4_1;
    try {
        const ptr0 = passStringToWasm0(profile, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(encoding, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ptr2 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len2 = WASM_VECTOR_LEN;
        const ret = wasm.convertProfileEncoded(ptr0, len0, ptr1, len1, strict_lossless, ptr2, len2);
        deferred4_0 = ret[0];
        deferred4_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred4_0, deferred4_1, 1);
    }
}

/**
 * Bundle conversion with explicit output AND encoding profile ids (F3).
 * `budget_bytes`: the browser resource profile's budget (review §1;
 * `undefined` = the conservative saver default).
 * @param {string} profile
 * @param {string} encoding
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @param {number | null} [budget_bytes]
 * @returns {string}
 */
export function convertProfileEncodedBundle(profile, encoding, strict_lossless, channel_json, budget_bytes) {
    let deferred4_0;
    let deferred4_1;
    try {
        const ptr0 = passStringToWasm0(profile, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(encoding, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ptr2 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len2 = WASM_VECTOR_LEN;
        const ret = wasm.convertProfileEncodedBundle(ptr0, len0, ptr1, len1, strict_lossless, ptr2, len2, !isLikeNone(budget_bytes), isLikeNone(budget_bytes) ? 0 : budget_bytes);
        deferred4_0 = ret[0];
        deferred4_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred4_0, deferred4_1, 1);
    }
}

/**
 * Resume a conversion from a checkpoint state (the same JSON
 * `stHostCheckpoint` emits; journal-recorded by the runner).
 * @param {string} resume_json
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @returns {string}
 */
export function convertResume(resume_json, strict_lossless, channel_json) {
    let deferred3_0;
    let deferred3_1;
    try {
        const ptr0 = passStringToWasm0(resume_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ret = wasm.convertResume(ptr0, len0, strict_lossless, ptr1, len1);
        deferred3_0 = ret[0];
        deferred3_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred3_0, deferred3_1, 1);
    }
}

/**
 * Resume under an explicit output profile; refused when the checkpoint
 * state was committed under a different one.
 * @param {string} resume_json
 * @param {string} profile
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @returns {string}
 */
export function convertResumeProfile(resume_json, profile, strict_lossless, channel_json) {
    let deferred4_0;
    let deferred4_1;
    try {
        const ptr0 = passStringToWasm0(resume_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(profile, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ptr2 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len2 = WASM_VECTOR_LEN;
        const ret = wasm.convertResumeProfile(ptr0, len0, ptr1, len1, strict_lossless, ptr2, len2);
        deferred4_0 = ret[0];
        deferred4_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred4_0, deferred4_1, 1);
    }
}

/**
 * Resume under explicit output AND encoding profiles (U3); refused when the
 * checkpoint state was committed under a different combination — including
 * a compact request against a legacy (preserve, no field) state.
 * @param {string} resume_json
 * @param {string} profile
 * @param {string} encoding
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @returns {string}
 */
export function convertResumeProfileEncoded(resume_json, profile, encoding, strict_lossless, channel_json) {
    let deferred5_0;
    let deferred5_1;
    try {
        const ptr0 = passStringToWasm0(resume_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(profile, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ptr2 = passStringToWasm0(encoding, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len2 = WASM_VECTOR_LEN;
        const ptr3 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len3 = WASM_VECTOR_LEN;
        const ret = wasm.convertResumeProfileEncoded(ptr0, len0, ptr1, len1, ptr2, len2, strict_lossless, ptr3, len3);
        deferred5_0 = ret[0];
        deferred5_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred5_0, deferred5_1, 1);
    }
}

/**
 * Bundle resume under explicit profiles (F3); refused when the checkpoint
 * state was committed under another profile/encoding/adapter combination.
 * @param {string} resume_json
 * @param {string} profile
 * @param {string} encoding
 * @param {boolean} strict_lossless
 * @param {string} channel_json
 * @param {number | null} [budget_bytes]
 * @returns {string}
 */
export function convertResumeProfileEncodedBundle(resume_json, profile, encoding, strict_lossless, channel_json, budget_bytes) {
    let deferred5_0;
    let deferred5_1;
    try {
        const ptr0 = passStringToWasm0(resume_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(profile, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ptr2 = passStringToWasm0(encoding, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len2 = WASM_VECTOR_LEN;
        const ptr3 = passStringToWasm0(channel_json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len3 = WASM_VECTOR_LEN;
        const ret = wasm.convertResumeProfileEncodedBundle(ptr0, len0, ptr1, len1, ptr2, len2, strict_lossless, ptr3, len3, !isLikeNone(budget_bytes), isLikeNone(budget_bytes) ? 0 : budget_bytes);
        deferred5_0 = ret[0];
        deferred5_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred5_0, deferred5_1, 1);
    }
}

/**
 * @returns {string}
 */
export function coreVersion() {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.coreVersion();
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}

export function enableCheckpoint() {
    wasm.enableCheckpoint();
}

/**
 * Piggyback a sha256 over every source byte read through `ByteSource`
 * during the next conversion (identity capture without an extra pass).
 */
export function enableSourceHash() {
    wasm.enableSourceHash();
}

/**
 * Re-open + validate the finished output (streamed sha256 + structural
 * IFD walk) through the host read-back callbacks. Only a passing result
 * may be marked `ready`. `expect_ifd` is the converter's `ifd_count` (main
 * chain + SubIFDs, every profile); 0 skips the equality.
 * @param {number} expect_ifd
 * @returns {string}
 */
export function finalizeValidate(expect_ifd) {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.finalizeValidate(expect_ifd);
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}

/**
 * Probe the input through host reads; returns a JSON string. Includes the
 * C2 disk-precheck estimate (`estimate.output_upper_bound_bytes` etc.).
 * @returns {string}
 */
export function probe() {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.probe();
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}

/**
 * Probe a bundle input (F3 MRXS) through the bundle host callbacks.
 * `budget_bytes` is the browser resource profile's budget (review §1): the
 * probe refuses with `resource_profile_insufficient` when its metadata
 * working set would exceed it — before any large allocation. `undefined`
 * keeps the conservative saver default (192 MiB).
 * @param {number | null} [budget_bytes]
 * @returns {string}
 */
export function probeBundle(budget_bytes) {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.probeBundle(!isLikeNone(budget_bytes), isLikeNone(budget_bytes) ? 0 : budget_bytes);
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}

/**
 * One dedicated pass: sha256 of the whole source through bounded host
 * reads (resume identity verification; hashing flag stays off).
 * @returns {string}
 */
export function sha256Source() {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.sha256Source();
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}

/**
 * @returns {string}
 */
export function sourceSha256() {
    let deferred1_0;
    let deferred1_1;
    try {
        const ret = wasm.sourceSha256();
        deferred1_0 = ret[0];
        deferred1_1 = ret[1];
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
}
function __wbg_get_imports() {
    const import0 = {
        __proto__: null,
        __wbg___wbindgen_debug_string_4687d8d8c2017d52: function(arg0, arg1) {
            const ret = debugString(arg1);
            const ptr1 = passStringToWasm0(ret, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
            const len1 = WASM_VECTOR_LEN;
            getDataViewMemory0().setInt32(arg0 + 4 * 1, len1, true);
            getDataViewMemory0().setInt32(arg0 + 4 * 0, ptr1, true);
        },
        __wbg___wbindgen_is_null_e343b7d08827ba72: function(arg0) {
            const ret = arg0 === null;
            return ret;
        },
        __wbg___wbindgen_is_undefined_8865fb403f8fe9d8: function(arg0) {
            const ret = arg0 === undefined;
            return ret;
        },
        __wbg___wbindgen_string_get_0380ccaa2f57f0d9: function(arg0, arg1) {
            const obj = arg1;
            const ret = typeof(obj) === 'string' ? obj : undefined;
            var ptr1 = isLikeNone(ret) ? 0 : passStringToWasm0(ret, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
            var len1 = WASM_VECTOR_LEN;
            getDataViewMemory0().setInt32(arg0 + 4 * 1, len1, true);
            getDataViewMemory0().setInt32(arg0 + 4 * 0, ptr1, true);
        },
        __wbg_stHostBundleCount_524c6cefb42f96b8: function() { return handleError(function () {
            const ret = stHostBundleCount();
            return ret;
        }, arguments); },
        __wbg_stHostBundleName_ecf61dd439495581: function() { return handleError(function (arg0, arg1) {
            const ret = stHostBundleName(arg1 >>> 0);
            const ptr1 = passStringToWasm0(ret, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
            const len1 = WASM_VECTOR_LEN;
            getDataViewMemory0().setInt32(arg0 + 4 * 1, len1, true);
            getDataViewMemory0().setInt32(arg0 + 4 * 0, ptr1, true);
        }, arguments); },
        __wbg_stHostBundleReadInto_372254272ec2b6c4: function() { return handleError(function (arg0, arg1, arg2, arg3) {
            const ret = stHostBundleReadInto(arg0 >>> 0, arg1, arg2 >>> 0, arg3 >>> 0);
            return ret;
        }, arguments); },
        __wbg_stHostBundleSize_f720cc3bee5b40a3: function() { return handleError(function (arg0) {
            const ret = stHostBundleSize(arg0 >>> 0);
            return ret;
        }, arguments); },
        __wbg_stHostCheckpoint_43fe58aa3bff7e3d: function(arg0, arg1) {
            stHostCheckpoint(getStringFromWasm0(arg0, arg1));
        },
        __wbg_stHostFlush_5f1f06b06af48153: function() { return handleError(function () {
            const ret = stHostFlush();
            return ret;
        }, arguments); },
        __wbg_stHostOutReadInto_790a4cec44abc547: function() { return handleError(function (arg0, arg1, arg2) {
            const ret = stHostOutReadInto(arg0, arg1 >>> 0, arg2 >>> 0);
            return ret;
        }, arguments); },
        __wbg_stHostOutSize_714c50dd620b93a5: function() {
            const ret = stHostOutSize();
            return ret;
        },
        __wbg_stHostProgress_86bdd4c301669a36: function(arg0, arg1) {
            stHostProgress(getStringFromWasm0(arg0, arg1));
        },
        __wbg_stHostReadInto_b541ee07780404e4: function() { return handleError(function (arg0, arg1, arg2) {
            const ret = stHostReadInto(arg0, arg1 >>> 0, arg2 >>> 0);
            return ret;
        }, arguments); },
        __wbg_stHostRead_6e869001af79fa58: function(arg0, arg1, arg2) {
            const ret = stHostRead(arg1, arg2 >>> 0);
            const ptr1 = passArray8ToWasm0(ret, wasm.__wbindgen_malloc);
            const len1 = WASM_VECTOR_LEN;
            getDataViewMemory0().setInt32(arg0 + 4 * 1, len1, true);
            getDataViewMemory0().setInt32(arg0 + 4 * 0, ptr1, true);
        },
        __wbg_stHostScratchFlush_3dc5af3add0ed8fb: function() { return handleError(function (arg0, arg1) {
            const ret = stHostScratchFlush(getStringFromWasm0(arg0, arg1));
            return ret;
        }, arguments); },
        __wbg_stHostScratchOpen_1d640cc4eb3dc71c: function() { return handleError(function (arg0, arg1, arg2) {
            const ret = stHostScratchOpen(getStringFromWasm0(arg0, arg1), arg2 !== 0);
            return ret;
        }, arguments); },
        __wbg_stHostScratchRead_ff38836f6c536e63: function(arg0, arg1, arg2, arg3, arg4) {
            const ret = stHostScratchRead(getStringFromWasm0(arg1, arg2), arg3, arg4 >>> 0);
            const ptr1 = passArray8ToWasm0(ret, wasm.__wbindgen_malloc);
            const len1 = WASM_VECTOR_LEN;
            getDataViewMemory0().setInt32(arg0 + 4 * 1, len1, true);
            getDataViewMemory0().setInt32(arg0 + 4 * 0, ptr1, true);
        },
        __wbg_stHostScratchTruncate_546f2bfc599062d0: function() { return handleError(function (arg0, arg1, arg2) {
            const ret = stHostScratchTruncate(getStringFromWasm0(arg0, arg1), arg2);
            return ret;
        }, arguments); },
        __wbg_stHostScratchWrite_051f4ce7f279e92c: function() { return handleError(function (arg0, arg1, arg2, arg3, arg4) {
            const ret = stHostScratchWrite(getStringFromWasm0(arg0, arg1), arg2, getArrayU8FromWasm0(arg3, arg4));
            return ret;
        }, arguments); },
        __wbg_stHostSourceSize_6affc52f6d4ba990: function() {
            const ret = stHostSourceSize();
            return ret;
        },
        __wbg_stHostTruncate_aef9802e1b2826fb: function() { return handleError(function (arg0) {
            const ret = stHostTruncate(arg0);
            return ret;
        }, arguments); },
        __wbg_stHostWrite_c7345d78b6af636b: function() { return handleError(function (arg0, arg1, arg2) {
            const ret = stHostWrite(arg0, getArrayU8FromWasm0(arg1, arg2));
            return ret;
        }, arguments); },
        __wbindgen_init_externref_table: function() {
            const table = wasm.__wbindgen_externrefs;
            const offset = table.grow(4);
            table.set(0, undefined);
            table.set(offset + 0, undefined);
            table.set(offset + 1, null);
            table.set(offset + 2, true);
            table.set(offset + 3, false);
        },
    };
    return {
        __proto__: null,
        "./slide_transform_bg.js": import0,
    };
}

function addToExternrefTable0(obj) {
    const idx = wasm.__externref_table_alloc();
    wasm.__wbindgen_externrefs.set(idx, obj);
    return idx;
}

function debugString(val) {
    // primitive types
    const type = typeof val;
    if (type == 'number' || type == 'boolean' || val == null) {
        return  `${val}`;
    }
    if (type == 'string') {
        return `"${val}"`;
    }
    if (type == 'symbol') {
        const description = val.description;
        if (description == null) {
            return 'Symbol';
        } else {
            return `Symbol(${description})`;
        }
    }
    if (type == 'function') {
        const name = val.name;
        if (typeof name == 'string' && name.length > 0) {
            return `Function(${name})`;
        } else {
            return 'Function';
        }
    }
    // objects
    if (Array.isArray(val)) {
        const length = val.length;
        let debug = '[';
        if (length > 0) {
            debug += debugString(val[0]);
        }
        for(let i = 1; i < length; i++) {
            debug += ', ' + debugString(val[i]);
        }
        debug += ']';
        return debug;
    }
    // Test for built-in
    const builtInMatches = /\[object ([^\]]+)\]/.exec(toString.call(val));
    let className;
    if (builtInMatches && builtInMatches.length > 1) {
        className = builtInMatches[1];
    } else {
        // Failed to match the standard '[object ClassName]'
        return toString.call(val);
    }
    if (className == 'Object') {
        // we're a user defined class or Object
        // JSON.stringify avoids problems with cycles, and is generally much
        // easier than looping through ownProperties of `val`.
        try {
            return 'Object(' + JSON.stringify(val) + ')';
        } catch (_) {
            return 'Object';
        }
    }
    // errors
    if (val instanceof Error) {
        return `${val.name}: ${val.message}\n${val.stack}`;
    }
    // TODO we could test for more things here, like `Set`s and `Map`s.
    return className;
}

function getArrayU8FromWasm0(ptr, len) {
    ptr = ptr >>> 0;
    return getUint8ArrayMemory0().subarray(ptr / 1, ptr / 1 + len);
}

let cachedDataViewMemory0 = null;
function getDataViewMemory0() {
    if (cachedDataViewMemory0 === null || cachedDataViewMemory0.buffer.detached === true || (cachedDataViewMemory0.buffer.detached === undefined && cachedDataViewMemory0.buffer !== wasm.memory.buffer)) {
        cachedDataViewMemory0 = new DataView(wasm.memory.buffer);
    }
    return cachedDataViewMemory0;
}

function getStringFromWasm0(ptr, len) {
    return decodeText(ptr >>> 0, len);
}

let cachedUint8ArrayMemory0 = null;
function getUint8ArrayMemory0() {
    if (cachedUint8ArrayMemory0 === null || cachedUint8ArrayMemory0.byteLength === 0) {
        cachedUint8ArrayMemory0 = new Uint8Array(wasm.memory.buffer);
    }
    return cachedUint8ArrayMemory0;
}

function handleError(f, args) {
    try {
        return f.apply(this, args);
    } catch (e) {
        const idx = addToExternrefTable0(e);
        wasm.__wbindgen_exn_store(idx);
    }
}

function isLikeNone(x) {
    return x === undefined || x === null;
}

function passArray8ToWasm0(arg, malloc) {
    const ptr = malloc(arg.length * 1, 1) >>> 0;
    getUint8ArrayMemory0().set(arg, ptr / 1);
    WASM_VECTOR_LEN = arg.length;
    return ptr;
}

function passStringToWasm0(arg, malloc, realloc) {
    if (realloc === undefined) {
        const buf = cachedTextEncoder.encode(arg);
        const ptr = malloc(buf.length, 1) >>> 0;
        getUint8ArrayMemory0().subarray(ptr, ptr + buf.length).set(buf);
        WASM_VECTOR_LEN = buf.length;
        return ptr;
    }

    let len = arg.length;
    let ptr = malloc(len, 1) >>> 0;

    const mem = getUint8ArrayMemory0();

    let offset = 0;

    for (; offset < len; offset++) {
        const code = arg.charCodeAt(offset);
        if (code > 0x7F) break;
        mem[ptr + offset] = code;
    }
    if (offset !== len) {
        if (offset !== 0) {
            arg = arg.slice(offset);
        }
        ptr = realloc(ptr, len, len = offset + arg.length * 3, 1) >>> 0;
        const view = getUint8ArrayMemory0().subarray(ptr + offset, ptr + len);
        const ret = cachedTextEncoder.encodeInto(arg, view);

        offset += ret.written;
        ptr = realloc(ptr, len, offset, 1) >>> 0;
    }

    WASM_VECTOR_LEN = offset;
    return ptr;
}

let cachedTextDecoder = new TextDecoder('utf-8', { ignoreBOM: true, fatal: true });
cachedTextDecoder.decode();
const MAX_SAFARI_DECODE_BYTES = 2146435072;
let numBytesDecoded = 0;
function decodeText(ptr, len) {
    numBytesDecoded += len;
    if (numBytesDecoded >= MAX_SAFARI_DECODE_BYTES) {
        cachedTextDecoder = new TextDecoder('utf-8', { ignoreBOM: true, fatal: true });
        cachedTextDecoder.decode();
        numBytesDecoded = len;
    }
    return cachedTextDecoder.decode(getUint8ArrayMemory0().subarray(ptr, ptr + len));
}

const cachedTextEncoder = new TextEncoder();

if (!('encodeInto' in cachedTextEncoder)) {
    cachedTextEncoder.encodeInto = function (arg, view) {
        const buf = cachedTextEncoder.encode(arg);
        view.set(buf);
        return {
            read: arg.length,
            written: buf.length
        };
    };
}

let WASM_VECTOR_LEN = 0;

let wasmModule, wasmInstance, wasm;
function __wbg_finalize_init(instance, module) {
    wasmInstance = instance;
    wasm = instance.exports;
    wasmModule = module;
    cachedDataViewMemory0 = null;
    cachedUint8ArrayMemory0 = null;
    wasm.__wbindgen_start();
    return wasm;
}

async function __wbg_load(module, imports) {
    if (typeof Response === 'function' && module instanceof Response) {
        if (!module.ok) {
            throw new Error(`failed to fetch Wasm: ${module.status} ${module.statusText} fetching '${module.url}'`);
        }

        if (typeof WebAssembly.instantiateStreaming === 'function') {
            try {
                return await WebAssembly.instantiateStreaming(module, imports);
            } catch (e) {
                const validResponse = expectedResponseType(module.type);

                if (validResponse && module.headers.get('Content-Type') !== 'application/wasm') {
                    console.warn("`WebAssembly.instantiateStreaming` failed because your server does not serve Wasm with `application/wasm` MIME type. Falling back to `WebAssembly.instantiate` which is slower. Original error:\n", e);

                } else { throw e; }
            }
        }

        const bytes = await module.arrayBuffer();
        return await WebAssembly.instantiate(bytes, imports);
    } else {
        const instance = await WebAssembly.instantiate(module, imports);

        if (instance instanceof WebAssembly.Instance) {
            return { instance, module };
        } else {
            return instance;
        }
    }

    function expectedResponseType(type) {
        switch (type) {
            case 'basic': case 'cors': case 'default': return true;
        }
        return false;
    }
}

function initSync(module) {
    if (wasm !== undefined) return wasm;


    if (module !== undefined) {
        if (Object.getPrototypeOf(module) === Object.prototype) {
            ({module} = module)
        } else {
            console.warn('using deprecated parameters for `initSync()`; pass a single object instead')
        }
    }

    const imports = __wbg_get_imports();
    if (!(module instanceof WebAssembly.Module)) {
        module = new WebAssembly.Module(module);
    }
    const instance = new WebAssembly.Instance(module, imports);
    return __wbg_finalize_init(instance, module);
}

async function __wbg_init(module_or_path) {
    if (wasm !== undefined) return wasm;


    if (module_or_path !== undefined) {
        if (Object.getPrototypeOf(module_or_path) === Object.prototype) {
            ({module_or_path} = module_or_path)
        } else {
            console.warn('using deprecated parameters for the initialization function; pass a single object instead')
        }
    }

    if (module_or_path === undefined) {
        module_or_path = new URL('slide_transform_bg.wasm', import.meta.url);
    }
    const imports = __wbg_get_imports();

    if (typeof module_or_path === 'string' || (typeof Request === 'function' && module_or_path instanceof Request) || (typeof URL === 'function' && module_or_path instanceof URL)) {
        module_or_path = fetch(module_or_path);
    }

    const { instance, module } = await __wbg_load(await module_or_path, imports);

    return __wbg_finalize_init(instance, module);
}

export { initSync, __wbg_init as default };
