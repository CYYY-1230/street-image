import base64
import io
import os
from functools import lru_cache
from pathlib import Path
from typing import Dict

import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from mmseg.apis import inference_model, init_model
from PIL import Image

MMSEG_MODEL_ROOT = Path(os.getenv("MMSEG_MODEL_ROOT", "/root/street_models/mmsegmentation"))
DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"

MMSEG_MODELS = {
    "FCN + ADE20K": {
        "config": "fcn_ade20k/fcn_r50-d8_4xb4-160k_ade20k-512x512.py",
        "checkpoint": "fcn_ade20k/fcn_r50-d8_512x512_160k_ade20k_20200615_100713-4edbc3b4.pth",
    },
    "FCN + Cityscapes": {
        "config": "fcn_cityscapes/fcn_r50-d8_4xb2-40k_cityscapes-512x1024.py",
        "checkpoint": "fcn_cityscapes/fcn_r50-d8_512x1024_40k_cityscapes_20200604_192608-efe53f0d.pth",
    },
    "PSPNet + ADE20K": {
        "config": "pspnet_ade20k/pspnet_r50-d8_4xb4-160k_ade20k-512x512.py",
        "checkpoint": "pspnet_ade20k/pspnet_r50-d8_512x512_160k_ade20k_20200615_184358-1890b0bd.pth",
    },
    "PSPNet + Cityscapes": {
        "config": "pspnet_cityscapes/pspnet_r50-d8_4xb2-40k_cityscapes-512x1024.py",
        "checkpoint": "pspnet_cityscapes/pspnet_r50-d8_512x1024_40k_cityscapes_20200605_003338-2966598c.pth",
    },
    "DeepLabv3 + ADE20K": {
        "config": "deeplabv3_ade20k/deeplabv3_r50-d8_4xb4-160k_ade20k-512x512.py",
        "checkpoint": "deeplabv3_ade20k/deeplabv3_r50-d8_512x512_160k_ade20k_20200615_123227-5d0ee427.pth",
    },
    "DeepLabv3 + Cityscapes": {
        "config": "deeplabv3_cityscapes/deeplabv3_r50-d8_4xb2-40k_cityscapes-512x1024.py",
        "checkpoint": "deeplabv3_cityscapes/deeplabv3_r50-d8_512x1024_40k_cityscapes_20200605_022449-acadc2f8.pth",
    },
}

app = FastAPI(title="StreetScope MMSegmentation Sidecar")


def normalize_label(label: str) -> str:
    return label.lower().replace("-", " ").replace("_", " ").strip()


@lru_cache(maxsize=2)
def load_mmseg_model(model_name: str):
    spec = MMSEG_MODELS.get(model_name)
    if not spec:
        raise HTTPException(status_code=400, detail=f"不支持的 MMSeg 模型：{model_name}")
    config = MMSEG_MODEL_ROOT / spec["config"]
    checkpoint = MMSEG_MODEL_ROOT / spec["checkpoint"]
    if not config.exists() or not checkpoint.exists():
        raise HTTPException(status_code=503, detail=f"{model_name} 权重或配置不存在，请先执行预下载")
    model = init_model(str(config), str(checkpoint), device=DEVICE)
    model.eval()
    return model


def ratio_for(labels: np.ndarray, id2label: Dict[int, str], keywords: tuple[str, ...]) -> float:
    target_ids = [idx for idx, label in id2label.items() if any(key in normalize_label(label) for key in keywords)]
    if not target_ids:
        return 0.0
    return float(np.isin(labels, np.array(target_ids, dtype=labels.dtype)).mean())


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
    return ((37 * value + 67) % 256, (83 * value + 109) % 256, (151 * value + 43) % 256)


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


def id2label_from_model(model) -> Dict[int, str]:
    classes = model.dataset_meta.get("classes", [])
    return {idx: str(label) for idx, label in enumerate(classes)}


@app.get("/health")
def health():
    status = {}
    for name, spec in MMSEG_MODELS.items():
        config = MMSEG_MODEL_ROOT / spec["config"]
        checkpoint = MMSEG_MODEL_ROOT / spec["checkpoint"]
        status[name] = "deployed" if config.exists() and checkpoint.exists() else "missing_files"
    return {"status": "ok", "device": DEVICE, "model_root": str(MMSEG_MODEL_ROOT), "supported_models": list(MMSEG_MODELS), "model_status": status}


@app.post("/segment")
async def segment(image: UploadFile = File(...), model_name: str = Form("FCN + ADE20K")):
    raw = await image.read()
    try:
        pil_image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc
    model = load_mmseg_model(model_name)
    with torch.inference_mode():
        result = inference_model(model, np.asarray(pil_image))
    labels = result.pred_sem_seg.data.detach().cpu().numpy()[0].astype(np.int32)
    id2label = id2label_from_model(model)
    return {
        "model_id": model_name,
        "device": DEVICE,
        "metrics": compute_metrics(labels, id2label, pil_image),
        **segmentation_images(labels, pil_image),
    }
