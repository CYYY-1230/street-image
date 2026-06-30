import base64
import io
import os
from functools import lru_cache
from typing import Dict

import numpy as np
import requests
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image
from transformers import AutoImageProcessor, Mask2FormerForUniversalSegmentation

MODEL_ALIASES = {
    "Mask2Former + ADE20K": "facebook/mask2former-swin-large-ade-semantic",
    "Mask2Former + Cityscapes": "facebook/mask2former-swin-large-cityscapes-semantic",
}
PLANNED_MODELS = {
    "FCN + ADE20K",
    "FCN + Cityscapes",
    "PSPNet + ADE20K",
    "PSPNet + Cityscapes",
    "DeepLabv3 + ADE20K",
    "DeepLabv3 + Cityscapes",
}
MMSEG_MODEL_FILES = {
    "FCN + ADE20K": "fcn_ade20k/fcn_r50-d8_512x512_160k_ade20k_20200615_100713-4edbc3b4.pth",
    "FCN + Cityscapes": "fcn_cityscapes/fcn_r50-d8_512x1024_40k_cityscapes_20200604_192608-efe53f0d.pth",
    "PSPNet + ADE20K": "pspnet_ade20k/pspnet_r50-d8_512x512_160k_ade20k_20200615_184358-1890b0bd.pth",
    "PSPNet + Cityscapes": "pspnet_cityscapes/pspnet_r50-d8_512x1024_40k_cityscapes_20200605_003338-2966598c.pth",
    "DeepLabv3 + ADE20K": "deeplabv3_ade20k/deeplabv3_r50-d8_512x512_160k_ade20k_20200615_123227-5d0ee427.pth",
    "DeepLabv3 + Cityscapes": "deeplabv3_cityscapes/deeplabv3_r50-d8_512x1024_40k_cityscapes_20200605_022449-acadc2f8.pth",
}

DEFAULT_MODEL = os.getenv("SEGMENTATION_MODEL", MODEL_ALIASES["Mask2Former + ADE20K"])
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
CACHE_DIR = os.getenv("HF_HOME") or os.getenv("TRANSFORMERS_CACHE") or ""
MMSEG_MODEL_ROOT = os.getenv("MMSEG_MODEL_ROOT", "/root/street_models/mmsegmentation")
MMSEG_SERVICE_URL = os.getenv("MMSEG_SERVICE_URL", "http://127.0.0.1:9001/segment")
MMSEG_HEALTH_URL = os.getenv("MMSEG_HEALTH_URL", "http://127.0.0.1:9001/health")

app = FastAPI(title="StreetScope Segmentation Service")


def normalize_label(label: str) -> str:
    return label.lower().replace("-", " ").replace("_", " ").strip()


@lru_cache(maxsize=3)
def load_model(model_name: str):
    if model_name in PLANNED_MODELS:
        raise HTTPException(status_code=501, detail=f"{model_name} 需要部署对应 FCN/PSPNet/DeepLabv3 权重后才能真实推理")
    model_id = MODEL_ALIASES.get(model_name, model_name or DEFAULT_MODEL)
    if model_id not in MODEL_ALIASES.values():
        raise HTTPException(status_code=400, detail=f"不支持的模型：{model_name}")
    processor = AutoImageProcessor.from_pretrained(model_id)
    model = Mask2FormerForUniversalSegmentation.from_pretrained(model_id)
    model.to(DEVICE)
    model.eval()
    return processor, model


def ratio_for(labels: np.ndarray, id2label: Dict[int, str], keywords: tuple[str, ...]) -> float:
    target_ids = [idx for idx, label in id2label.items() if any(key in normalize_label(label) for key in keywords)]
    if not target_ids:
        return 0.0
    mask = np.isin(labels, np.array(target_ids, dtype=labels.dtype))
    return float(mask.mean())


def shannon_entropy(labels: np.ndarray) -> float:
    _, counts = np.unique(labels, return_counts=True)
    probs = counts.astype(np.float64) / max(int(counts.sum()), 1)
    entropy = -float(np.sum(probs * np.log(probs + 1e-12)))
    return entropy / max(float(np.log(max(len(probs), 2))), 1.0)


def compute_metrics(labels: np.ndarray, id2label: Dict[int, str], image: Image.Image) -> Dict[str, float]:
    vegetation = ratio_for(labels, id2label, ("tree", "grass", "plant", "vegetation", "flora", "palm"))
    sky = ratio_for(labels, id2label, ("sky",))
    water = ratio_for(labels, id2label, ("water", "sea", "river", "lake", "pool"))
    building = ratio_for(labels, id2label, ("building", "house", "skyscraper", "wall", "facade"))
    road = ratio_for(labels, id2label, ("road", "street", "lane"))
    sidewalk = ratio_for(labels, id2label, ("sidewalk", "pavement", "walkway", "curb"))
    vehicle = ratio_for(labels, id2label, ("car", "bus", "truck", "van", "vehicle", "motorcycle", "bicycle"))
    person = ratio_for(labels, id2label, ("person", "rider", "pedestrian"))
    hardscape = min(1.0, road + sidewalk + ratio_for(labels, id2label, ("wall", "fence", "pole", "signboard")))
    natural = min(1.0, vegetation + water + ratio_for(labels, id2label, ("earth", "mountain", "field", "terrain")))
    image_array = np.asarray(image.convert("RGB"), dtype=np.float32)
    rgb_mean = image_array.mean(axis=(0, 1))
    rgb_var = image_array.var(axis=(0, 1))
    return {
        "gvi": round(vegetation, 6),
        "bvi": round(sky + water, 6),
        "sky_ratio": round(sky, 6),
        "water_ratio": round(water, 6),
        "building_ratio": round(building, 6),
        "road_ratio": round(road, 6),
        "sidewalk_ratio": round(sidewalk, 6),
        "vehicle_space_ratio": round(min(1.0, road + vehicle), 6),
        "hardscape_ratio": round(hardscape, 6),
        "human_vehicle_density": round(min(1.0, person + vehicle), 6),
        "natural_ratio": round(natural, 6),
        "enclosure_ratio": round(min(1.0, building + hardscape * 0.35), 6),
        "visual_entropy": round(shannon_entropy(labels), 6),
        "cvi": round(min(1.0, float(rgb_var.mean()) / 6500.0), 6),
        "rgb_mean_r": round(float(rgb_mean[0]), 4),
        "rgb_mean_g": round(float(rgb_mean[1]), 4),
        "rgb_mean_b": round(float(rgb_mean[2]), 4),
        "rgb_var_r": round(float(rgb_var[0]), 4),
        "rgb_var_g": round(float(rgb_var[1]), 4),
        "rgb_var_b": round(float(rgb_var[2]), 4),
    }


def color_for_label(label_id: int) -> tuple[int, int, int]:
    value = int(label_id)
    return (
        (37 * value + 67) % 256,
        (83 * value + 109) % 256,
        (151 * value + 43) % 256,
    )


def segmentation_images(labels: np.ndarray, image: Image.Image) -> Dict[str, str]:
    rgb = np.zeros((*labels.shape, 3), dtype=np.uint8)
    for label_id in np.unique(labels):
        rgb[labels == label_id] = color_for_label(int(label_id))
    mask = Image.fromarray(rgb, mode="RGB")
    overlay = Image.blend(image.convert("RGB"), mask, 0.45)
    mask_buffer = io.BytesIO()
    overlay_buffer = io.BytesIO()
    mask.save(mask_buffer, format="PNG", optimize=True)
    overlay.save(overlay_buffer, format="PNG", optimize=True)
    return {
        "mask_png_base64": base64.b64encode(mask_buffer.getvalue()).decode("ascii"),
        "overlay_png_base64": base64.b64encode(overlay_buffer.getvalue()).decode("ascii"),
    }


def mmseg_health_status() -> Dict[str, str] | None:
    try:
        response = requests.get(MMSEG_HEALTH_URL, timeout=1.5)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    status = payload.get("model_status")
    return status if isinstance(status, dict) else None


@app.get("/health")
def health():
    sidecar_status = mmseg_health_status()
    mmseg_status: Dict[str, str] = {}
    for name, relative_path in MMSEG_MODEL_FILES.items():
        weight_path = os.path.join(MMSEG_MODEL_ROOT, relative_path)
        if sidecar_status and sidecar_status.get(name) == "deployed":
            mmseg_status[name] = "deployed"
        else:
            mmseg_status[name] = "weights_downloaded_needs_sidecar" if os.path.exists(weight_path) else "needs_download_and_sidecar"
    return {
        "status": "ok",
        "device": DEVICE,
        "default_model": DEFAULT_MODEL,
        "cache_dir": CACHE_DIR,
        "mmseg_model_root": MMSEG_MODEL_ROOT,
        "mmseg_service_url": MMSEG_SERVICE_URL,
        "supported_models": list(MODEL_ALIASES.keys()),
        "planned_models": sorted(PLANNED_MODELS),
        "model_status": {
            **{name: "deployed" for name in MODEL_ALIASES},
            **mmseg_status,
        },
    }


def proxy_mmseg_segment(raw: bytes, filename: str, model_name: str):
    try:
        response = requests.post(
            MMSEG_SERVICE_URL,
            data={"model_name": model_name},
            files={"image": (filename or "image.jpg", raw, "application/octet-stream")},
            timeout=180,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"MMSegmentation sidecar 不可用：{exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=f"MMSegmentation 推理失败：{detail}")
    return response.json()


@app.post("/segment")
async def segment(image: UploadFile = File(...), model_name: str = Form("Mask2Former + ADE20K")):
    raw = await image.read()
    if model_name in PLANNED_MODELS:
        return proxy_mmseg_segment(raw, image.filename or "image.jpg", model_name)
    try:
        pil_image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc
    processor, model = load_model(model_name)
    inputs = processor(images=pil_image, return_tensors="pt")
    inputs = {key: value.to(DEVICE) for key, value in inputs.items()}
    with torch.inference_mode():
        outputs = model(**inputs)
    target_size = [(pil_image.height, pil_image.width)]
    segmentation = processor.post_process_semantic_segmentation(outputs, target_sizes=target_size)[0]
    labels = segmentation.detach().cpu().numpy().astype(np.int32)
    id2label = {int(key): value for key, value in model.config.id2label.items()}
    return {
        "model_id": MODEL_ALIASES.get(model_name, model_name or DEFAULT_MODEL),
        "device": DEVICE,
        "metrics": compute_metrics(labels, id2label, pil_image),
        **segmentation_images(labels, pil_image),
    }
