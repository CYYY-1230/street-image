import argparse
import os
import subprocess
import sys
from pathlib import Path

from transformers import AutoImageProcessor, Mask2FormerForUniversalSegmentation

from app import MMSEG_MODEL_FILES, MODEL_ALIASES


MMSEG_CONFIGS = {
    "FCN + Cityscapes": ("fcn_cityscapes", "fcn_r50-d8_4xb2-40k_cityscapes-512x1024"),
    "FCN + ADE20K": ("fcn_ade20k", "fcn_r50-d8_4xb4-160k_ade20k-512x512"),
    "PSPNet + Cityscapes": ("pspnet_cityscapes", "pspnet_r50-d8_4xb2-40k_cityscapes-512x1024"),
    "PSPNet + ADE20K": ("pspnet_ade20k", "pspnet_r50-d8_4xb4-160k_ade20k-512x512"),
    "DeepLabv3 + Cityscapes": ("deeplabv3_cityscapes", "deeplabv3_r50-d8_4xb2-40k_cityscapes-512x1024"),
    "DeepLabv3 + ADE20K": ("deeplabv3_ade20k", "deeplabv3_r50-d8_4xb4-160k_ade20k-512x512"),
}


def predownload(model_name: str, model_id: str, cache_dir: str | None) -> None:
    print(f"[StreetScope] downloading {model_name}: {model_id}", flush=True)
    AutoImageProcessor.from_pretrained(model_id, cache_dir=cache_dir)
    Mask2FormerForUniversalSegmentation.from_pretrained(model_id, cache_dir=cache_dir)
    print(f"[StreetScope] ready {model_name}", flush=True)


def predownload_mmseg(model_name: str, dest_root: Path) -> None:
    folder_name, config_id = MMSEG_CONFIGS[model_name]
    target_weight = dest_root / MMSEG_MODEL_FILES[model_name]
    if target_weight.exists():
        print(f"[StreetScope] ready {model_name}: {target_weight}", flush=True)
        return
    target_dir = dest_root / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    print(f"[StreetScope] downloading {model_name}: {config_id}", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mim",
            "download",
            "mmsegmentation",
            "--config",
            config_id,
            "--dest",
            str(target_dir),
        ],
        check=True,
    )
    print(f"[StreetScope] ready {model_name}: {target_weight}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Predownload StreetScope segmentation models.")
    parser.add_argument(
        "--cache-dir",
        default=os.getenv("HF_HOME") or os.getenv("TRANSFORMERS_CACHE"),
        help="Optional Hugging Face cache directory. Defaults to HF_HOME/TRANSFORMERS_CACHE.",
    )
    parser.add_argument(
        "--model",
        action="append",
        choices=sorted(MODEL_ALIASES.keys()),
        help="Model alias to download. Repeatable. Defaults to all supported models.",
    )
    parser.add_argument(
        "--include-mmseg",
        action="store_true",
        help="Also download FCN/PSPNet/DeepLabv3 OpenMMLab configs and checkpoints.",
    )
    parser.add_argument(
        "--mmseg-root",
        default=os.getenv("MMSEG_MODEL_ROOT", "/root/street_models/mmsegmentation"),
        help="Directory for OpenMMLab config/checkpoint files.",
    )
    args = parser.parse_args()
    if args.cache_dir:
        Path(args.cache_dir).mkdir(parents=True, exist_ok=True)
    selected = args.model or sorted(MODEL_ALIASES.keys())
    for model_name in selected:
        predownload(model_name, MODEL_ALIASES[model_name], args.cache_dir)
    if args.include_mmseg:
        mmseg_root = Path(args.mmseg_root)
        mmseg_root.mkdir(parents=True, exist_ok=True)
        for model_name in sorted(MMSEG_CONFIGS):
            predownload_mmseg(model_name, mmseg_root)


if __name__ == "__main__":
    main()
