# 仅评测用镜像（不参与服务部署）：cellpose 3.1.1.3（CPnet 家族，
# nuclei / cyto3 权重）+ numba。与服务的 cellpose 4.2.1.1 不能共存于同一
# 环境（同名包、骨干不兼容），故独立成镜像跑 scripts/eval_monuseg.py
# --family 3 的候选对比。
#
# 基础镜像与运行镜像同款（torch 2.1.0+cu121，sm_75 兼容）；PyPI 依赖由
# 宿主侧预取 wheels-cp3/（py310 manylinux，无 torch/numpy——用基础镜像
# 自带 2.1.0+cu121 / 1.26.4）随上下文离线安装。
FROM docker.io/pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    CELLPOSE_LOCAL_MODELS_PATH=/models \
    PYTHONUNBUFFERED=1

COPY wheels-cp3/ /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels \
        "cellpose==3.1.1.3" \
    && rm -rf /wheels

# 构建期烟雾测试（CPU、无权重）
RUN python -c \
    "import torch, cellpose; from cellpose import models; \
     print('import ok: torch', torch.__version__, 'cellpose', cellpose.version)"
