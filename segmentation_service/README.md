# StreetScope Segmentation Service

This is the production segmentation API used by StreetScope.

## API

`POST /segment`

Form fields:

- `image`: jpg/png/webp street-view image
- `model_name`: `Mask2Former + ADE20K` or `Mask2Former + Cityscapes`

FCN / PSPNet / DeepLabv3 are common thesis comparison choices, but they require a separate MMSegmentation deployment with matching ADE20K or Cityscapes checkpoints. The API returns `501` for those names until those weights are installed and wired in.

Response:

```json
{
  "metrics": {
    "gvi": 0.31,
    "sky_ratio": 0.22,
    "building_ratio": 0.18,
    "road_ratio": 0.24
  }
}
```

## Local GPU Run

```bash
cd segmentation_service
pip install -r requirements.txt
python predownload_models.py
uvicorn app:app --host 0.0.0.0 --port 9000
```

Then fill this URL in StreetScope:

```text
http://127.0.0.1:9000/segment
```

## Docker

```bash
cd segmentation_service
docker build -t streetscope-segmentation .
docker run --gpus all -p 9000:9000 streetscope-segmentation
```

For cloud GPU providers, expose port `9000` and use:

```text
http://<your-cloud-host>:9000/segment
```

Use `Mask2Former + ADE20K` first. It is the broadest scene-parsing option and usually fits street-view GVI research better than Cityscapes when you also need sky, building, water, vegetation, road, and sidewalk-like classes.

## Predownload Models

For a fresh cloud GPU instance, use the bootstrap script instead of running the steps by hand:

```bash
cd /root/streetscope-segmentation
bash cloud_bootstrap.sh
```

It installs the main Mask2Former service, the MMSegmentation sidecar, downloads every configured checkpoint, registers both services in supervisor, and verifies `/health`.

Run this once when creating a new GPU instance, before starting the API:

```bash
cd /root/streetscope-segmentation
python predownload_models.py --include-mmseg
```

It downloads every model listed in `MODEL_ALIASES` into the Hugging Face cache. For the current production service that means:

- `Mask2Former + ADE20K`
- `Mask2Former + Cityscapes`

With `--include-mmseg`, it also downloads OpenMMLab configs and checkpoints into `/root/street_models/mmsegmentation`:

- `FCN + ADE20K`
- `FCN + Cityscapes`
- `PSPNet + ADE20K`
- `PSPNet + Cityscapes`
- `DeepLabv3 + ADE20K`
- `DeepLabv3 + Cityscapes`

Those six files are cached for the next integration step, but the current API still needs an MMSegmentation inference adapter before they can be used by `/segment`.

If you use a persistent disk, set `HF_HOME` before running the script so the cache survives instance replacement:

```bash
export HF_HOME=/data/huggingface
export MMSEG_MODEL_ROOT=/data/street_models/mmsegmentation
python predownload_models.py --include-mmseg
```
