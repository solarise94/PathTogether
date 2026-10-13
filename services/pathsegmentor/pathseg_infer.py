# -*- coding: utf-8 -*-
"""PathSegmentor 推理核心：预处理 / 提示词构造 / 掩码回映射 / 模型加载与分段。

纯逻辑部分（预处理、几何回映射、提示词构造、PNG 编码）不依赖 torch，
单元测试可在无 GPU/权重的环境运行；torch 与上游 modeling 仅在
``load_model`` / ``segment`` 内部延迟导入。

推理路径逐行复刻上游 ``inference_examples.ipynb``（commit
e67763ab835b3ef184cb6791eb2cd059ec8b9f9a）：

  1. 预处理 = ``inference_utils/processing_utils.read_rgb``：RGB 化（调用方
     完成）→ pad 成正方形（两侧各 (差值)//2；奇数差时短 1px，上游原样保留）
     → 每通道 skimage ``transform.resize`` 到 1024×1024（bicubic、
     preserve_range、anti_aliasing）；输入恰为 1024×1024 时跳过 resize
     （同尺寸 resize 恒等）；
  2. ``model.model.evaluate_demo([{image, text, height, width}])``，
     task_switch 只开 grounding；
  3. ``vl_similarity`` 匹配 prompt 嵌入 → 取对应 pred mask → 双线性插值回
     1024×1024 → sigmoid → ``> threshold``（上游固定 0.5，此处可配）；
  4. 掩码回映射：resize 回 pad 后正方形尺寸，再裁掉 padding → 原图尺寸。

已知上游问题：``inference_utils/output_processing.py`` 在 import 期以硬编码
绝对路径（/home/chenzhixuan/...）读 target_dist.json，普通环境 import
``inference_utils.inference`` 即失败——因此本模块不复用该包，而是复刻
``interactive_infer_image``（并借此支持自定义 threshold）。
"""
import hashlib
import io
from collections import namedtuple

import numpy as np
from PIL import Image
from skimage import transform as skimage_transform

#: 上游代码 revision（Containerfile 与运行时共同固定）
UPSTREAM_COMMIT = "e67763ab835b3ef184cb6791eb2cd059ec8b9f9a"
#: 上游 detectron2 fork 固定 commit（requirements.txt 锁定）
DETECTRON2_COMMIT = "42121d75e10d9f858f3a91b6a39f5722c02868f0"

#: 模型输入尺寸（transforms.Resize(1024) + read_rgb 的 1024×1024）
INPUT_SIZE = 1024

#: 预处理几何记录：原图尺寸 / pad 后正方形尺寸 / pad 偏移
Geometry = namedtuple("Geometry", "orig_h orig_w src_h src_w pad_y pad_x")


# --------------------------------------------------------------------------
# 预处理与掩码回映射（复刻 read_rgb，可逆）
# --------------------------------------------------------------------------

def preprocess_image(img):
    """RGB uint8 HxWx3 → (1024x1024x3 uint8, Geometry)。

    复刻 ``read_rgb``：pad 成正方形（pad = 差值//2，两侧各 pad；奇数差时
    padded 边比正方形短 1，与上游一致），再逐通道 skimage bicubic resize 到
    1024×1024。输入已是 1024×1024 时跳过 resize（同尺寸 resize 恒等）。
    """
    img = np.asarray(img)
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError("img must be an HxWx3 RGB array, got shape %r"
                         % (tuple(img.shape),))
    orig_h, orig_w = int(img.shape[0]), int(img.shape[1])
    src_h, src_w = orig_h, orig_w
    pad_y = pad_x = 0
    if orig_h > orig_w:
        pad = (orig_h - orig_w) // 2
        img = np.pad(img, ((0, 0), (pad, pad), (0, 0)), "constant",
                     constant_values=0)
        src_w = orig_w + 2 * pad
        pad_x = pad
    elif orig_w > orig_h:
        pad = (orig_w - orig_h) // 2
        img = np.pad(img, ((pad, pad), (0, 0), (0, 0)), "constant",
                     constant_values=0)
        src_h = orig_h + 2 * pad
        pad_y = pad
    if (src_h, src_w) != (INPUT_SIZE, INPUT_SIZE):
        resized = np.zeros((INPUT_SIZE, INPUT_SIZE, 3), dtype=np.uint8)
        for channel in range(3):
            resized[:, :, channel] = skimage_transform.resize(
                img[:, :, channel], (INPUT_SIZE, INPUT_SIZE), order=3,
                mode="constant", preserve_range=True, anti_aliasing=True)
        img = resized
    geometry = Geometry(orig_h, orig_w, int(src_h), int(src_w),
                        int(pad_y), int(pad_x))
    return np.ascontiguousarray(img), geometry


def mask_to_original_size(mask, geometry):
    """1024×1024 bool mask → 原图尺寸 bool mask（preprocess_image 的逆变换）。

    先线性插值放大回 pad 后正方形（float 上做 >0.5，等价「概率回映射再二值
    化」），再裁掉 padding。1024×1024 无 pad 输入为恒等（示例集即此情形，
    与 GT 逐像素可比）。
    """
    mask = np.asarray(mask).astype(bool)
    orig_h, orig_w = geometry.orig_h, geometry.orig_w
    if (geometry.src_h, geometry.src_w) == (orig_h, orig_w) == \
            (INPUT_SIZE, INPUT_SIZE):
        return mask
    scaled = cv2_resize_mask(mask.astype(np.float32),
                             (geometry.src_w, geometry.src_h))
    y0, x0 = geometry.pad_y, geometry.pad_x
    return scaled[y0:y0 + orig_h, x0:x0 + orig_w]


def cv2_resize_mask(mask_f32, size_wh):
    """mask float 插值（cv2.INTER_LINEAR）；依赖延迟导入便于无 cv2 测试环境。"""
    import cv2
    resized = cv2.resize(mask_f32, (int(size_wh[0]), int(size_wh[1])),
                         interpolation=cv2.INTER_LINEAR)
    return np.asarray(resized) > 0.5


def encode_mask_png(mask):
    """bool mask → 8-bit PNG bytes（0=背景，255=前景），内存中完成不落盘。"""
    arr = np.where(np.asarray(mask).astype(bool), np.uint8(255), np.uint8(0))
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def file_sha256(path, chunk_bytes=8 * 1024 * 1024):
    """流式 SHA-256（权重文件 ~1.8GB，不整读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# 提示词构造（上游语法：{level}-level {target} in {region} pathology）
# --------------------------------------------------------------------------

#: level 词表（上游 CSV 的第一列斜杠层级：Cell/Nuclei/Tissue）
LEVELS = ("cell", "nuclei", "tissue")

#: target → 支持的 level 集合。自上游 hierachical_labels_and_prompts.csv
#: （e67763a，160 行）的 Text Prompts 列收敛；一个 target 出现在多个 level
#: 时由 _LEVEL_ORDER 取先出现者，_TARGET_LEVEL_OVERRIDES 可显式覆盖。
_TARGET_LEVELS = {
    # cell-level
    "adipocyte": {"cell"}, "cancerous": {"cell"},
    "dysplastic/malignant epithelial": {"cell"}, "endothelial": {"cell"},
    "glial": {"cell"}, "healthy epithelial": {"cell"},
    "mitosis": {"cell"}, "myeloid cells": {"cell"}, "myo epithelial": {"cell"},
    "neuron": {"cell"}, "red blood cells": {"cell"},
    "leukocytes": {"cell", "nuclei"},
    "lymphocyte": {"cell", "nuclei"}, "macrophage": {"cell", "nuclei"},
    "neutrophil": {"cell", "nuclei"}, "plasma cells": {"cell", "nuclei"},
    "epithelial": {"cell", "nuclei", "tissue"},
    "fibroblast": {"cell", "nuclei"}, "inflammatory": {"cell", "nuclei"},
    "necrosis": {"cell"},
    # nuclei-level
    "apoptotic body": {"nuclei"}, "connective": {"nuclei"}, "dead": {"nuclei"},
    "ductal epithelium": {"nuclei"}, "eosinophil": {"nuclei"},
    "mitotic figure": {"nuclei"}, "myoepithelium": {"nuclei"},
    "neoplastic": {"nuclei"},
    "neuroendocrine tumor mitotic figure": {"nuclei"},
    "neuroendocrine tumor non mitotic figure": {"nuclei"},
    "nuclei": {"nuclei"}, "tumor": {"nuclei", "tissue"},
    "vascular endothelium": {"nuclei"},
    # tissue-level
    "angioinvasion": {"tissue"}, "blood": {"tissue"},
    "blood vessel": {"tissue"}, "dcis": {"tissue"}, "fat": {"tissue"},
    "gland": {"tissue"}, "glandular secretions": {"tissue"},
    "lymphatics": {"tissue"}, "lymphocytic infiltrate": {"tissue"},
    "malignant lesion": {"tissue"}, "metaplasia nos": {"tissue"},
    "metastatic colon cancer in a liver": {"tissue"},
    "mucoid material": {"tissue"}, "necrosis or debris": {"tissue"},
    "nerve": {"tissue"}, "normal acinus or duct": {"tissue"},
    "normal tissue": {"tissue"}, "other immune infiltrate": {"tissue"},
    "prostate cancer": {"tissue"}, "prostate healthy glands": {"tissue"},
    "prostate tumor": {"tissue"}, "skin adnexa": {"tissue"},
    "smooth muscle cells": {"tissue"}, "stroma": {"tissue"},
    "tumor associated stroma tissue": {"tissue"},
    "tumor epithelial tissue": {"tissue"},
}

#: 歧义 target 的 level 覆盖（缺省按 _LEVEL_ORDER 第一个）
_LEVEL_ORDER = ("cell", "tissue", "nuclei")
_TARGET_LEVEL_OVERRIDES = {
    "tumor": "tissue",           # 默认整体病变组织；细胞核级请显式 "nuclei tumor"
    "necrosis": "cell",          # cell-level necrosis（乳腺）；组织级用 "necrosis or debris"
}


def _resolve_level(target):
    """target → level：显式覆盖 > _LEVEL_ORDER 中首个支持的 level > 兜底 tissue。"""
    levels = _TARGET_LEVELS.get(target)
    if not levels:
        return "tissue"
    if target in _TARGET_LEVEL_OVERRIDES:
        return _TARGET_LEVEL_OVERRIDES[target]
    for level in _LEVEL_ORDER:
        if level in levels:
            return level
    return "tissue"


def build_prompt(object_phrase, region=None):
    """HTTP 契约的 object/region → 上游训练提示词。

    规则（见 README）：
      1. ``+`` 视为空格；全小写（上游提示词全小写，CLIP tokenizer 大小写敏感）；
      2. object 首词已是 level（cell/nuclei/tissue）且后随其它词 → 拆分
         （如 "nuclei connective" → "nuclei-level connective"）；
      3. object 本身就是 level → level 与 target 相同
         （"nuclei" → "nuclei-level nuclei"，上游 CSV 存在该 prompt）；
      4. 其余查 _TARGET_LEVELS 词表（做复数/“ cells”后缀归一），查不到兜底
         tissue；region 缺省 "unspecified"；
      5. 应用上游 ``format_string``：unspecified → multiple。

    返回 ``(prompt, level, target, region_used)``。
    """
    if not isinstance(object_phrase, str):
        raise ValueError("object must be a string")
    text = " ".join(object_phrase.replace("+", " ").split()).lower()
    if not text:
        raise ValueError("object must be non-empty")
    words = text.split(" ")
    if words[0] in LEVELS and len(words) > 1:
        level, target = words[0], " ".join(words[1:])
    elif text in LEVELS:
        level, target = text, text
    else:
        lookup = text
        if lookup.endswith(" cells") and lookup[:-6] in _TARGET_LEVELS:
            lookup = lookup[:-6]
        elif lookup not in _TARGET_LEVELS and \
                lookup.endswith("s") and lookup[:-1] in _TARGET_LEVELS:
            lookup = lookup[:-1]
        level = _resolve_level(lookup)
        target = text
    region_used = " ".join(
        (region or "").replace("+", " ").split()).lower() or "unspecified"
    prompt = "%s-level %s in %s pathology" % (level, target, region_used)
    prompt = prompt.replace("unspecified", "multiple")  # 上游 format_string
    return prompt, level, target, region_used


# --------------------------------------------------------------------------
# 模型加载与分段（torch 上游链路，延迟导入）
# --------------------------------------------------------------------------

def load_model(weights_path, expected_sha256=None):
    """加载 PathSegmentor 到 cuda:0（复刻 notebook cell 0 + from_pretrained）。

    - 先流式计算权重 SHA-256；``expected_sha256`` 给定且不符 → 拒绝加载；
    - ``torch.load`` 优先 ``weights_only=True``（纯 tensor state_dict 应当
      可加载）；失败才降级 ``weights_only=False``（该权重来自用户的可信
      只读副本，隔离容器内可接受；降级事实记录在返回值供 /readyz 报告）；
    - ``align_and_update_state_dicts`` + ``load_state_dict(strict=False)``
      与上游 ``BaseModel.from_pretrained`` 一致；
    - 加载后按 notebook 刷新默认文本嵌入
      （PATHSEG_CLASSES + ["background"]，is_eval=True）。

    返回 ``(model, info)``，info 含 sha256 / weights_only / device。
    """
    import torch
    from modeling import build_model
    from modeling.BaseModel import BaseModel
    from utilities.arguments import load_opt_from_config_files
    from utilities.constants import PATHSEG_CLASSES
    from utilities.distributed import init_distributed
    from utilities.model import align_and_update_state_dicts

    sha256 = file_sha256(weights_path)
    if expected_sha256 and sha256 != expected_sha256:
        raise ValueError(
            "weights sha256 mismatch: expected %s, got %s"
            % (expected_sha256, sha256))

    opt = load_opt_from_config_files(["configs/pathsegmentor_inference.yaml"])
    opt = init_distributed(opt)  # 单进程（无 MPI）；device=cuda:0
    model = BaseModel(opt, build_model(opt))

    try:
        state_dict = torch.load(weights_path, map_location="cpu",
                                weights_only=True)
        weights_only = True
    except Exception:  # noqa: BLE001 — 非 tensor pickle 才需要降级
        state_dict = torch.load(weights_path, map_location="cpu",
                                weights_only=False)
        weights_only = False
    state_dict = align_and_update_state_dicts(model.model.state_dict(),
                                              state_dict)
    model.model.load_state_dict(state_dict, strict=False)

    device = opt["device"]
    model = model.eval().to(device)
    with torch.no_grad():
        model.model.sem_seg_head.predictor.lang_encoder.get_text_embeddings(
            PATHSEG_CLASSES + ["background"], is_eval=True)
    info = {"sha256": sha256, "weights_only": weights_only,
            "device": str(device)}
    return model, info


def segment(model, img_rgb, prompt, threshold=0.5):
    """单图推理 → 原图尺寸 bool mask（复刻 interactive_infer_image）。

    prompt 为构造好的字符串（build_prompt / 示例集文件名派生）；
    threshold 作用于 sigmoid 概率（上游固定 > 0.5，此处可配，比较沿用 >）。
    """
    import torch
    import torch.nn.functional as F
    from modeling.language.loss import vl_similarity

    arr, geometry = preprocess_image(img_rgb)
    device = next(model.parameters()).device
    image = torch.from_numpy(arr.copy()).permute(2, 0, 1).to(device)
    height = width = INPUT_SIZE  # 预处理后恒为 1024×1024

    data = {"image": image, "text": [prompt], "height": height,
            "width": width}
    task_switch = model.model.task_switch
    task_switch["spatial"] = False
    task_switch["visual"] = False
    task_switch["grounding"] = True
    task_switch["audio"] = False

    with torch.no_grad():
        results, image_size, extra = model.model.evaluate_demo([data])

        pred_masks = results["pred_masks"][0]
        v_emb = results["pred_captions"][0]
        t_emb = extra["grounding_class"]

        t_emb = t_emb / (t_emb.norm(dim=-1, keepdim=True) + 1e-7)
        v_emb = v_emb / (v_emb.norm(dim=-1, keepdim=True) + 1e-7)

        temperature = \
            model.model.sem_seg_head.predictor.lang_encoder.logit_scale
        out_prob = vl_similarity(v_emb, t_emb, temperature=temperature)

        matched_id = out_prob.max(0)[1]
        pred_masks_pos = pred_masks[matched_id, :, :]

        prob = F.interpolate(
            pred_masks_pos[None, ], image_size[-2:], mode="bilinear"
        )[0, :, :height, :width].sigmoid().cpu().numpy()

    mask_1024 = prob > threshold
    return mask_to_original_size(mask_1024, geometry)
