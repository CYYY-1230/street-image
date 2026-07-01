from __future__ import annotations

import csv
import base64
import binascii
import hmac
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional
from urllib.parse import quote, urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests
import shapefile
import numpy as np
from PIL import Image, ImageDraw
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("STREETSCOPE_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(exist_ok=True)
IMAGE_ARCHIVE_DIR = DATA_DIR / "archive" / "streetview_images"
IMAGE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
TASK_STORE_PATH = DATA_DIR / "tasks_index.json"
TASK_STORE_LOCK = threading.Lock()

app = FastAPI(title="StreetScope Research API", version="0.1.0")
cors_origins = [origin.strip() for origin in os.getenv("STREETSCOPE_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
cors_origin_regex = os.getenv(
    "STREETSCOPE_CORS_ORIGIN_REGEX",
    r"https://.*\.vercel\.app|http://(localhost|127\.0\.0\.1):51[0-9]{2}",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def configured_users() -> Dict[str, str]:
    users: Dict[str, str] = {}
    username = os.getenv("STREETSCOPE_USER", "").strip()
    password = os.getenv("STREETSCOPE_PASSWORD", "")
    if username and password:
        users[username] = password
    for item in os.getenv("STREETSCOPE_USERS", "").split(","):
        if ":" not in item:
            continue
        raw_user, raw_password = item.split(":", 1)
        raw_user = raw_user.strip()
        if raw_user and raw_password:
            users[raw_user] = raw_password
    return users


def verify_password(candidate: str, expected: str) -> bool:
    if expected.startswith("sha256$"):
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(f"sha256${digest}", expected)
    return hmac.compare_digest(candidate, expected)


def auth_enabled() -> bool:
    return bool(configured_users())


def decode_basic_auth(header: str) -> tuple[str, str] | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return None
    if ":" not in decoded:
        return None
    return decoded.split(":", 1)


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if not auth_enabled() or request.url.path == "/api/health":
        return await call_next(request)
    credentials = decode_basic_auth(request.headers.get("authorization", ""))
    users = configured_users()
    if not credentials or credentials[0] not in users or not verify_password(credentials[1], users[credentials[0]]):
        return HTMLResponse(
            "StreetScope 需要登录。",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="StreetScope"'},
        )
    request.state.user_id = credentials[0]
    return await call_next(request)


def current_user_id(request: Request | None) -> str:
    if not auth_enabled():
        return ""
    return str(getattr(getattr(request, "state", None), "user_id", "") or "")


def user_can_access_task(task: TaskState, request: Request | None) -> bool:
    owner = str(task.meta.get("owner") or "")
    user_id = current_user_id(request)
    return not auth_enabled() or not owner or owner == user_id


class Boundary(BaseModel):
    north: float
    south: float
    east: float
    west: float


class SampleRequest(BaseModel):
    boundary: Boundary
    interval_m: int = Field(default=100, ge=25, le=500)
    road_density: Literal["low", "medium", "high"] = "medium"


class OsmRoadRequest(BaseModel):
    boundary: Boundary
    interval_m: int = Field(default=100, ge=25, le=500)
    keep_walkable: bool = True
    exclude_high_speed: bool = True
    clean_roads: bool = True


class SamplePoint(BaseModel):
    point_id: str
    lng: float
    lat: float
    coord_type: str = "wgs84"
    lng_wgs84: float = 0
    lat_wgs84: float = 0
    lng_gcj02: float = 0
    lat_gcj02: float = 0
    lng_bd09: float
    lat_bd09: float
    road_id: str
    road_name: str
    admin_code: str = ""
    admin_name: str
    sample_interval: int
    source: str = "generated"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")


class RoadFeature(BaseModel):
    road_id: str
    road_name: str
    coordinates: List[List[float]]


class SampleResponse(BaseModel):
    points: List[SamplePoint]
    roads: List[RoadFeature]
    estimate: Dict[str, float]
    source: str = "generated"


class DownloadRequest(BaseModel):
    project_name: str = "street-scope-production"
    ak: Optional[str] = None
    provider: Literal["baidu", "baidu_web"] = "baidu_web"
    use_real_baidu: bool = False
    points: List[SamplePoint]
    boundary: Optional[Boundary] = None
    roads: List[RoadFeature] = Field(default_factory=list)
    headings: List[int] = Field(default_factory=lambda: [0, 90, 180, 270])
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=512, ge=256, le=2048)
    pitch: int = Field(default=0, ge=0, le=90)
    fov: int = Field(default=90, ge=10, le=360)
    coordtype: Literal["bd09ll", "wgs84ll", "gcj02"] = "bd09ll"
    image_mode: Literal["directions", "stitched", "panorama"] = "directions"
    capture_date: str = "latest"
    skip_existing: bool = True
    concurrency: int = Field(default=2, ge=1, le=8)
    retry_count: int = Field(default=1, ge=0, le=5)


class BaiduTestRequest(BaseModel):
    ak: str
    point: Optional[SamplePoint] = None
    width: int = Field(default=512, ge=256, le=2048)
    height: int = Field(default=256, ge=256, le=2048)
    heading: int = Field(default=0, ge=0, le=360)
    pitch: int = Field(default=0, ge=0, le=90)
    fov: int = Field(default=90, ge=10, le=360)
    coordtype: Literal["bd09ll", "wgs84ll", "gcj02"] = "bd09ll"


class QualityUpdateRequest(BaseModel):
    image_id: str
    quality_status: Literal["unchecked", "accepted", "low_quality", "excluded"]
    note: str = ""


class RetryFailedRequest(BaseModel):
    ak: Optional[str] = None


class TaskControlRequest(BaseModel):
    action: Literal["pause", "resume", "cancel"]


class MetricsRequest(BaseModel):
    project_name: str = "street-scope-production"
    points: List[SamplePoint]
    boundary: Optional[Boundary] = None
    roads: List[RoadFeature] = Field(default_factory=list)
    headings: List[int] = Field(default_factory=lambda: [0, 90, 180, 270])
    source_download_task_id: str = ""
    model_name: str = "Mask2Former + ADE20K"
    selected_metrics: List[str] = Field(default_factory=lambda: ["gvi", "sky_ratio", "building_ratio", "road_ratio", "sidewalk_ratio"])
    inference_mode: Literal["external"] = "external"
    segmentation_service_url: str = ""


@dataclass
class TaskState:
    task_id: str
    kind: str
    status: Literal["queued", "running", "paused", "completed", "failed", "canceled"] = "queued"
    progress: int = 0
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    message: str = "任务已创建"
    records: List[Dict[str, object]] = field(default_factory=list)
    meta: Dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")


TASKS: Dict[str, TaskState] = {}


def save_task_store() -> None:
    with TASK_STORE_LOCK:
        payload = {"tasks": [asdict(task) for task in TASKS.values()]}
        tmp_path = TASK_STORE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(TASK_STORE_PATH)


def load_task_store() -> None:
    if not TASK_STORE_PATH.exists():
        return
    try:
        payload = json.loads(TASK_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    raw_tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(raw_tasks, list):
        return
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        try:
            task = TaskState(
                task_id=str(raw.get("task_id", "")),
                kind=str(raw.get("kind", "")),
                status=raw.get("status", "queued"),
                progress=int(raw.get("progress", 0) or 0),
                total=int(raw.get("total", 0) or 0),
                succeeded=int(raw.get("succeeded", 0) or 0),
                failed=int(raw.get("failed", 0) or 0),
                message=str(raw.get("message", "任务已恢复")),
                records=raw.get("records", []) if isinstance(raw.get("records"), list) else [],
                meta=raw.get("meta", {}) if isinstance(raw.get("meta"), dict) else {},
                created_at=str(raw.get("created_at") or datetime.utcnow().isoformat(timespec="seconds") + "Z"),
            )
        except (TypeError, ValueError):
            continue
        if not task.task_id:
            continue
        if task.status in {"queued", "running"}:
            task.status = "paused"
            task.message = "服务重启，任务已暂停，可继续补采"
        TASKS[task.task_id] = task


load_task_store()


def reconcile_task_state(task: TaskState) -> None:
    if task.kind != "download" or not task.total:
        return
    done = task.succeeded + task.failed
    task.progress = min(100, round(done / max(task.total, 1) * 100))
    if task.status == "completed" and done < task.total:
        task.status = "paused"
        task.message = f"任务未完成：已处理 {done}/{task.total} 张图像，可继续补采"


def task_summary(task: TaskState) -> Dict[str, object]:
    reconcile_task_state(task)
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "status": task.status,
        "progress": task.progress,
        "total": task.total,
        "succeeded": task.succeeded,
        "failed": task.failed,
        "message": task.message,
        "project_name": task.meta.get("project_name", ""),
        "record_count": len(task.records),
        "created_at": task.created_at,
        "export_url": f"/api/export/{task.task_id}" if task.status == "completed" else "",
    }


def task_should_continue(task: TaskState) -> bool:
    while task.status == "paused":
        task.message = "任务已暂停"
        time.sleep(0.15)
    if task.status == "canceled":
        task.message = "任务已取消"
        return False
    return True


def meters_to_lat(meters: float) -> float:
    return meters / 111_320


def meters_to_lng(meters: float, lat: float) -> float:
    return meters / (111_320 * max(math.cos(math.radians(lat)), 0.2))


def out_of_china(lng: float, lat: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def transform_lat(lng: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def transform_lng(lng: float, lat: float) -> float:
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    if out_of_china(lng, lat):
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * math.pi * 3000.0 / 180.0)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * math.pi * 3000.0 / 180.0)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def wgs84_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    return gcj02_to_bd09(gcj_lng, gcj_lat)


LLBAND = [75, 60, 45, 30, 15, 0]
LL2MC = [
    [-0.0015702102444, 111320.7020616939, 1704480524535203, -10338987376042340, 26112667856603880, -35149669176653700, 26595700718403920, -10725012454188240, 1800819912950474, 82.5],
    [0.0008277824516172526, 111320.7020463578, 647795574.6671607, -4082003173.641316, 10774905663.51142, -15171875531.51559, 12053065338.62167, -5124939663.577472, 913311935.9512032, 67.5],
    [0.00337398766765, 111320.7020202162, 4481351.045890365, -23393751.19931662, 79682215.47186455, -115964993.2797253, 97236711.15602145, -43661946.33752821, 8477230.501135234, 52.5],
    [0.00220636496208, 111320.7020209128, 51751.86112841131, 3796837.749470245, 992013.7397791013, -1221952.21711287, 1340652.697009075, -620943.6990984312, 144416.9293806241, 37.5],
    [-0.0003441963504368392, 111320.7020576856, 278.2353980772752, 2485758.690035394, 6070.750963243378, 54821.18345352118, 9540.606633304236, -2710.55326746645, 1405.483844121726, 22.5],
    [-0.0003218135878613132, 111320.7020701615, 0.00369383431289, 823725.6402795718, 0.46104986909093, 2351.343141331292, 1.58060784298199, 8.77738589078284, 0.37238884252424, 7.45],
]


def bd09_to_mercator(lng: float, lat: float) -> tuple[float, float]:
    lng = ((lng + 180) % 360) - 180
    lat = max(-74, min(74, lat))
    factor = LL2MC[-1]
    for idx, band in enumerate(LLBAND):
        if lat >= band:
            factor = LL2MC[idx]
            break
    if lat < 0:
        for idx in range(len(LLBAND) - 1, -1, -1):
            if lat <= -LLBAND[idx]:
                factor = LL2MC[idx]
                break
    x = factor[0] + factor[1] * abs(lng)
    c = abs(lat) / factor[9]
    y = factor[2] + factor[3] * c + factor[4] * c**2 + factor[5] * c**3 + factor[6] * c**4 + factor[7] * c**5 + factor[8] * c**6
    return (-x if lng < 0 else x), (-y if lat < 0 else y)


def build_sample_point(
    *,
    point_id: str,
    lng: float,
    lat: float,
    road_id: str,
    road_name: str,
    admin_name: str,
    sample_interval: int,
    source: str = "generated",
    admin_code: str = "",
    coord_type: str = "wgs84",
) -> SamplePoint:
    gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    bd_lng, bd_lat = gcj02_to_bd09(gcj_lng, gcj_lat)
    return SamplePoint(
        point_id=point_id,
        lng=round(lng, 7),
        lat=round(lat, 7),
        coord_type=coord_type,
        lng_wgs84=round(lng, 7),
        lat_wgs84=round(lat, 7),
        lng_gcj02=round(gcj_lng, 7),
        lat_gcj02=round(gcj_lat, 7),
        lng_bd09=round(bd_lng, 7),
        lat_bd09=round(bd_lat, 7),
        road_id=road_id,
        road_name=road_name,
        admin_code=admin_code,
        admin_name=admin_name,
        sample_interval=sample_interval,
        source=source,
    )


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lng1, lat1 = a
    lng2, lat2 = b
    radius = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def interpolate_segment(a: tuple[float, float], b: tuple[float, float], ratio: float) -> tuple[float, float]:
    return a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio


def sample_polyline(coords: List[List[float]], interval_m: int) -> List[tuple[float, float]]:
    clean = [(float(item[0]), float(item[1])) for item in coords if len(item) >= 2]
    if len(clean) < 2:
        return []
    segment_lengths = [haversine_m(clean[i], clean[i + 1]) for i in range(len(clean) - 1)]
    total = sum(segment_lengths)
    if total <= 0:
        return []
    distances = list(range(0, int(total) + 1, interval_m))
    if not distances or distances[-1] < total:
        distances.append(int(total))
    points: List[tuple[float, float]] = []
    for distance in distances:
        remaining = distance
        for idx, length in enumerate(segment_lengths):
            if remaining <= length or idx == len(segment_lengths) - 1:
                ratio = 0 if length == 0 else min(1, remaining / length)
                points.append(interpolate_segment(clean[idx], clean[idx + 1], ratio))
                break
            remaining -= length
    return points


def collect_line_features(geojson: Dict[str, object]) -> List[RoadFeature]:
    features: List[RoadFeature] = []

    def add_line(coordinates: object, road_id: str, road_name: str) -> None:
        if not isinstance(coordinates, list):
            return
        if coordinates and isinstance(coordinates[0], list) and coordinates[0] and isinstance(coordinates[0][0], (int, float)):
            features.append(RoadFeature(road_id=road_id, road_name=road_name, coordinates=coordinates))

    if isinstance(geojson, dict) and geojson.get("type") == "LineString":
        add_line(geojson.get("coordinates"), "U0001", "上传路网 1")
    if isinstance(geojson, dict) and geojson.get("type") == "MultiLineString" and isinstance(geojson.get("coordinates"), list):
        for part_index, part in enumerate(geojson["coordinates"], 1):
            add_line(part, f"U0001_{part_index}", "上传路网 1")

    raw_features = geojson.get("features") if isinstance(geojson, dict) else None
    if isinstance(raw_features, list):
        for idx, feature in enumerate(raw_features, 1):
            if not isinstance(feature, dict):
                continue
            geometry = feature.get("geometry")
            properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            if not isinstance(geometry, dict):
                continue
            road_id = str(properties.get("road_id") or properties.get("id") or f"U{idx:04d}")
            road_name = str(properties.get("road_name") or properties.get("name") or properties.get("道路名") or f"上传路网 {idx}")
            geom_type = geometry.get("type")
            coordinates = geometry.get("coordinates")
            if geom_type == "LineString":
                add_line(coordinates, road_id, road_name)
            elif geom_type == "MultiLineString" and isinstance(coordinates, list):
                for part_index, part in enumerate(coordinates, 1):
                    add_line(part, f"{road_id}_{part_index}", road_name)
    return features


WALKABLE_HIGHWAYS = {
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "pedestrian",
    "footway",
    "path",
    "cycleway",
    "steps",
}
HIGH_SPEED_HIGHWAYS = {"motorway", "motorway_link", "trunk", "trunk_link", "primary_link"}
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
OVERPASS_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "User-Agent": "StreetScopeResearch/0.1 (local thesis data workflow)",
}


def collect_osm_line_features(payload: Dict[str, object], keep_walkable: bool = True, exclude_high_speed: bool = True) -> List[RoadFeature]:
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return []
    nodes: Dict[int, List[float]] = {}
    ways: List[Dict[str, object]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "node" and isinstance(element.get("id"), int):
            lon = element.get("lon")
            lat = element.get("lat")
            if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
                nodes[int(element["id"])] = [float(lon), float(lat)]
        elif element.get("type") == "way":
            ways.append(element)

    roads: List[RoadFeature] = []
    for index, way in enumerate(ways, 1):
        tags = way.get("tags") if isinstance(way.get("tags"), dict) else {}
        highway = str(tags.get("highway", ""))
        if not highway:
            continue
        if exclude_high_speed and highway in HIGH_SPEED_HIGHWAYS:
            continue
        if keep_walkable and highway not in WALKABLE_HIGHWAYS:
            continue
        refs = way.get("nodes")
        if not isinstance(refs, list):
            continue
        coordinates = [nodes[int(ref)] for ref in refs if isinstance(ref, int) and int(ref) in nodes]
        if len(coordinates) < 2:
            continue
        road_id = str(way.get("id") or f"OSM{index:05d}")
        road_name = str(tags.get("name") or tags.get("name:zh") or tags.get("ref") or f"OSM {highway} {index}")
        roads.append(RoadFeature(road_id=f"OSM{road_id}", road_name=road_name, coordinates=coordinates))
    return roads


def fetch_overpass_json(query: str) -> Dict[str, object]:
    errors: List[str] = []
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            response = requests.post(endpoint, data=query.encode("utf-8"), headers=OVERPASS_HEADERS, timeout=45)
            if response.status_code >= 400:
                body = re.sub(r"\s+", " ", response.text).strip()
                errors.append(f"{endpoint} -> HTTP {response.status_code}: {body[:180] or response.reason}")
                continue
            return response.json()
        except requests.RequestException as exc:
            errors.append(f"{endpoint} -> {exc}")
        except json.JSONDecodeError as exc:
            errors.append(f"{endpoint} -> JSON 解析失败：{exc}")
    raise HTTPException(status_code=502, detail="OSM Overpass 请求失败；已尝试多个节点：" + " | ".join(errors))


def fetch_osm_map_payload(boundary: Boundary) -> Dict[str, object]:
    url = "https://api.openstreetmap.org/api/0.6/map"
    params = {
        "bbox": f"{boundary.west},{boundary.south},{boundary.east},{boundary.north}",
    }
    try:
        response = requests.get(url, params=params, headers={"User-Agent": OVERPASS_HEADERS["User-Agent"]}, timeout=45)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"OSM Map API 请求失败：{exc}") from exc
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail=f"OSM Map API XML 解析失败：{exc}") from exc

    elements: List[Dict[str, object]] = []
    for node in root.findall("node"):
        node_id = node.attrib.get("id")
        lat = node.attrib.get("lat")
        lon = node.attrib.get("lon")
        if not node_id or lat is None or lon is None:
            continue
        try:
            elements.append({"type": "node", "id": int(node_id), "lat": float(lat), "lon": float(lon)})
        except ValueError:
            continue

    for way in root.findall("way"):
        way_id = way.attrib.get("id")
        if not way_id:
            continue
        refs: List[int] = []
        tags: Dict[str, str] = {}
        for child in list(way):
            if child.tag == "nd":
                ref = child.attrib.get("ref")
                if ref:
                    try:
                        refs.append(int(ref))
                    except ValueError:
                        continue
            elif child.tag == "tag":
                key = child.attrib.get("k")
                value = child.attrib.get("v")
                if key and value is not None:
                    tags[key] = value
        try:
            elements.append({"type": "way", "id": int(way_id), "nodes": refs, "tags": tags})
        except ValueError:
            continue
    return {"elements": elements}


def polyline_length_m(coords: List[List[float]]) -> float:
    return sum(haversine_m(tuple(coords[idx]), tuple(coords[idx + 1])) for idx in range(len(coords) - 1))


def point_line_distance_m(point: List[float], start: List[float], end: List[float]) -> float:
    if start == end:
        return haversine_m(tuple(point), tuple(start))
    lat_ref = math.radians((point[1] + start[1] + end[1]) / 3)
    scale_x = 111_320 * max(math.cos(lat_ref), 0.2)
    scale_y = 111_320
    px, py = point[0] * scale_x, point[1] * scale_y
    sx, sy = start[0] * scale_x, start[1] * scale_y
    ex, ey = end[0] * scale_x, end[1] * scale_y
    dx, dy = ex - sx, ey - sy
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / max(dx * dx + dy * dy, 1e-9)))
    proj_x, proj_y = sx + t * dx, sy + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def simplify_polyline(coords: List[List[float]], tolerance_m: float = 2.0) -> List[List[float]]:
    if len(coords) <= 2:
        return coords
    max_distance = -1.0
    index = 0
    for idx in range(1, len(coords) - 1):
        distance = point_line_distance_m(coords[idx], coords[0], coords[-1])
        if distance > max_distance:
            index = idx
            max_distance = distance
    if max_distance > tolerance_m:
        left = simplify_polyline(coords[: index + 1], tolerance_m)
        right = simplify_polyline(coords[index:], tolerance_m)
        return left[:-1] + right
    return [coords[0], coords[-1]]


def point_in_boundary(point: List[float], boundary: Boundary, eps: float = 1e-12) -> bool:
    return boundary.west - eps <= point[0] <= boundary.east + eps and boundary.south - eps <= point[1] <= boundary.north + eps


def clip_segment_to_boundary(start: List[float], end: List[float], boundary: Boundary) -> Optional[List[List[float]]]:
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    dx = x1 - x0
    dy = y1 - y0
    p = [-dx, dx, -dy, dy]
    q = [x0 - boundary.west, boundary.east - x0, y0 - boundary.south, boundary.north - y0]
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-15:
            if qi < 0:
                return None
            continue
        t = qi / pi
        if pi < 0:
            u1 = max(u1, t)
        else:
            u2 = min(u2, t)
        if u1 > u2:
            return None
    clipped_start = [round(x0 + u1 * dx, 7), round(y0 + u1 * dy, 7)]
    clipped_end = [round(x0 + u2 * dx, 7), round(y0 + u2 * dy, 7)]
    if clipped_start == clipped_end:
        return None
    return [clipped_start, clipped_end]


def clip_roads_to_boundary(roads: List[RoadFeature], boundary: Boundary) -> List[RoadFeature]:
    clipped: List[RoadFeature] = []
    for road in roads:
        parts: List[List[List[float]]] = []
        current: List[List[float]] = []
        for idx in range(len(road.coordinates) - 1):
            segment = clip_segment_to_boundary(road.coordinates[idx], road.coordinates[idx + 1], boundary)
            if not segment:
                if current:
                    parts.append(current)
                    current = []
                continue
            if current and current[-1] != segment[0]:
                parts.append(current)
                current = []
            if not current:
                current.append(segment[0])
            current.append(segment[1])
        if current:
            parts.append(current)
        for part_index, part in enumerate(parts, 1):
            cleaned_part: List[List[float]] = []
            for point in part:
                if not cleaned_part or cleaned_part[-1] != point:
                    cleaned_part.append(point)
            if len(cleaned_part) >= 2:
                suffix = f"_{part_index}" if len(parts) > 1 else ""
                clipped.append(RoadFeature(road_id=f"{road.road_id}{suffix}", road_name=road.road_name, coordinates=cleaned_part))
    return clipped


def clean_road_features(roads: List[RoadFeature], min_length_m: float = 15.0, simplify_tolerance_m: float = 2.0) -> tuple[List[RoadFeature], Dict[str, float]]:
    cleaned: List[RoadFeature] = []
    seen: set[str] = set()
    duplicate_vertices_removed = 0
    short_removed = 0
    duplicate_removed = 0
    simplified_vertices_removed = 0
    for road in roads:
        coords: List[List[float]] = []
        last_key = ""
        for item in road.coordinates:
            if len(item) < 2:
                continue
            key = f"{round(float(item[0]), 7)},{round(float(item[1]), 7)}"
            if key == last_key:
                duplicate_vertices_removed += 1
                continue
            coords.append([round(float(item[0]), 7), round(float(item[1]), 7)])
            last_key = key
        if len(coords) < 2 or polyline_length_m(coords) < min_length_m:
            short_removed += 1
            continue
        simplified = simplify_polyline(coords, simplify_tolerance_m)
        simplified_vertices_removed += max(0, len(coords) - len(simplified))
        signature_parts = [f"{round(point[0], 5)},{round(point[1], 5)}" for point in simplified]
        signature = "|".join(signature_parts)
        reverse_signature = "|".join(reversed(signature_parts))
        canonical = min(signature, reverse_signature)
        if canonical in seen:
            duplicate_removed += 1
            continue
        seen.add(canonical)
        cleaned.append(RoadFeature(road_id=road.road_id, road_name=road.road_name, coordinates=simplified))
    report = {
        "raw_roads": len(roads),
        "cleaned_roads": len(cleaned),
        "short_roads_removed": short_removed,
        "duplicate_roads_removed": duplicate_removed,
        "duplicate_vertices_removed": duplicate_vertices_removed,
        "simplified_vertices_removed": simplified_vertices_removed,
    }
    return cleaned, report


def sample_roads_to_response(roads: List[RoadFeature], interval_m: int, source: str, admin_name: str, clean_roads: bool = False) -> SampleResponse:
    cleaning_report: Dict[str, float] = {}
    if clean_roads:
        roads, cleaning_report = clean_road_features(roads)
    points: List[SamplePoint] = []
    for road in roads:
        for lng, lat in sample_polyline(road.coordinates, interval_m):
            points.append(
                build_sample_point(
                    point_id=f"P{len(points)+1:06d}",
                    lng=lng,
                    lat=lat,
                    road_id=road.road_id,
                    road_name=road.road_name,
                    admin_name=admin_name,
                    sample_interval=interval_m,
                    source=source,
                )
            )
    response = response_from_points(points, roads, source)
    response.estimate.update(cleaning_report)
    response.estimate["road_cleaning_enabled"] = 1 if clean_roads else 0
    return response


def collect_shapefile_line_features(raw_zip: bytes) -> List[RoadFeature]:
    features: List[RoadFeature] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
                archive.extractall(tmp_path)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="SHP 需要以 .zip 形式上传") from exc
        shp_files = list(tmp_path.rglob("*.shp"))
        if not shp_files:
            raise HTTPException(status_code=400, detail="ZIP 中没有找到 .shp 文件")
        reader = shapefile.Reader(str(shp_files[0]))
        fields = [field[0] for field in reader.fields[1:]]
        lower_fields = {field.lower(): index for index, field in enumerate(fields)}

        def field_value(record: object, candidates: List[str], fallback: str) -> str:
            values = list(record)
            for name in candidates:
                index = lower_fields.get(name.lower())
                if index is not None and index < len(values) and values[index] not in ("", None):
                    return str(values[index])
            return fallback

        for idx, shape_record in enumerate(reader.iterShapeRecords(), 1):
            shape = shape_record.shape
            if shape.shapeType not in {
                shapefile.POLYLINE,
                shapefile.POLYLINEZ,
                shapefile.POLYLINEM,
            }:
                continue
            points = [[float(lng), float(lat)] for lng, lat, *_ in shape.points]
            parts = list(shape.parts) + [len(points)]
            road_name = field_value(shape_record.record, ["road_name", "name", "道路名", "road"], f"上传SHP路网 {idx}")
            road_id = field_value(shape_record.record, ["road_id", "id", "道路编号"], f"SHP{idx:04d}")
            for part_index in range(len(parts) - 1):
                part = points[parts[part_index] : parts[part_index + 1]]
                if len(part) >= 2:
                    suffix = f"_{part_index + 1}" if len(parts) > 2 else ""
                    features.append(RoadFeature(road_id=f"{road_id}{suffix}", road_name=road_name, coordinates=part))
    return features


def collect_shapefile_sample_points(raw_zip: bytes, coord_type: str) -> List[SamplePoint]:
    points: List[SamplePoint] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
                archive.extractall(tmp_path)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="SHP 需要以 .zip 形式上传") from exc
        shp_files = list(tmp_path.rglob("*.shp"))
        if not shp_files:
            raise HTTPException(status_code=400, detail="ZIP 中没有找到 .shp 文件")
        reader = shapefile.Reader(str(shp_files[0]))
        fields = [field[0] for field in reader.fields[1:]]
        lower_fields = {field.lower(): index for index, field in enumerate(fields)}

        def field_value(record: object, candidates: List[str], fallback: str) -> str:
            values = list(record)
            for name in candidates:
                index = lower_fields.get(name.lower())
                if index is not None and index < len(values) and values[index] not in ("", None):
                    return str(values[index])
            return fallback

        for idx, shape_record in enumerate(reader.iterShapeRecords(), 1):
            shape = shape_record.shape
            if shape.shapeType not in {
                shapefile.POINT,
                shapefile.POINTZ,
                shapefile.POINTM,
                shapefile.MULTIPOINT,
                shapefile.MULTIPOINTZ,
                shapefile.MULTIPOINTM,
            }:
                continue
            for point_index, raw_point in enumerate(shape.points, 1):
                if len(raw_point) < 2:
                    continue
                lng = float(raw_point[0])
                lat = float(raw_point[1])
                suffix = f"_{point_index}" if len(shape.points) > 1 else ""
                point_id = field_value(shape_record.record, ["point_id", "id", "编号"], f"SHP{idx:06d}{suffix}")
                road_name = field_value(shape_record.record, ["road_name", "name", "道路名", "road"], "用户上传点")
                road_id = field_value(shape_record.record, ["road_id", "道路编号"], "UPLOADED")
                admin_code = field_value(shape_record.record, ["admin_code", "行政区划代码"], "")
                admin_name = field_value(shape_record.record, ["admin_name", "行政区", "区县"], "用户上传")
                if coord_type == "bd09":
                    points.append(
                        SamplePoint(
                            point_id=str(point_id),
                            lng=round(lng, 7),
                            lat=round(lat, 7),
                            coord_type="bd09",
                            lng_wgs84=round(lng, 7),
                            lat_wgs84=round(lat, 7),
                            lng_gcj02=round(lng, 7),
                            lat_gcj02=round(lat, 7),
                            lng_bd09=round(lng, 7),
                            lat_bd09=round(lat, 7),
                            road_id=road_id,
                            road_name=road_name,
                            admin_code=admin_code,
                            admin_name=admin_name,
                            sample_interval=0,
                            source="uploaded_shp",
                        )
                    )
                else:
                    points.append(
                        build_sample_point(
                            point_id=str(point_id),
                            lng=lng,
                            lat=lat,
                            road_id=road_id,
                            road_name=road_name,
                            admin_code=admin_code,
                            admin_name=admin_name,
                            sample_interval=0,
                            source="uploaded_shp",
                        )
                    )
    return points


def boundary_from_coordinates(coordinates: List[List[float]]) -> Boundary:
    if not coordinates:
        raise HTTPException(status_code=400, detail="没有识别到边界坐标")
    lngs = [float(item[0]) for item in coordinates]
    lats = [float(item[1]) for item in coordinates]
    west = min(lngs)
    east = max(lngs)
    south = min(lats)
    north = max(lats)
    if east <= west or north <= south:
        raise HTTPException(status_code=400, detail="边界坐标范围无效")
    return Boundary(west=round(west, 7), east=round(east, 7), south=round(south, 7), north=round(north, 7))


def collect_geojson_coordinates(geojson: object) -> List[List[float]]:
    coordinates: List[List[float]] = []

    def collect(value: object) -> None:
        if not isinstance(value, list):
            return
        if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
            coordinates.append([float(value[0]), float(value[1])])
            return
        for item in value:
            collect(item)

    if isinstance(geojson, dict):
        collect(geojson.get("coordinates"))
        geometry = geojson.get("geometry")
        if isinstance(geometry, dict):
            collect(geometry.get("coordinates"))
        features = geojson.get("features")
        if isinstance(features, list):
            for feature in features:
                if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict):
                    collect(feature["geometry"].get("coordinates"))
    return coordinates


def collect_kml_coordinates(text: str) -> List[List[float]]:
    coordinates: List[List[float]] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"KML 解析失败：{exc}") from exc
    for element in root.iter():
        if not element.tag.lower().endswith("coordinates") or not element.text:
            continue
        for chunk in element.text.replace("\n", " ").replace("\t", " ").split():
            parts = chunk.split(",")
            if len(parts) >= 2:
                try:
                    coordinates.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    continue
    return coordinates


def collect_shapefile_boundary_coordinates(raw_zip: bytes) -> List[List[float]]:
    coordinates: List[List[float]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
                archive.extractall(tmp_path)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="SHP 需要以 .zip 形式上传") from exc
        shp_files = list(tmp_path.rglob("*.shp"))
        if not shp_files:
            raise HTTPException(status_code=400, detail="ZIP 中没有找到 .shp 文件")
        reader = shapefile.Reader(str(shp_files[0]))
        for shape in reader.shapes():
            if shape.shapeType in {
                shapefile.POLYGON,
                shapefile.POLYGONZ,
                shapefile.POLYGONM,
                shapefile.POLYLINE,
                shapefile.POLYLINEZ,
                shapefile.POLYLINEM,
            }:
                coordinates.extend([[float(lng), float(lat)] for lng, lat, *_ in shape.points])
    return coordinates


def response_from_points(points: List[SamplePoint], roads: List[RoadFeature], source: str) -> SampleResponse:
    if points:
        lngs = [p.lng for p in points]
        lats = [p.lat for p in points]
        center_lat = sum(lats) / len(lats)
        width_m = (max(lngs) - min(lngs)) * 111_320 * max(math.cos(math.radians(center_lat)), 0.2)
        height_m = (max(lats) - min(lats)) * 111_320
        area_km2 = (width_m * height_m) / 1_000_000
    else:
        area_km2 = 0
    road_length_m = 0.0
    for road in roads:
        for idx in range(len(road.coordinates) - 1):
            road_length_m += haversine_m(tuple(road.coordinates[idx]), tuple(road.coordinates[idx + 1]))
    estimate = {
        "area_km2": round(area_km2, 3),
        "road_length_km": round(road_length_m / 1000, 2),
        "sample_points": len(points),
        "four_direction_images": len(points) * 4,
    }
    return SampleResponse(points=points, roads=roads, estimate=estimate, source=source)


def build_sample_points(req: SampleRequest) -> SampleResponse:
    raise HTTPException(status_code=410, detail="生产模式已禁用内置网格采样，请使用 OSM 路网、导入路网或导入已有采样点")


def normalize_capture_date(value: object) -> str:
    text = str(value or "latest").strip()
    if not text or text.lower() in {"latest", "newest", "current", "最新", "最新可用"}:
        return "latest"
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        return text
    return "latest"


def capture_date_query(value: object) -> str:
    capture_date = normalize_capture_date(value)
    return "" if capture_date == "latest" else f"&date={quote(capture_date)}"


def baidu_url(req: DownloadRequest, point: SamplePoint, heading: int) -> str:
    lng = point.lng_bd09 if req.coordtype == "bd09ll" else point.lng
    lat = point.lat_bd09 if req.coordtype == "bd09ll" else point.lat
    return (
        "https://api.map.baidu.com/panorama/v2?"
        f"ak={req.ak}&width={req.width}&height={req.height}"
        f"&location={lng},{lat}&coordtype={req.coordtype}"
        f"&heading={heading}&pitch={req.pitch}&fov={req.fov}"
        f"{capture_date_query(req.capture_date)}"
    )


def baidu_test_url(req: BaiduTestRequest) -> str:
    point = req.point or SamplePoint(
        point_id="TEST",
        lng=121.4737,
        lat=31.2304,
        lng_bd09=121.4802,
        lat_bd09=31.2364,
        road_id="TEST",
        road_name="人民广场测试点",
        admin_name="上海市",
        sample_interval=0,
        source="test",
    )
    lng = point.lng_bd09 if req.coordtype == "bd09ll" else point.lng
    lat = point.lat_bd09 if req.coordtype == "bd09ll" else point.lat
    return (
        "https://api.map.baidu.com/panorama/v2?"
        f"ak={req.ak}&width={req.width}&height={req.height}"
        f"&location={lng},{lat}&coordtype={req.coordtype}"
        f"&heading={req.heading}&pitch={req.pitch}&fov={req.fov}"
    )


WEB_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Mozilla/5.0 (Linux; Android 13; SM-S901B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
]


def baidu_web_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(WEB_USER_AGENTS),
        "Referer": "https://map.baidu.com/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }


def baidu_web_panoid_url(point: SamplePoint, capture_date: object = "latest") -> str:
    x, y = bd09_to_mercator(point.lng_bd09, point.lat_bd09)
    return f"https://mapsv0.bdimg.com/?&qt=qsdata&x={x}&y={y}&l=17.031000000000002&action=0&mode=day&t={int(time.time() * 1000)}{capture_date_query(capture_date)}"


def baidu_web_image_url(panoid: str, heading: int, pitch: int, width: int, height: int, capture_date: object = "latest") -> str:
    return f"https://mapsv0.bdimg.com/?qt=pr3d&fovy=90&quality=100&panoid={panoid}&heading={heading}&pitch={pitch}&width={width}&height={height}{capture_date_query(capture_date)}"


BAIDU_WEB_NATIVE_PANORAMA_LEVEL = 4
BAIDU_WEB_NATIVE_TILE_SIZE = 512


def baidu_web_panorama_tile_url(panoid: str, x: int, y: int, level: int = BAIDU_WEB_NATIVE_PANORAMA_LEVEL, capture_date: object = "latest") -> str:
    return f"https://mapsv0.bdimg.com/?qt=pdata&sid={panoid}&pos={y}_{x}&z={level}&from=PC{capture_date_query(capture_date)}"


def baidu_web_panorama_grid(level: int = BAIDU_WEB_NATIVE_PANORAMA_LEVEL) -> tuple[int, int]:
    if level <= 1:
        return 1, 1
    if level == 2:
        return 1, 1
    rows = 2 ** (level - 2)
    return 2 * rows, rows


def download_baidu_web_native_panorama(panoid: str, output_path: Path, level: int = BAIDU_WEB_NATIVE_PANORAMA_LEVEL, capture_date: object = "latest") -> tuple[str, int, int, str]:
    cols, rows = baidu_web_panorama_grid(level)
    panorama = Image.new("RGB", (cols * BAIDU_WEB_NATIVE_TILE_SIZE, rows * BAIDU_WEB_NATIVE_TILE_SIZE))
    total_bytes = 0
    http_status = 200
    first_url = baidu_web_panorama_tile_url(panoid, 0, 0, level, capture_date)
    headers = baidu_web_headers()
    for y in range(rows):
        for x in range(cols):
            url = baidu_web_panorama_tile_url(panoid, x, y, level, capture_date)
            response = requests.get(url, headers=headers, timeout=20)
            http_status = response.status_code
            total_bytes += len(response.content)
            if not response.ok or response.content[:2] != b"\xff\xd8":
                raise RuntimeError(f"百度原生全景瓦片下载失败：z={level}, pos={y}_{x}, HTTP {response.status_code}")
            tile = Image.open(io.BytesIO(response.content)).convert("RGB")
            panorama.paste(tile, (x * BAIDU_WEB_NATIVE_TILE_SIZE, y * BAIDU_WEB_NATIVE_TILE_SIZE))
            tile.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    panorama.save(output_path, format="JPEG", quality=92, optimize=True)
    panorama.close()
    return first_url, http_status, total_bytes, f"image/jpeg; native-pdata-level={level}; tiles={cols}x{rows}; projection=equirectangular"


def baidu_web_get_panoid(point: SamplePoint, capture_date: object = "latest") -> tuple[Optional[str], str]:
    url = baidu_web_panoid_url(point, capture_date)
    response = requests.get(url, headers=baidu_web_headers(), timeout=12)
    response.raise_for_status()
    text = response.content.decode("utf-8", errors="ignore")
    match = re.search(r'"id":"(.+?)",', text)
    return (match.group(1) if match else None), url


def redact_api_key(url: str) -> str:
    if "ak=" not in url:
        return url
    prefix, rest = url.split("ak=", 1)
    if "&" in rest:
        _, suffix = rest.split("&", 1)
        return f"{prefix}ak=***&{suffix}"
    return f"{prefix}ak=***"


METRIC_KEYS = [
    "gvi",
    "bvi",
    "sky_ratio",
    "water_ratio",
    "building_ratio",
    "road_ratio",
    "sidewalk_ratio",
    "vehicle_ratio",
    "person_ratio",
    "enclosure_ratio",
    "natural_ratio",
    "vehicle_space_ratio",
    "hardscape_ratio",
    "human_vehicle_density",
    "visual_entropy",
    "cvi",
    "rgb_mean_r",
    "rgb_mean_g",
    "rgb_mean_b",
    "rgb_var_r",
    "rgb_var_g",
    "rgb_var_b",
    "hsv_mean_h",
    "hsv_mean_s",
    "hsv_mean_v",
]

SHP_METRIC_FIELDS = {
    "gvi": "gvi",
    "bvi": "bvi",
    "sky_ratio": "sky_rat",
    "water_ratio": "water_rat",
    "building_ratio": "build_rat",
    "road_ratio": "road_rat",
    "sidewalk_ratio": "sidew_rat",
    "vehicle_ratio": "veh_rat",
    "person_ratio": "pers_rat",
    "enclosure_ratio": "encl_rat",
    "natural_ratio": "nat_rat",
    "vehicle_space_ratio": "veh_space",
    "hardscape_ratio": "hard_rat",
    "human_vehicle_density": "hv_dens",
    "visual_entropy": "vis_ent",
    "cvi": "cvi",
    "rgb_mean_r": "rgb_m_r",
    "rgb_mean_g": "rgb_m_g",
    "rgb_mean_b": "rgb_m_b",
    "rgb_var_r": "rgb_v_r",
    "rgb_var_g": "rgb_v_g",
    "rgb_var_b": "rgb_v_b",
    "hsv_mean_h": "hsv_m_h",
    "hsv_mean_s": "hsv_m_s",
    "hsv_mean_v": "hsv_m_v",
}


def clamp_ratio(value: float) -> float:
    return min(1.0, max(0.0, round(float(value), 4)))


def normalize_metric_ratios(raw: Dict[str, object]) -> Dict[str, float]:
    def value(key: str, fallback: float) -> float:
        try:
            return round(float(raw.get(key, fallback) or fallback), 4)
        except (TypeError, ValueError):
            return round(fallback, 4)

    gvi = value("gvi", value("vegetation_ratio", 0.25))
    sky = value("sky_ratio", 0.22)
    water = value("water_ratio", 0.015)
    building = value("building_ratio", 0.28)
    road = value("road_ratio", 0.18)
    sidewalk = value("sidewalk_ratio", 0.08)
    vehicle = value("vehicle_ratio", value("car_ratio", 0.018))
    person = value("person_ratio", value("pedestrian_ratio", 0.01))
    terrain = value("terrain_ratio", 0.018)
    wall = value("wall_ratio", 0.012)
    fence = value("fence_ratio", 0.006)
    rider = value("rider_ratio", 0.004)
    bvi = value("bvi", sky + water)
    enclosure = value("enclosure_ratio", building + wall + fence)
    natural = value("natural_ratio", gvi + water + terrain * 0.35)
    vehicle_space = value("vehicle_space_ratio", road + vehicle)
    hardscape = value("hardscape_ratio", road + sidewalk + terrain + wall)
    human_vehicle_density = value("human_vehicle_density", person + vehicle + rider)
    class_values = [gvi, sky, water, building, road, sidewalk, vehicle, person, terrain, wall, fence]
    entropy = value("visual_entropy", -sum(p * math.log(max(p, 0.001)) for p in class_values if p > 0))
    rgb_mean_r = value("rgb_mean_r", 126 + building * 45 + road * 20 - gvi * 28)
    rgb_mean_g = value("rgb_mean_g", 122 + gvi * 72 + sky * 18 - road * 16)
    rgb_mean_b = value("rgb_mean_b", 124 + sky * 70 + water * 35 - road * 18)
    rgb_var_r = value("rgb_var_r", 420 + entropy * 85 + building * 160)
    rgb_var_g = value("rgb_var_g", 430 + entropy * 90 + gvi * 220)
    rgb_var_b = value("rgb_var_b", 440 + entropy * 95 + sky * 230)
    hsv_mean_h = value("hsv_mean_h", 82 + gvi * 70 + sky * 45)
    hsv_mean_s = value("hsv_mean_s", 0.22 + gvi * 0.32 + water * 0.18)
    hsv_mean_v = value("hsv_mean_v", 0.48 + sky * 0.22 + road * 0.05)
    cvi = value("cvi", (rgb_var_r + rgb_var_g + rgb_var_b) / 1500 + hsv_mean_s * 0.35)
    return {
        "gvi": clamp_ratio(gvi),
        "bvi": clamp_ratio(bvi),
        "sky_ratio": clamp_ratio(sky),
        "water_ratio": clamp_ratio(water),
        "building_ratio": clamp_ratio(building),
        "road_ratio": clamp_ratio(road),
        "sidewalk_ratio": clamp_ratio(sidewalk),
        "vehicle_ratio": clamp_ratio(vehicle),
        "person_ratio": clamp_ratio(person),
        "enclosure_ratio": clamp_ratio(enclosure),
        "natural_ratio": clamp_ratio(natural),
        "vehicle_space_ratio": clamp_ratio(vehicle_space),
        "hardscape_ratio": clamp_ratio(hardscape),
        "human_vehicle_density": clamp_ratio(human_vehicle_density),
        "visual_entropy": round(max(0.0, entropy), 4),
        "cvi": round(max(0.0, cvi), 4),
        "rgb_mean_r": round(max(0.0, min(255.0, rgb_mean_r)), 4),
        "rgb_mean_g": round(max(0.0, min(255.0, rgb_mean_g)), 4),
        "rgb_mean_b": round(max(0.0, min(255.0, rgb_mean_b)), 4),
        "rgb_var_r": round(max(0.0, rgb_var_r), 4),
        "rgb_var_g": round(max(0.0, rgb_var_g), 4),
        "rgb_var_b": round(max(0.0, rgb_var_b), 4),
        "hsv_mean_h": round(max(0.0, min(360.0, hsv_mean_h)), 4),
        "hsv_mean_s": clamp_ratio(hsv_mean_s),
        "hsv_mean_v": clamp_ratio(hsv_mean_v),
    }


def segmentation_health_url(service_url: str) -> str:
    parsed = urlparse(service_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("模型服务地址格式不正确，请填写类似 http://host:9000/segment 的地址")
    return urlunparse((parsed.scheme, parsed.netloc, "/health", "", "", ""))


def check_external_segmentation_service(service_url: str) -> None:
    health_url = segmentation_health_url(service_url)
    try:
        response = requests.get(health_url, timeout=8)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"模型服务不可用：{health_url} 无法正常响应（{exc}）") from exc


def call_external_segmentation_service(image_bytes: bytes, file_name: str, model_name: str, service_url: str) -> Dict[str, object]:
    if not service_url.strip():
        raise HTTPException(status_code=400, detail="外部推理模式需要填写分割服务地址")
    try:
        response = requests.post(
            service_url.strip(),
            data={"model_name": model_name},
            files={"image": (file_name, image_bytes, "image/jpeg")},
            timeout=(15, 180),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"外部分割服务请求失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("外部分割服务返回不是 JSON") from exc
    ratios = payload.get("metrics", payload) if isinstance(payload, dict) else {}
    if not isinstance(ratios, dict):
        raise RuntimeError("外部分割服务 JSON 缺少 metrics 对象")
    result: Dict[str, object] = normalize_metric_ratios(ratios)
    if isinstance(payload, dict):
        for key in ("mask_png_base64", "overlay_png_base64", "model_id", "device"):
            if key in payload:
                result[key] = payload[key]
    return result


def decode_png_base64(value: object) -> Optional[bytes]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None


def write_segmentation_artifacts(task: TaskState, point_id: str, heading_label: str, segmentation: Dict[str, object]) -> tuple[str, str]:
    artifact_dir = DATA_DIR / task.task_id / "segmentation_masks"
    mask_dir = artifact_dir / "masks"
    overlay_dir = artifact_dir / "overlays"
    mask_dir.mkdir(parents=True, exist_ok=True)
    overlay_dir.mkdir(parents=True, exist_ok=True)
    mask_file = f"{safe_file_part(point_id)}_{safe_file_part(heading_label)}_mask.png"
    overlay_file = f"{safe_file_part(point_id)}_{safe_file_part(heading_label)}_overlay.png"
    mask_bytes = decode_png_base64(segmentation.get("mask_png_base64"))
    overlay_bytes = decode_png_base64(segmentation.get("overlay_png_base64"))
    if not mask_bytes or not overlay_bytes:
        raise RuntimeError("外部分割服务没有返回真实 mask/overlay PNG")
    (mask_dir / mask_file).write_bytes(mask_bytes)
    (overlay_dir / overlay_file).write_bytes(overlay_bytes)
    return mask_file, overlay_file


PANORAMA_HEADINGS = [0, 90, 180, 270]
CUBE_FACE_HEADINGS = [0, 90, 180, 270]
CUBE_FACE_NAMES = ["front", "right", "back", "left"]
HORIZONTAL_FACE_SIZE = 1024
HORIZONTAL_FACE_HFOV = 90
HORIZONTAL_FACE_VFOV = 55
HORIZONTAL_FACE_PITCH = 10


def equirectangular_to_cube_face(
    image: Image.Image,
    yaw_degrees: float,
    face_size: int = HORIZONTAL_FACE_SIZE,
    horizontal_fov: float = HORIZONTAL_FACE_HFOV,
    vertical_fov: float = HORIZONTAL_FACE_VFOV,
    pitch_degrees: float = HORIZONTAL_FACE_PITCH,
) -> Image.Image:
    source = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width = source.shape[:2]
    x_extent = math.tan(math.radians(horizontal_fov) / 2)
    y_extent = math.tan(math.radians(vertical_fov) / 2)
    xs = np.linspace(-x_extent, x_extent, face_size, dtype=np.float32)
    ys = np.linspace(y_extent, -y_extent, face_size, dtype=np.float32)
    xx, yy = np.meshgrid(xs, ys)
    zz = np.ones_like(xx)
    norm = np.sqrt(xx * xx + yy * yy + zz * zz)
    xx, yy, zz = xx / norm, yy / norm, zz / norm

    pitch = math.radians(pitch_degrees)
    sin_pitch = math.sin(pitch)
    cos_pitch = math.cos(pitch)
    pitched_y = yy * cos_pitch + zz * sin_pitch
    pitched_z = -yy * sin_pitch + zz * cos_pitch

    yaw = math.radians(yaw_degrees)
    sin_yaw = math.sin(yaw)
    cos_yaw = math.cos(yaw)
    world_x = xx * cos_yaw + pitched_z * sin_yaw
    world_z = -xx * sin_yaw + pitched_z * cos_yaw
    world_y = pitched_y

    longitude = np.arctan2(world_x, world_z)
    latitude = np.arcsin(np.clip(world_y, -1.0, 1.0))
    src_x = (longitude / (2 * math.pi) + 0.5) * width
    src_y = (0.5 - latitude / math.pi) * height

    x0 = np.floor(src_x).astype(np.int32) % width
    x1 = (x0 + 1) % width
    y0 = np.clip(np.floor(src_y).astype(np.int32), 0, height - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    wx = (src_x - np.floor(src_x))[..., None]
    wy = (src_y - np.floor(src_y))[..., None]

    top = source[y0, x0] * (1 - wx) + source[y0, x1] * wx
    bottom = source[y1, x0] * (1 - wx) + source[y1, x1] * wx
    sampled = top * (1 - wy) + bottom * wy
    return Image.fromarray(np.clip(sampled, 0, 255).astype(np.uint8))


def native_panorama_to_horizontal_cube_strip(image_path: Path, point_id: str, output_dir: Path, face_size: int = HORIZONTAL_FACE_SIZE) -> Dict[str, object]:
    if not image_path.exists():
        raise RuntimeError(f"原生全景图不存在：{image_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    source = Image.open(image_path).convert("RGB")
    try:
        faces = [equirectangular_to_cube_face(source, yaw, face_size=face_size) for yaw in CUBE_FACE_HEADINGS]
        strip = Image.new("RGB", (face_size * len(faces), face_size))
        x = 0
        for face in faces:
            strip.paste(face, (x, 0))
            x += face_size
        file_name = f"{safe_file_part(point_id)}_cube_faces_front_right_back_left.jpg"
        file_path = output_dir / file_name
        strip.save(file_path, format="JPEG", quality=92, optimize=True)
    finally:
        source.close()
        for face in locals().get("faces", []):
            face.close()
        if "strip" in locals():
            strip.close()
    return {
        "file_name": file_name,
        "file_path": str(file_path),
        "image_type": "cube_horizontal_faces",
        "heading": 360,
        "heading_label": "cube_faces",
        "source_headings": ",".join(str(item) for item in CUBE_FACE_HEADINGS),
        "cube_faces": ",".join(CUBE_FACE_NAMES),
        "cube_excluded_faces": "top,bottom",
        "projection_hfov": HORIZONTAL_FACE_HFOV,
        "projection_vfov": HORIZONTAL_FACE_VFOV,
        "projection_pitch": HORIZONTAL_FACE_PITCH,
    }


def ordered_direction_images(point_id: str, images: List[Dict[str, object]]) -> List[Dict[str, object]]:
    by_heading: Dict[int, Dict[str, object]] = {}
    for image in images:
        try:
            heading = int(image.get("heading", 0) or 0)
        except (TypeError, ValueError):
            continue
        if heading in PANORAMA_HEADINGS and heading not in by_heading:
            by_heading[heading] = image
    missing = [heading for heading in PANORAMA_HEADINGS if heading not in by_heading]
    if missing:
        raise RuntimeError(f"{point_id} 缺少四方向图像：{missing}")
    for heading in PANORAMA_HEADINGS:
        path = Path(str(by_heading[heading].get("file_path") or ""))
        if not path.exists():
            raise RuntimeError(f"{point_id} 的 {heading}° 图像不存在：{path}")
    return [by_heading[heading] for heading in PANORAMA_HEADINGS]


def stitch_direction_images(point_id: str, images: List[Dict[str, object]], output_dir: Path) -> Dict[str, object]:
    ordered = ordered_direction_images(point_id, images)
    opened: List[Image.Image] = []
    try:
        for image in ordered:
            opened.append(Image.open(Path(str(image.get("file_path") or ""))).convert("RGB"))
        target_height = min(image.height for image in opened)
        resized = [
            image.resize((round(image.width * target_height / image.height), target_height), Image.Resampling.LANCZOS)
            for image in opened
        ]
        panorama = Image.new("RGB", (sum(image.width for image in resized), target_height))
        x = 0
        for image in resized:
            panorama.paste(image, (x, 0))
            x += image.width
        output_dir.mkdir(parents=True, exist_ok=True)
        first = ordered[0]
        file_name = f"{safe_file_part(str(first.get('project_name') or 'streetscope'))}_{safe_file_part(point_id)}_direction_preview_0_90_180_270.jpg"
        file_path = output_dir / file_name
        panorama.save(file_path, format="JPEG", quality=92, optimize=True)
    finally:
        for image in opened:
            image.close()

    first = ordered[0]
    return {
        **first,
        "image_id": f"{point_id}_direction_preview",
        "point_id": point_id,
        "heading": 360,
        "heading_label": "direction_preview",
        "image_type": "direction_preview",
        "file_name": file_name,
        "file_path": str(file_path),
        "source_headings": ",".join(str(item) for item in PANORAMA_HEADINGS),
        "analysis_note": "四方向图仅横向并排作质检预览；真实指标按 0/90/180/270 四张原图分别分割后汇总",
    }


def download_work_items(req: DownloadRequest) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    if req.image_mode in {"directions", "stitched"}:
        for point in req.points:
            for heading in req.headings:
                items.append({"point": point, "heading": int(heading), "image_type": "direction", "label": str(heading)})
    if req.image_mode == "panorama":
        for point in req.points:
            items.append({"point": point, "heading": 0, "image_type": "panorama", "label": "pano"})
    return items


def download_item_key(point_id: object, image_type: object, label: object) -> str:
    return f"{point_id}|{image_type}|{label}"


def existing_download_keys(task: TaskState) -> set[str]:
    return {
        download_item_key(record.get("point_id"), record.get("image_type"), record.get("heading"))
        for record in task.records
        if record.get("status") in {"success", "failed"}
    }


def recompute_download_counts(task: TaskState) -> None:
    if task.kind != "download":
        return
    task.succeeded = sum(1 for record in task.records if record.get("status") == "success")
    task.failed = sum(1 for record in task.records if record.get("status") == "failed")
    if task.total:
        task.progress = min(100, round((task.succeeded + task.failed) / max(task.total, 1) * 100))


def download_request_from_task(task: TaskState, ak: Optional[str] = None) -> DownloadRequest:
    params = task.meta.get("image_params", {})
    points_raw = task.meta.get("points", [])
    roads_raw = task.meta.get("roads", [])
    boundary_raw = task.meta.get("boundary")
    if not isinstance(params, dict) or not isinstance(points_raw, list):
        raise RuntimeError("缺少下载任务元数据，无法继续")
    points = [SamplePoint(**point) for point in points_raw if isinstance(point, dict)]
    roads = [RoadFeature(**road) for road in roads_raw if isinstance(road, dict)] if isinstance(roads_raw, list) else []
    boundary = Boundary(**boundary_raw) if isinstance(boundary_raw, dict) else None
    provider = str(params.get("provider") or "baidu_web")
    use_real_baidu = provider == "baidu"
    if use_real_baidu and not ak:
        ak = str(params.get("ak") or "")
    return DownloadRequest(
        project_name=str(task.meta.get("project_name") or "streetscope"),
        ak=ak or "",
        use_real_baidu=use_real_baidu,
        provider=provider if provider in {"baidu", "baidu_web"} else "baidu_web",
        points=points,
        boundary=boundary,
        roads=roads,
        headings=[int(value) for value in task.meta.get("headings", []) if str(value).lstrip("-").isdigit()],
        width=int(params.get("width", 1024) or 1024),
        height=int(params.get("height", 512) or 512),
        pitch=int(params.get("pitch", 0) or 0),
        fov=int(params.get("fov", 90) or 90),
        coordtype=params.get("coordtype", "bd09ll"),
        image_mode=params.get("image_mode", "directions"),
        capture_date=normalize_capture_date(params.get("capture_date", "latest")),
        skip_existing=bool(params.get("skip_existing", True)),
        concurrency=int(params.get("concurrency", 2) or 2),
        retry_count=int(params.get("retry_count", 1) or 1),
    )


def safe_file_part(value: object) -> str:
    text = str(value).replace("/", "-").replace("\\", "-").replace(" ", "_")
    return "".join(char for char in text if char.isalnum() or char in "._-一二三四五六七八九十路街大道区县市省镇乡村")


def image_archive_key(req: DownloadRequest, point: SamplePoint, heading: int, image_type: str, label: str) -> str:
    payload = {
        "provider": req.provider,
        "point_id": point.point_id,
        "lng": round(point.lng, 7),
        "lat": round(point.lat, 7),
        "lng_bd09": round(point.lng_bd09, 7),
        "lat_bd09": round(point.lat_bd09, 7),
        "heading": label,
        "image_type": image_type,
        "width": req.width,
        "height": req.height,
        "pitch": req.pitch,
        "fov": 360 if image_type == "panorama" else req.fov,
        "coordtype": req.coordtype,
        "capture_date": normalize_capture_date(req.capture_date),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def download_one_image(req: DownloadRequest, point: SamplePoint, heading: int, image_type: str, label: str, project_dir: Path) -> Dict[str, object]:
    capture_date = normalize_capture_date(req.capture_date)
    capture_date_label = capture_date if capture_date != "latest" else "latest"
    archive_key = image_archive_key(req, point, heading, image_type, label)
    file_name = "_".join(
        [
            safe_file_part(req.project_name),
            safe_file_part(point.point_id),
            safe_file_part(point.road_name),
            f"{point.lng:.7f}",
            f"{point.lat:.7f}",
            safe_file_part(label),
            safe_file_part(capture_date_label),
        ]
    ) + ".jpg"
    file_path = project_dir / file_name
    archive_path = IMAGE_ARCHIVE_DIR / f"{archive_key}.jpg"
    ok = True
    error = ""
    request_url = ""
    http_status = 0
    content_type = ""
    response_bytes = 0
    requested_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    started = time.perf_counter()
    attempts = 0
    download_action = "downloaded"
    panoid = ""
    panoid_url = ""
    use_official_baidu = req.provider == "baidu" or req.use_real_baidu

    if req.skip_existing and file_path.exists() and file_path.is_file():
        response_bytes = file_path.stat().st_size
        content_type = "image/jpeg"
        request_url = "local://existing"
        download_action = "skipped_existing"
    elif req.skip_existing and archive_path.exists() and archive_path.is_file():
        shutil.copy2(archive_path, file_path)
        response_bytes = file_path.stat().st_size
        content_type = "image/jpeg"
        request_url = f"archive://streetview/{archive_key}"
        download_action = "reused_archive"
    elif use_official_baidu and req.ak:
        request_req = req.model_copy(update={"fov": 360}) if image_type == "panorama" else req
        raw_url = baidu_url(request_req, point, heading)
        request_url = redact_api_key(raw_url)
        max_attempts = max(1, req.retry_count + 1)
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                response = requests.get(raw_url, timeout=12)
                http_status = response.status_code
                content_type = response.headers.get("content-type", "")
                response_bytes = len(response.content)
                if response.ok and "image" in content_type:
                    file_path.write_bytes(response.content)
                    ok = True
                    error = ""
                    break
                ok = False
                error = f"HTTP {response.status_code}: {response.text[:80]}"
            except requests.RequestException as exc:
                ok = False
                error = str(exc)
            if attempt < max_attempts:
                time.sleep(0.25)
    elif req.provider == "baidu_web":
        max_attempts = max(1, req.retry_count + 1)
        panoid = None
        panoid_url = ""
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                if not panoid:
                    found_panoid, panoid_url = baidu_web_get_panoid(point, capture_date)
                    panoid = found_panoid or ""
                if not panoid:
                    ok = False
                    error = "未查询到 panoid，当前点可能没有百度街景"
                    break
                if image_type == "panorama":
                    raw_url, http_status, response_bytes, content_type = download_baidu_web_native_panorama(panoid, file_path, capture_date=capture_date)
                    request_url = raw_url
                    ok = True
                    error = ""
                    break
                raw_url = baidu_web_image_url(panoid, heading, req.pitch, req.width, req.height, capture_date)
                request_url = raw_url
                response = requests.get(raw_url, headers=baidu_web_headers(), timeout=12)
                http_status = response.status_code
                content_type = response.headers.get("content-type", "")
                response_bytes = len(response.content)
                if response.ok and "image" in content_type:
                    file_path.write_bytes(response.content)
                    ok = True
                    error = ""
                    break
                ok = False
                error = f"HTTP {response.status_code}: {response.text[:80]}"
            except RuntimeError as exc:
                ok = False
                error = str(exc)
            except requests.RequestException as exc:
                ok = False
                error = str(exc)
            if attempt < max_attempts:
                time.sleep(0.5)
    else:
        ok = False
        error = "生产模式只允许百度官方 API 或授权 Web 街景来源"
    if ok and file_path.exists() and download_action == "downloaded":
        shutil.copy2(file_path, archive_path)

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "point_id": point.point_id,
        "file_name": file_name,
        "file_path": str(file_path),
        "heading": label,
        "image_type": image_type,
        "lng": point.lng,
        "lat": point.lat,
        "road_id": point.road_id,
        "road_name": point.road_name,
        "admin_name": point.admin_name,
        "provider": req.provider,
        "capture_date": capture_date,
        "capture_date_label": capture_date_label,
        "request_url": request_url,
        "panoid": panoid if req.provider == "baidu_web" else "",
        "panoid_url": panoid_url if req.provider == "baidu_web" else "",
        "native_analysis_note": "完整原生全景；语义分割阶段会投影为 front/right/back/left 四个水平 cube faces，并排除 top/bottom" if image_type == "panorama" else "",
        "requested_at": requested_at,
        "http_status": http_status,
        "content_type": content_type,
        "response_bytes": response_bytes,
        "duration_ms": duration_ms,
        "attempts": attempts,
        "download_action": download_action,
        "archive_key": archive_key,
        "status": "success" if ok else "failed",
        "error": error,
        "quality_status": "unchecked" if ok else "failed",
        "quality_note": "",
    }


def run_download_items(task_id: str, req: DownloadRequest, items: List[Dict[str, object]], completion_message: str) -> None:
    task = TASKS[task_id]
    task.status = "running"
    if not task.total:
        task.total = len(download_work_items(req))
    save_task_store()
    project_dir = DATA_DIR / task_id / "streetview_images"
    project_dir.mkdir(parents=True, exist_ok=True)

    index = 0
    while index < len(items):
        if not task_should_continue(task):
            return
        batch = items[index : index + max(1, req.concurrency)]
        with ThreadPoolExecutor(max_workers=max(1, req.concurrency)) as executor:
            future_map = {
                executor.submit(
                    download_one_image,
                    req,
                    item["point"],
                    int(item["heading"]),
                    str(item["image_type"]),
                    str(item["label"]),
                    project_dir,
                ): item
                for item in batch
            }
            for future in as_completed(future_map):
                if not task_should_continue(task):
                    return
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - task records should preserve worker failures.
                    item = future_map[future]
                    point = item["point"]
                    record = {
                        "point_id": point.point_id,
                        "file_name": "",
                        "file_path": "",
                        "heading": str(item["label"]),
                        "image_type": str(item["image_type"]),
                        "lng": point.lng,
                        "lat": point.lat,
                        "road_id": point.road_id,
                        "road_name": point.road_name,
                        "admin_name": point.admin_name,
                        "provider": req.provider,
                        "request_url": "",
                        "requested_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "http_status": 0,
                        "content_type": "",
                        "response_bytes": 0,
                        "duration_ms": 0,
                        "attempts": 0,
                        "download_action": "worker_failed",
                        "status": "failed",
                        "error": str(exc),
                        "quality_status": "failed",
                        "quality_note": "",
                    }
                item = future_map[future]
                record_key = download_item_key(record.get("point_id"), record.get("image_type"), record.get("heading"))
                expected_key = download_item_key(item["point"].point_id, item["image_type"], item["label"])
                if record_key != expected_key:
                    record_key = expected_key
                if record_key in existing_download_keys(task):
                    continue
                record["image_id"] = f"I{len(task.records)+1:06d}"
                task.records.append(record)
                recompute_download_counts(task)
                task.message = f"已处理 {task.succeeded + task.failed}/{task.total} 张图像"
                save_task_store()
        index += len(batch)
    done = task.succeeded + task.failed
    task.progress = min(100, round(done / max(task.total, 1) * 100))
    task.status = "completed" if done >= task.total else "paused"
    task.message = completion_message if task.status == "completed" else f"任务已暂停：已处理 {done}/{task.total} 张图像，可继续补采"
    save_task_store()


def run_download_task(task_id: str, req: DownloadRequest) -> None:
    task = TASKS[task_id]
    items = download_work_items(req)
    task.total = len(items)
    task.succeeded = 0
    task.failed = 0
    task.progress = 0
    task.records = []
    run_download_items(task_id, req, items, "街景图像任务完成")


def resume_missing_downloads(task_id: str, ak: Optional[str] = None) -> None:
    task = TASKS[task_id]
    if task.kind != "download":
        return
    try:
        req = download_request_from_task(task, ak)
        all_items = download_work_items(req)
    except RuntimeError as exc:
        task.message = str(exc)
        save_task_store()
        return
    task.total = len(all_items)
    done_keys = existing_download_keys(task)
    pending_items = [
        item
        for item in all_items
        if download_item_key(item["point"].point_id, item["image_type"], item["label"]) not in done_keys
    ]
    if not pending_items:
        done = task.succeeded + task.failed
        task.progress = min(100, round(done / max(task.total, 1) * 100))
        task.status = "completed" if done >= task.total else "paused"
        task.message = "没有剩余图片需要补采"
        save_task_store()
        return
    task.message = f"继续补采剩余图片：{len(pending_items)} 张"
    save_task_store()
    run_download_items(task_id, req, pending_items, "剩余街景图像补采完成")


def retry_failed_downloads(task_id: str, ak: Optional[str] = None) -> None:
    task = TASKS[task_id]
    if task.kind != "download":
        return
    if task.status == "running":
        task.message = "任务运行中，完成或暂停后再重试失败图片"
        save_task_store()
        return
    try:
        req_template = download_request_from_task(task, ak)
    except RuntimeError as exc:
        task.message = str(exc)
        save_task_store()
        return
    if req_template.provider == "baidu" and not req_template.ak:
        task.message = "真实百度任务重试需要重新填写 API Key"
        save_task_store()
        return
    point_lookup = {point.point_id: point for point in req_template.points}
    failed_records = [record for record in task.records if record.get("status") == "failed"]
    if not failed_records:
        task.message = "没有失败图片需要重试"
        save_task_store()
        return
    previous_status = task.status
    task.status = "running"
    task.message = f"正在重试失败图片：{len(failed_records)} 条"
    save_task_store()
    retried = 0
    recovered = 0
    for record in failed_records:
        if not task_should_continue(task):
            return
        task.status = "running"
        point = point_lookup.get(str(record.get("point_id")))
        if not point:
            record["error"] = "找不到原始采样点"
            continue
        heading_label = str(record.get("heading", 0) or 0)
        heading = 0 if heading_label == "pano" else int(float(heading_label))
        image_type = str(record.get("image_type") or ("panorama" if heading_label == "pano" else "direction"))
        req = req_template.model_copy(update={
            "points": [point],
            "headings": [heading],
            "image_mode": "panorama" if image_type == "panorama" else "directions",
            "skip_existing": False,
            "concurrency": 1,
        })
        result = download_one_image(req, point, heading, image_type, heading_label, DATA_DIR / task_id / "streetview_images")
        existing_image_id = record.get("image_id", "")
        record.update(result)
        record["image_id"] = existing_image_id
        if record.get("status") == "success":
            record["status"] = "success"
            record["error"] = ""
            record["quality_status"] = "unchecked"
            task.succeeded += 1
            task.failed = max(0, task.failed - 1)
            recovered += 1
        retried += 1
        done = task.succeeded + task.failed
        task.progress = min(100, round(done / max(task.total, 1) * 100))
        task.message = f"失败图片重试中：{retried}/{len(failed_records)}，恢复 {recovered} 条"
        save_task_store()
    done = task.succeeded + task.failed
    task.status = "completed" if done >= task.total else ("paused" if previous_status in {"paused", "completed", "failed"} else previous_status)
    task.message = f"失败图片重试完成：{retried} 条，恢复 {recovered} 条；已处理 {done}/{task.total}"
    save_task_store()


def run_metrics_task(task_id: str, req: MetricsRequest) -> None:
    task = TASKS[task_id]
    task.status = "running"
    task.message = "正在准备真实语义分割任务"
    task.progress = 0
    save_task_store()
    try:
        task.message = "正在检查云端模型服务"
        save_task_store()
        check_external_segmentation_service(req.segmentation_service_url)
        source_task = TASKS.get(req.source_download_task_id)
        if not source_task or source_task.kind != "download":
            raise RuntimeError("真实分割需要先完成街景图像下载任务")
        source_images = [
            record
            for record in source_task.records
            if record.get("status") == "success" and record.get("file_path") and Path(str(record.get("file_path"))).exists()
        ]
        if not source_images:
            raise RuntimeError("下载任务中没有可用于真实分割的图片文件")
        native_panoramas = [image for image in source_images if str(image.get("image_type") or "") == "panorama"]
        source_image_mode = str(((source_task.meta.get("image_params") or {}) if isinstance(source_task.meta.get("image_params"), dict) else {}).get("image_mode") or "")
        grouped: Dict[str, List[Dict[str, object]]] = {}
        for image in source_images:
            if str(image.get("image_type") or "direction") != "direction":
                continue
            grouped.setdefault(str(image.get("point_id") or ""), []).append(image)
        metric_images: List[Dict[str, object]]
        if native_panoramas:
            cube_dir = DATA_DIR / task_id / "cube_horizontal_faces"
            metric_images = []
            task.message = f"正在生成水平视角分析图 0/{len(native_panoramas)}"
            save_task_store()
            for index, image in enumerate(native_panoramas, start=1):
                point_id = str(image.get("point_id") or "")
                cube = native_panorama_to_horizontal_cube_strip(Path(str(image.get("file_path"))), point_id, cube_dir)
                metric_images.append(
                    {
                        **image,
                        **cube,
                        "native_full_file_path": str(image.get("file_path") or ""),
                        "native_analysis_note": "原生全景已投影为 front/right/back/left 四个水平 cube faces；top/bottom 已排除，避免采集车/nadir 干扰",
                    }
                )
                if index == 1 or index == len(native_panoramas) or index % 10 == 0:
                    task.message = f"正在生成水平视角分析图 {index}/{len(native_panoramas)}"
                    save_task_store()
            task.meta["segmentation_unit"] = "native_panorama_cube_horizontal_faces_excluding_top_bottom"
            task.meta["cube_faces"] = CUBE_FACE_NAMES
            task.meta["cube_excluded_faces"] = ["top", "bottom"]
            task.meta["projection_hfov"] = HORIZONTAL_FACE_HFOV
            task.meta["projection_vfov"] = HORIZONTAL_FACE_VFOV
            task.meta["projection_pitch"] = HORIZONTAL_FACE_PITCH
        elif source_image_mode == "stitched":
            preview_dir = DATA_DIR / task_id / "stitched_panoramas"
            point_ids = sorted(pid for pid in grouped if pid)
            metric_images = []
            task.message = f"正在拼接四方向分析图 0/{len(point_ids)}"
            save_task_store()
            for index, point_id in enumerate(point_ids, start=1):
                stitched = stitch_direction_images(point_id, grouped[point_id], preview_dir)
                metric_images.append(
                    {
                        **stitched,
                        "image_type": "stitched_panorama",
                        "heading_label": "stitched_0_90_180_270",
                        "analysis_note": "0/90/180/270 四方向图横向拼接后作为单张图提交模型计算",
                    }
                )
                if index == 1 or index == len(point_ids) or index % 10 == 0:
                    task.message = f"正在拼接四方向分析图 {index}/{len(point_ids)}"
                    save_task_store()
            if not metric_images:
                raise RuntimeError("没有找到可拼接的 0/90/180/270 四方向街景图")
            task.meta["segmentation_unit"] = "stitched_panorama_0_90_180_270"
            task.meta["panorama_stitch_order"] = PANORAMA_HEADINGS
        else:
            metric_images = []
            preview_dir = DATA_DIR / task_id / "direction_previews"
            point_ids = sorted(pid for pid in grouped if pid)
            task.message = f"正在准备四方向分割任务 0/{len(point_ids)}"
            save_task_store()
            for index, point_id in enumerate(point_ids, start=1):
                ordered = ordered_direction_images(point_id, grouped[point_id])
                preview = stitch_direction_images(point_id, ordered, preview_dir)
                metric_images.append(
                    {
                        **preview,
                        "image_type": "direction_set",
                        "direction_images": ordered,
                    }
                )
                if index == 1 or index == len(point_ids) or index % 10 == 0:
                    task.message = f"正在准备四方向分割任务 {index}/{len(point_ids)}"
                    save_task_store()
            if not metric_images:
                raise RuntimeError("没有找到原生全景图，也没有找到可拼接的 0/90/180/270 四方向街景图")
            task.meta["segmentation_unit"] = "direction_images_0_90_180_270_average"
            task.meta["direction_headings"] = PANORAMA_HEADINGS
            task.meta["direction_preview_note"] = "direction_previews 仅用于人工质检，不作为语义分割模型输入"
        task.total = len(metric_images)
        task.message = f"已准备 {task.total} 张分析图，正在提交云端模型分割"
        save_task_store()
        for image in metric_images:
            if not task_should_continue(task):
                return
            task.status = "running"
            if str(image.get("image_type") or "") == "direction_set":
                record = metric_record_from_direction_set(task, image, req.model_name, req.segmentation_service_url)
            else:
                record = metric_record_from_image(task, image, req.model_name, "external", req.segmentation_service_url)
            task.records.append(record)
            task.succeeded += 1
            task.progress = min(100, round(task.succeeded / max(task.total, 1) * 100))
            task.message = f"已计算 {task.succeeded}/{task.total} 个采样点指标"
            save_task_store()
    except RuntimeError as exc:
        task.status = "failed"
        task.failed = max(1, task.total - task.succeeded)
        task.message = str(exc)
        save_task_store()
        return
    task.status = "completed"
    task.message = "真实街景语义分割与指标任务完成"
    save_task_store()


def parse_image_filename(file_name: str, index: int) -> Dict[str, object]:
    stem = Path(file_name).stem
    parts = stem.replace("-", "_").split("_")
    point_id = next((part for part in parts if part.upper().startswith("P") and any(ch.isdigit() for ch in part)), f"UP{index:06d}")
    heading = 0
    for part in reversed(parts):
        try:
            value = int(float(part))
        except ValueError:
            continue
        if 0 <= value <= 360:
            heading = value
            break
    numbers: List[float] = []
    for part in parts:
        try:
            numbers.append(float(part))
        except ValueError:
            continue
    lng = next((value for value in numbers if 70 <= value <= 140), 0.0)
    lat = next((value for value in numbers if 0 <= value <= 55 and abs(value - heading) > 0.0001), 0.0)
    return {"point_id": point_id, "heading": heading, "lng": lng, "lat": lat}


def metric_record_from_image(task: TaskState, image: Dict[str, object], model_name: str, inference_mode: str = "external", service_url: str = "") -> Dict[str, object]:
    lng = float(image.get("lng", 0) or 0)
    lat = float(image.get("lat", 0) or 0)
    heading = int(image.get("heading", 0) or 0)
    heading_label = str(image.get("heading_label") or image.get("label") or heading)
    if inference_mode != "external":
        raise RuntimeError("生产模式只支持外部真实分割服务")
    file_path = Path(str(image.get("file_path") or ""))
    if not file_path.exists():
        raise RuntimeError(f"图片文件不存在：{file_path}")
    segmentation = call_external_segmentation_service(file_path.read_bytes(), str(image.get("file_name") or file_path.name), model_name, service_url)
    mask_file, overlay_file = write_segmentation_artifacts(task, str(image.get("point_id") or "UPLOADED_IMAGE"), heading_label, segmentation)
    metrics = {key: value for key, value in segmentation.items() if key in METRIC_KEYS}
    inference_source = "external_service"
    image_id = f"U{len(task.records)+1:06d}"
    point_id = str(image.get("point_id") or image_id)
    return {
        "image_id": image_id,
        "point_id": point_id,
        "heading": heading,
        "model_name": model_name,
        "inference_source": inference_source,
        "road_id": str(image.get("road_id") or "UPLOADED_IMAGE"),
        "road_name": str(image.get("road_name") or "上传图片"),
        "admin_name": str(image.get("admin_name") or "上传图片"),
        "file_name": str(image.get("file_name") or ""),
        "file_path": str(image.get("file_path") or ""),
        "image_type": str(image.get("image_type") or ""),
        "native_full_file_path": str(image.get("native_full_file_path") or ""),
        "native_analysis_note": str(image.get("native_analysis_note") or ""),
        "lng": lng,
        "lat": lat,
        "source_headings": str(image.get("source_headings") or heading),
        "cube_faces": str(image.get("cube_faces") or ""),
        "cube_excluded_faces": str(image.get("cube_excluded_faces") or ""),
        "projection_hfov": image.get("projection_hfov", ""),
        "projection_vfov": image.get("projection_vfov", ""),
        "projection_pitch": image.get("projection_pitch", ""),
        "mask_file": mask_file,
        "overlay_file": overlay_file,
        "segmentation_model_id": str(segmentation.get("model_id") or ""),
        "segmentation_device": str(segmentation.get("device") or ""),
        **metrics,
    }


def metric_record_from_direction_set(task: TaskState, image: Dict[str, object], model_name: str, service_url: str) -> Dict[str, object]:
    direction_images = image.get("direction_images")
    if not isinstance(direction_images, list) or not direction_images:
        raise RuntimeError(f"{image.get('point_id') or '采样点'} 缺少四方向图像")

    direction_records: List[Dict[str, object]] = []
    for direction_image in direction_images:
        if not isinstance(direction_image, dict):
            continue
        direction_records.append(metric_record_from_image(task, direction_image, model_name, "external", service_url))
    if not direction_records:
        raise RuntimeError(f"{image.get('point_id') or '采样点'} 没有可计算的方向图像")

    first = direction_records[0]
    averaged_metrics = {
        key: round(sum(float(record.get(key, 0) or 0) for record in direction_records) / len(direction_records), 4)
        for key in METRIC_KEYS
    }
    point_id = str(image.get("point_id") or first.get("point_id") or f"U{len(task.records)+1:06d}")
    return {
        **first,
        "image_id": f"U{len(task.records)+1:06d}",
        "point_id": point_id,
        "heading": 360,
        "heading_label": "direction_average",
        "image_type": "direction_set_average",
        "file_name": str(image.get("file_name") or ""),
        "file_path": str(image.get("file_path") or ""),
        "source_headings": ",".join(str(item) for item in PANORAMA_HEADINGS),
        "analysis_note": "0/90/180/270 四方向原图分别语义分割后，对指标取方向均值；预览拼图不参与模型推理",
        "direction_file_names": ";".join(str(record.get("file_name") or "") for record in direction_records),
        "direction_file_paths": ";".join(str(record.get("file_path") or "") for record in direction_records),
        "direction_mask_files": ";".join(str(record.get("mask_file") or "") for record in direction_records),
        "direction_overlay_files": ";".join(str(record.get("overlay_file") or "") for record in direction_records),
        "mask_file": "",
        "overlay_file": "",
        **averaged_metrics,
    }


def run_uploaded_image_metrics_task(task_id: str, model_name: str, inference_mode: str = "external", service_url: str = "") -> None:
    task = TASKS[task_id]
    images = task.meta.get("uploaded_images", [])
    if not isinstance(images, list):
        task.status = "failed"
        task.message = "上传图片元数据无效"
        return
    task.status = "running"
    task.total = len(images)
    save_task_store()
    for image in images:
        if not task_should_continue(task):
            return
        task.status = "running"
        if not isinstance(image, dict):
            continue
        try:
            task.records.append(metric_record_from_image(task, image, model_name, inference_mode, service_url))
        except RuntimeError as exc:
            task.status = "failed"
            task.failed = max(1, task.total - task.succeeded)
            task.message = str(exc)
            save_task_store()
            return
        time.sleep(0.001)
        task.succeeded += 1
        task.progress = min(100, round(task.succeeded / max(task.total, 1) * 100))
        task.message = f"已分割 {task.succeeded}/{task.total} 张上传图片"
        save_task_store()
    task.status = "completed"
    task.message = "上传图片分割与指标任务完成"
    save_task_store()


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sample", response_model=SampleResponse)
def sample(req: SampleRequest) -> SampleResponse:
    return build_sample_points(req)


@app.post("/api/import-boundary", response_model=Boundary)
async def import_boundary(file: UploadFile = File(...)) -> Boundary:
    raw = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".zip") or raw[:2] == b"PK":
        return boundary_from_coordinates(collect_shapefile_boundary_coordinates(raw))
    if filename.endswith(".kml") or b"<kml" in raw[:500].lower():
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gbk")
        return boundary_from_coordinates(collect_kml_coordinates(text))
    try:
        geojson = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"边界文件解析失败：{exc}") from exc
    return boundary_from_coordinates(collect_geojson_coordinates(geojson))


@app.post("/api/import-points", response_model=SampleResponse)
async def import_points(file: UploadFile = File(...), coord_type: Literal["wgs84", "bd09"] = "wgs84") -> SampleResponse:
    raw = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".zip") or raw[:2] == b"PK":
        points = collect_shapefile_sample_points(raw, coord_type)
        if not points:
            raise HTTPException(status_code=400, detail="没有识别到点状 SHP 要素")
        return response_from_points(points, [], "uploaded_points_shp")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV 为空")

    def pick(row: Dict[str, str], candidates: List[str]) -> Optional[str]:
        normalized = {key.strip().lower(): value for key, value in row.items() if key}
        for name in candidates:
            if name.lower() in normalized and normalized[name.lower()] != "":
                return normalized[name.lower()]
        return None

    points: List[SamplePoint] = []
    for idx, row in enumerate(rows, 1):
        lng_text = pick(row, ["lng", "lon", "longitude", "经度", "x"])
        lat_text = pick(row, ["lat", "latitude", "纬度", "y"])
        if lng_text is None or lat_text is None:
            continue
        try:
            lng = float(lng_text)
            lat = float(lat_text)
        except ValueError:
            continue
        point_id = pick(row, ["point_id", "id", "编号"]) or f"P{idx:06d}"
        road_name = pick(row, ["road_name", "name", "道路名", "road"]) or "用户上传点"
        if coord_type == "bd09":
            points.append(
                SamplePoint(
                    point_id=str(point_id),
                    lng=round(lng, 7),
                    lat=round(lat, 7),
                    coord_type="bd09",
                    lng_wgs84=round(lng, 7),
                    lat_wgs84=round(lat, 7),
                    lng_gcj02=round(lng, 7),
                    lat_gcj02=round(lat, 7),
                    lng_bd09=round(lng, 7),
                    lat_bd09=round(lat, 7),
                    road_id=pick(row, ["road_id", "道路编号"]) or "UPLOADED",
                    road_name=str(road_name),
                    admin_code=pick(row, ["admin_code", "行政区划代码"]) or "",
                    admin_name=pick(row, ["admin_name", "行政区", "区县"]) or "用户上传",
                    sample_interval=0,
                    source="uploaded_csv",
                )
            )
            continue
        points.append(
            build_sample_point(
                point_id=str(point_id),
                lng=lng,
                lat=lat,
                road_id=pick(row, ["road_id", "道路编号"]) or "UPLOADED",
                road_name=str(road_name),
                admin_code=pick(row, ["admin_code", "行政区划代码"]) or "",
                admin_name=pick(row, ["admin_name", "行政区", "区县"]) or "用户上传",
                sample_interval=0,
                source="uploaded_csv",
            )
        )
    if not points:
        raise HTTPException(status_code=400, detail="没有识别到 lng/lat 坐标列")
    return response_from_points(points, [], "uploaded_points")


@app.post("/api/export-sample-points-shp")
def export_sample_points_shp(points: List[SamplePoint]) -> StreamingResponse:
    point_dicts = [point.model_dump() for point in points]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        write_points_shapefile(zf, point_dicts)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="sample_points_shp.zip"'},
    )


@app.post("/api/import-roads", response_model=SampleResponse)
async def import_roads(file: UploadFile = File(...), interval_m: int = 100, clean_roads: bool = True) -> SampleResponse:
    if interval_m < 25 or interval_m > 500:
        raise HTTPException(status_code=400, detail="采样间隔需在 25m 到 500m 之间")
    raw = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".zip") or raw[:2] == b"PK":
        roads = collect_shapefile_line_features(raw)
        source = "uploaded_shp_roads"
    else:
        try:
            geojson = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"GeoJSON 解析失败：{exc}") from exc
        roads = collect_line_features(geojson)
        source = "uploaded_roads"
    if not roads:
        raise HTTPException(status_code=400, detail="没有识别到线状路网")
    return sample_roads_to_response(roads, interval_m, source, "上传路网", clean_roads=clean_roads)


@app.post("/api/osm-roads", response_model=SampleResponse)
def osm_roads(req: OsmRoadRequest) -> SampleResponse:
    b = req.boundary
    if b.north <= b.south or b.east <= b.west:
        raise HTTPException(status_code=400, detail="研究区边界无效")
    approx_area_km2 = (b.east - b.west) * 111.32 * max(math.cos(math.radians((b.north + b.south) / 2)), 0.2) * (b.north - b.south) * 111.32
    if approx_area_km2 > 80:
        raise HTTPException(status_code=400, detail="OSM 路网加载区域过大，请缩小到约 80 km² 内")
    query = f"""
    [out:json][timeout:25];
    (
      way["highway"]({b.south},{b.west},{b.north},{b.east});
    );
    out body;
    >;
    out skel qt;
    """
    overpass_error = ""
    try:
        payload = fetch_overpass_json(query)
    except HTTPException as exc:
        overpass_error = str(exc.detail)
        payload = fetch_osm_map_payload(b)
    roads = collect_osm_line_features(payload, keep_walkable=req.keep_walkable, exclude_high_speed=req.exclude_high_speed)
    roads = clip_roads_to_boundary(roads, b)
    if not roads:
        suffix = f"；Overpass 兜底信息：{overpass_error}" if overpass_error else ""
        raise HTTPException(status_code=404, detail=f"当前范围未识别到符合条件的 OSM 路网{suffix}")
    return sample_roads_to_response(roads, req.interval_m, "osm_overpass", "OSM 路网", clean_roads=req.clean_roads)


@app.post("/api/baidu-test")
def test_baidu_key(req: BaiduTestRequest) -> Dict[str, object]:
    if not req.ak.strip():
        raise HTTPException(status_code=400, detail="请填写百度地图 API Key")
    try:
        response = requests.get(baidu_test_url(req), timeout=10)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"请求百度 API 失败：{exc}") from exc
    content_type = response.headers.get("content-type", "")
    ok = response.ok and "image" in content_type
    return {
        "ok": ok,
        "status_code": response.status_code,
        "content_type": content_type,
        "bytes": len(response.content),
        "message": "API Key 可用，测试点返回图像" if ok else response.text[:200],
    }


@app.post("/api/download-task")
def create_download_task(req: DownloadRequest, request: Request) -> Dict[str, str]:
    if req.provider not in {"baidu", "baidu_web"}:
        raise HTTPException(status_code=400, detail="生产模式只支持百度官方 API 或授权 Web 街景来源")
    if (req.use_real_baidu or req.provider == "baidu") and not req.ak:
        raise HTTPException(status_code=400, detail="真实百度下载需要填写 API Key")
    if req.image_mode in {"directions", "stitched"} and not req.headings:
        raise HTTPException(status_code=400, detail="四方向图模式至少需要选择一个 heading")
    task_id = str(uuid.uuid4())
    TASKS[task_id] = TaskState(
        task_id=task_id,
        kind="download",
        meta={
            "owner": current_user_id(request),
            "project_name": req.project_name,
            "points": [point.model_dump() for point in req.points],
            "boundary": req.boundary.model_dump() if req.boundary else None,
            "roads": [road.model_dump() for road in req.roads],
            "headings": req.headings,
            "image_params": {
                "provider": req.provider,
                "width": req.width,
                "height": req.height,
                "pitch": req.pitch,
                "fov": req.fov,
                "coordtype": req.coordtype,
                "image_mode": req.image_mode,
                "capture_date": normalize_capture_date(req.capture_date),
                "skip_existing": req.skip_existing,
                "concurrency": req.concurrency,
                "retry_count": req.retry_count,
            },
        },
    )
    save_task_store()
    threading.Thread(target=run_download_task, args=(task_id, req), daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/metrics-task")
def create_metrics_task(req: MetricsRequest, request: Request) -> Dict[str, str]:
    if req.inference_mode != "external":
        raise HTTPException(status_code=400, detail="生产模式只支持外部真实分割服务")
    if not req.segmentation_service_url.strip():
        raise HTTPException(status_code=400, detail="真实分割需要填写模型服务地址")
    if not req.source_download_task_id.strip():
        raise HTTPException(status_code=400, detail="真实分割需要绑定已完成的街景下载任务")
    source_task = TASKS.get(req.source_download_task_id)
    if not source_task or source_task.kind != "download":
        raise HTTPException(status_code=400, detail="真实分割需要绑定有效的街景下载任务")
    if not user_can_access_task(source_task, request):
        raise HTTPException(status_code=403, detail="不能访问其他账号的街景下载任务")
    if source_task.status != "completed":
        raise HTTPException(status_code=400, detail="街景下载任务完成后才能创建真实分割任务")
    if not any(
        record.get("status") == "success" and record.get("file_path") and Path(str(record.get("file_path"))).exists()
        for record in source_task.records
    ):
        raise HTTPException(status_code=400, detail="下载任务中没有可用于真实分割的图片文件")
    task_id = str(uuid.uuid4())
    TASKS[task_id] = TaskState(
        task_id=task_id,
        kind="metrics",
        meta={
            "owner": current_user_id(request),
            "project_name": req.project_name,
            "points": [point.model_dump() for point in req.points],
            "boundary": req.boundary.model_dump() if req.boundary else None,
            "roads": [road.model_dump() for road in req.roads],
            "headings": req.headings,
            "source_download_task_id": req.source_download_task_id,
            "model_name": req.model_name,
            "selected_metrics": req.selected_metrics,
            "inference_mode": req.inference_mode,
            "segmentation_service_url": req.segmentation_service_url,
        },
    )
    save_task_store()
    threading.Thread(target=run_metrics_task, args=(task_id, req), daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/uploaded-image-metrics-task")
async def create_uploaded_image_metrics_task(
    request: Request,
    files: List[UploadFile] = File(...),
    project_name: str = Form("uploaded-image-analysis"),
    model_name: str = Form("Mask2Former + ADE20K"),
    selected_metrics: str = Form("gvi,sky_ratio,building_ratio,road_ratio,sidewalk_ratio"),
    inference_mode: str = Form("external"),
    segmentation_service_url: str = Form(""),
) -> Dict[str, str]:
    allowed_models = {
        "Mask2Former + ADE20K",
        "Mask2Former + Cityscapes",
        "FCN + ADE20K",
        "FCN + Cityscapes",
        "PSPNet + ADE20K",
        "PSPNet + Cityscapes",
        "DeepLabv3 + ADE20K",
        "DeepLabv3 + Cityscapes",
    }
    if model_name not in allowed_models:
        raise HTTPException(status_code=400, detail="不支持的模型")
    if inference_mode != "external":
        raise HTTPException(status_code=400, detail="生产模式只支持外部真实分割服务")
    if not segmentation_service_url.strip():
        raise HTTPException(status_code=400, detail="真实分割需要填写模型服务地址")
    allowed_ext = {".jpg", ".jpeg", ".png", ".webp"}
    task_id = str(uuid.uuid4())
    upload_dir = DATA_DIR / task_id / "uploaded_images"
    upload_dir.mkdir(parents=True, exist_ok=True)
    uploaded_images: List[Dict[str, object]] = []

    def save_image_bytes(file_name: str, raw: bytes) -> None:
        suffix = Path(file_name).suffix.lower()
        if suffix not in allowed_ext:
            return
        try:
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                image.verify()
        except Exception:
            return
        safe_name = f"{len(uploaded_images)+1:06d}_{Path(file_name).name.replace('/', '-')}"
        file_path = upload_dir / safe_name
        file_path.write_bytes(raw)
        parsed = parse_image_filename(file_name, len(uploaded_images) + 1)
        uploaded_images.append(
            {
                **parsed,
                "file_name": safe_name,
                "file_path": str(file_path),
                "width": width,
                "height": height,
            }
        )

    for upload in files:
        raw = await upload.read()
        filename = upload.filename or "uploaded"
        if filename.lower().endswith(".zip") or raw[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    for item in archive.infolist():
                        if item.is_dir():
                            continue
                        save_image_bytes(Path(item.filename).name, archive.read(item))
            except zipfile.BadZipFile as exc:
                raise HTTPException(status_code=400, detail="图片 ZIP 解析失败") from exc
        else:
            save_image_bytes(filename, raw)

    if not uploaded_images:
        raise HTTPException(status_code=400, detail="没有识别到可用图片，支持 jpg/jpeg/png/webp 或图片 ZIP")

    selected = [item.strip() for item in selected_metrics.split(",") if item.strip()]
    TASKS[task_id] = TaskState(
        task_id=task_id,
        kind="metrics",
        meta={
            "owner": current_user_id(request),
            "project_name": project_name,
            "points": [],
            "boundary": None,
            "roads": [],
            "headings": [],
            "model_name": model_name,
            "selected_metrics": selected,
            "inference_mode": inference_mode,
            "segmentation_service_url": segmentation_service_url,
            "uploaded_images": uploaded_images,
        },
    )
    save_task_store()
    threading.Thread(target=run_uploaded_image_metrics_task, args=(task_id, model_name, inference_mode, segmentation_service_url), daemon=True).start()
    return {"task_id": task_id, "image_count": str(len(uploaded_images))}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> Dict[str, object]:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user_can_access_task(task, request):
        raise HTTPException(status_code=403, detail="不能访问其他账号的任务")
    return {
        **task_summary(task),
        "task_id": task.task_id,
        "kind": task.kind,
        "status": task.status,
        "progress": task.progress,
        "total": task.total,
        "succeeded": task.succeeded,
        "failed": task.failed,
        "message": task.message,
        "records": task.records[:500],
    }


@app.get("/api/tasks")
def list_tasks(request: Request) -> Dict[str, object]:
    tasks = sorted([task for task in TASKS.values() if user_can_access_task(task, request)], key=lambda item: item.created_at, reverse=True)
    return {"tasks": [task_summary(task) for task in tasks[:50]]}


@app.get("/api/projects")
def list_projects(request: Request) -> Dict[str, object]:
    projects: Dict[str, Dict[str, object]] = {}
    for task in TASKS.values():
        if not user_can_access_task(task, request):
            continue
        name = str(task.meta.get("project_name") or "未命名项目")
        project = projects.setdefault(
            name,
            {
                "project_name": name,
                "task_count": 0,
                "download_count": 0,
                "metrics_count": 0,
                "completed_count": 0,
                "latest_status": task.status,
                "latest_task_id": task.task_id,
                "latest_created_at": task.created_at,
                "latest_export_url": "",
            },
        )
        project["task_count"] = int(project["task_count"]) + 1
        if task.kind == "download":
            project["download_count"] = int(project["download_count"]) + 1
        if task.kind == "metrics":
            project["metrics_count"] = int(project["metrics_count"]) + 1
        if task.status == "completed":
            project["completed_count"] = int(project["completed_count"]) + 1
        if str(task.created_at) >= str(project["latest_created_at"]):
            project["latest_status"] = task.status
            project["latest_task_id"] = task.task_id
            project["latest_created_at"] = task.created_at
            project["latest_export_url"] = f"/api/export/{task.task_id}" if task.status == "completed" else ""
    return {"projects": sorted(projects.values(), key=lambda item: str(item["latest_created_at"]), reverse=True)}


@app.post("/api/tasks/{task_id}/control")
def control_task(task_id: str, req: TaskControlRequest, request: Request) -> Dict[str, object]:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user_can_access_task(task, request):
        raise HTTPException(status_code=403, detail="不能控制其他账号的任务")
    if task.status in {"completed", "failed", "canceled"} and req.action != "resume":
        return {"ok": False, "message": "任务已结束，无法继续控制", "task": get_task(task_id, request)}
    if req.action == "pause":
        if task.status == "running":
            task.status = "paused"
            task.message = "任务已暂停"
            save_task_store()
        return {"ok": True, "task": get_task(task_id, request)}
    if req.action == "resume":
        if task.status == "running":
            return {"ok": True, "message": "任务正在运行，无需重复继续", "task": get_task(task_id, request)}
        if task.kind == "download":
            try:
                req_template = download_request_from_task(task)
                expected_total = len(download_work_items(req_template))
            except RuntimeError:
                expected_total = task.total
            done = task.succeeded + task.failed
            if done < expected_total:
                task.total = expected_total
                task.status = "running"
                task.message = f"继续补采剩余图片：已处理 {done}/{expected_total}"
                save_task_store()
                threading.Thread(target=resume_missing_downloads, args=(task_id, None), daemon=True).start()
                return {"ok": True, "task": get_task(task_id, request)}
        if task.status == "paused":
            task.status = "running"
            task.message = "任务继续运行"
            save_task_store()
        return {"ok": True, "task": get_task(task_id, request)}
    task.status = "canceled"
    task.message = "任务已取消"
    save_task_store()
    return {"ok": True, "task": get_task(task_id, request)}


@app.post("/api/tasks/{task_id}/quality")
def update_record_quality(task_id: str, req: QualityUpdateRequest, request: Request) -> Dict[str, object]:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user_can_access_task(task, request):
        raise HTTPException(status_code=403, detail="不能修改其他账号的任务")
    for record in task.records:
        if record.get("image_id") == req.image_id:
            record["quality_status"] = req.quality_status
            record["quality_note"] = req.note
            save_task_store()
            return {"ok": True, "record": record}
    raise HTTPException(status_code=404, detail="图片记录不存在")


@app.post("/api/tasks/{task_id}/retry-failed")
def retry_failed_task(task_id: str, req: RetryFailedRequest, request: Request) -> Dict[str, object]:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user_can_access_task(task, request):
        raise HTTPException(status_code=403, detail="不能重试其他账号的任务")
    if task.kind != "download":
        raise HTTPException(status_code=400, detail="只有街景下载任务支持失败重试")
    if task.status == "running":
        raise HTTPException(status_code=409, detail="任务运行中，完成或暂停后再重试失败图片")
    threading.Thread(target=retry_failed_downloads, args=(task_id, req.ak), daemon=True).start()
    save_task_store()
    return {"ok": True, "message": "失败重试任务已启动"}


def csv_bytes(rows: List[Dict[str, object]]) -> bytes:
    if not rows:
        return b""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def data_dictionary_bytes() -> bytes:
    rows = [
        {"field": "point_id", "meaning": "采样点唯一编号", "unit": "", "file": "sample_points / metrics"},
        {"field": "lng", "meaning": "WGS84 经度", "unit": "degree", "file": "sample_points / metrics"},
        {"field": "lat", "meaning": "WGS84 纬度", "unit": "degree", "file": "sample_points / metrics"},
        {"field": "road_id", "meaning": "采样点匹配的道路编号", "unit": "", "file": "sample_points / metrics"},
        {"field": "road_name", "meaning": "采样点匹配的道路名称", "unit": "", "file": "sample_points / metrics"},
        {"field": "admin_name", "meaning": "行政区名称", "unit": "", "file": "sample_points / metrics"},
        {"field": "heading", "meaning": "街景图像方向角；360 表示点位汇总或多方向集合", "unit": "degree", "file": "image_metrics"},
        {"field": "vegetation / gvi", "meaning": "绿视率，植被像素占有效像素比例", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "sky / sky_ratio", "meaning": "天空像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "water / water_ratio", "meaning": "水体像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "building / building_ratio", "meaning": "建筑像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "road / road_ratio", "meaning": "道路像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "sidewalk / sidewalk_ratio", "meaning": "人行道/铺装步行空间像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "vehicle / vehicle_ratio", "meaning": "车辆像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "person / person_ratio", "meaning": "行人像素占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "blue_view / bvi", "meaning": "蓝视率，天空与水体占比之和", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "natural / natural_ratio", "meaning": "自然要素综合占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "hardscape / hardscape_ratio", "meaning": "硬质铺装与硬质界面综合占比", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "enclosure / enclosure_ratio", "meaning": "界面围合度估计指标", "unit": "ratio", "file": "element_ratios / metrics"},
        {"field": "visual_entropy", "meaning": "视觉熵，语义类别分布复杂度", "unit": "normalized", "file": "metrics"},
        {"field": "color_richness / cvi", "meaning": "色彩丰富度指标", "unit": "normalized", "file": "metrics"},
    ]
    return csv_bytes(rows)


def safe_archive_name(value: object, fallback: str = "StreetScope") -> str:
    name = str(value or fallback).strip() or fallback
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or fallback


def archive_filename(task: TaskState) -> str:
    project_name = safe_archive_name(task.meta.get("project_name"), "StreetScope")
    date_text = datetime.now().strftime("%Y%m%d")
    return f"{project_name}_{date_text}.zip"


def points_csv_bytes(points: List[Dict[str, object]]) -> bytes:
    headers = [
        "project_id",
        "point_id",
        "lng",
        "lat",
        "coord_type",
        "lng_wgs84",
        "lat_wgs84",
        "lng_gcj02",
        "lat_gcj02",
        "lng_bd09",
        "lat_bd09",
        "road_id",
        "road_name",
        "admin_code",
        "admin_name",
        "sample_interval",
        "source",
        "created_at",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(points)
    return output.getvalue().encode("utf-8-sig")


def points_geojson_bytes(points: List[Dict[str, object]]) -> bytes:
    features = []
    for point in points:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point.get("lng"), point.get("lat")]},
                "properties": point,
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")


def metric_points_geojson_bytes(points: List[Dict[str, object]]) -> bytes:
    features = []
    for point in points:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point.get("lng"), point.get("lat")]},
                "properties": point,
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")


def boundary_geojson_bytes(boundary: Optional[Dict[str, object]]) -> bytes:
    if not boundary:
        return json.dumps({"type": "FeatureCollection", "features": []}, ensure_ascii=False, indent=2).encode("utf-8")
    west = float(boundary["west"])
    east = float(boundary["east"])
    south = float(boundary["south"])
    north = float(boundary["north"])
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
        },
        "properties": {"source": "task_boundary"},
    }
    return json.dumps({"type": "FeatureCollection", "features": [feature]}, ensure_ascii=False, indent=2).encode("utf-8")


def roads_geojson_bytes(roads: List[Dict[str, object]]) -> bytes:
    features = []
    for road in roads:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": road.get("coordinates", [])},
                "properties": {"road_id": road.get("road_id", ""), "road_name": road.get("road_name", "")},
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")


def write_points_shapefile(zf: zipfile.ZipFile, points: List[Dict[str, object]]) -> None:
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POINT)
    writer.autoBalance = 1
    writer.field("point_id", "C", size=40)
    writer.field("lng", "F", size=18, decimal=8)
    writer.field("lat", "F", size=18, decimal=8)
    writer.field("coord_type", "C", size=12)
    writer.field("lng_wgs84", "F", size=18, decimal=8)
    writer.field("lat_wgs84", "F", size=18, decimal=8)
    writer.field("lng_gcj02", "F", size=18, decimal=8)
    writer.field("lat_gcj02", "F", size=18, decimal=8)
    writer.field("lng_bd09", "F", size=18, decimal=8)
    writer.field("lat_bd09", "F", size=18, decimal=8)
    writer.field("road_id", "C", size=40)
    writer.field("road_name", "C", size=120)
    writer.field("admin_code", "C", size=20)
    writer.field("admin", "C", size=80)
    writer.field("interval", "N", size=8)
    writer.field("source", "C", size=40)
    for point in points:
        lng = float(point.get("lng", 0) or 0)
        lat = float(point.get("lat", 0) or 0)
        writer.point(lng, lat)
        writer.record(
            str(point.get("point_id", ""))[:40],
            lng,
            lat,
            str(point.get("coord_type", ""))[:12],
            float(point.get("lng_wgs84", point.get("lng", 0)) or 0),
            float(point.get("lat_wgs84", point.get("lat", 0)) or 0),
            float(point.get("lng_gcj02", 0) or 0),
            float(point.get("lat_gcj02", 0) or 0),
            float(point.get("lng_bd09", 0) or 0),
            float(point.get("lat_bd09", 0) or 0),
            str(point.get("road_id", ""))[:40],
            str(point.get("road_name", ""))[:120],
            str(point.get("admin_code", ""))[:20],
            str(point.get("admin_name", ""))[:80],
            int(point.get("sample_interval", 0) or 0),
            str(point.get("source", ""))[:40],
        )
    writer.close()
    zf.writestr("03_sample_points_采样点/shp/sample_points.shp", shp_io.getvalue())
    zf.writestr("03_sample_points_采样点/shp/sample_points.shx", shx_io.getvalue())
    zf.writestr("03_sample_points_采样点/shp/sample_points.dbf", dbf_io.getvalue())
    zf.writestr(
        "03_sample_points_采样点/shp/sample_points.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


def write_boundary_shapefile(zf: zipfile.ZipFile, boundary: Optional[Dict[str, object]]) -> None:
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POLYGON)
    writer.autoBalance = 1
    writer.field("source", "C", size=40)
    if boundary:
        west = float(boundary["west"])
        east = float(boundary["east"])
        south = float(boundary["south"])
        north = float(boundary["north"])
        ring = [[west, south], [west, north], [east, north], [east, south], [west, south]]
        writer.poly([ring])
        writer.record("task_boundary")
    writer.close()
    zf.writestr("01_boundary_研究区/shp/boundary.shp", shp_io.getvalue())
    zf.writestr("01_boundary_研究区/shp/boundary.shx", shx_io.getvalue())
    zf.writestr("01_boundary_研究区/shp/boundary.dbf", dbf_io.getvalue())
    zf.writestr(
        "01_boundary_研究区/shp/boundary.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


def write_metric_fields(writer: shapefile.Writer) -> None:
    for key in METRIC_KEYS:
        writer.field(SHP_METRIC_FIELDS[key], "F", size=14, decimal=6)


def metric_record_values(row: Dict[str, object]) -> List[float]:
    return [float(row.get(key, 0) or 0) for key in METRIC_KEYS]


def write_metric_points_shapefile(zf: zipfile.ZipFile, points: List[Dict[str, object]]) -> None:
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POINT)
    writer.autoBalance = 1
    writer.field("point_id", "C", size=40)
    writer.field("lng", "F", size=18, decimal=8)
    writer.field("lat", "F", size=18, decimal=8)
    writer.field("road_id", "C", size=40)
    writer.field("road_name", "C", size=120)
    writer.field("admin", "C", size=80)
    write_metric_fields(writer)
    for point in points:
        lng = float(point.get("lng", 0) or 0)
        lat = float(point.get("lat", 0) or 0)
        writer.point(lng, lat)
        writer.record(
            str(point.get("point_id", ""))[:40],
            lng,
            lat,
            str(point.get("road_id", ""))[:40],
            str(point.get("road_name", ""))[:120],
            str(point.get("admin_name", ""))[:80],
            *metric_record_values(point),
        )
    writer.close()
    zf.writestr("06_metrics_指标结果/shp/point_metrics.shp", shp_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/point_metrics.shx", shx_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/point_metrics.dbf", dbf_io.getvalue())
    zf.writestr(
        "06_metrics_指标结果/shp/point_metrics.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


def write_roads_shapefile(zf: zipfile.ZipFile, roads: List[Dict[str, object]]) -> None:
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POLYLINE)
    writer.autoBalance = 1
    writer.field("road_id", "C", size=40)
    writer.field("road_name", "C", size=120)
    count = 0
    for road in roads:
        coords = road.get("coordinates", [])
        if not isinstance(coords, list) or len(coords) < 2:
            continue
        line = [[float(item[0]), float(item[1])] for item in coords if isinstance(item, list) and len(item) >= 2]
        if len(line) < 2:
            continue
        writer.line([line])
        writer.record(str(road.get("road_id", ""))[:40], str(road.get("road_name", ""))[:120])
        count += 1
    writer.close()
    zf.writestr("02_road_network_路网/shp/roads.shp", shp_io.getvalue())
    zf.writestr("02_road_network_路网/shp/roads.shx", shx_io.getvalue())
    zf.writestr("02_road_network_路网/shp/roads.dbf", dbf_io.getvalue())
    zf.writestr(
        "02_road_network_路网/shp/roads.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


def road_metrics_geojson_bytes(road_metrics: List[Dict[str, object]], roads: List[Dict[str, object]]) -> bytes:
    road_lookup = {str(road.get("road_id")): road for road in roads}
    features = []
    for row in road_metrics:
        road = road_lookup.get(str(row.get("road_id")))
        if not road:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": road.get("coordinates", [])},
                "properties": row,
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")


def admin_metrics_geojson_bytes(admin_metrics: List[Dict[str, object]], boundary: Optional[Dict[str, object]]) -> bytes:
    if not boundary:
        return json.dumps({"type": "FeatureCollection", "features": []}, ensure_ascii=False, indent=2).encode("utf-8")
    west = float(boundary["west"])
    east = float(boundary["east"])
    south = float(boundary["south"])
    north = float(boundary["north"])
    geometry = {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}
    features = [{"type": "Feature", "geometry": geometry, "properties": row} for row in admin_metrics]
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")


def grid_metrics_geojson_bytes(grid_metrics: List[Dict[str, object]]) -> bytes:
    features = []
    for row in grid_metrics:
        try:
            west = float(row.get("west", 0) or 0)
            east = float(row.get("east", 0) or 0)
            south = float(row.get("south", 0) or 0)
            north = float(row.get("north", 0) or 0)
        except (TypeError, ValueError):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]},
                "properties": row,
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")


def write_road_metrics_shapefile(zf: zipfile.ZipFile, road_metrics: List[Dict[str, object]], roads: List[Dict[str, object]]) -> None:
    road_lookup = {str(road.get("road_id")): road for road in roads}
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POLYLINE)
    writer.autoBalance = 1
    writer.field("road_id", "C", size=40)
    writer.field("road_name", "C", size=120)
    writer.field("pt_count", "N", size=10)
    write_metric_fields(writer)
    for row in road_metrics:
        road = road_lookup.get(str(row.get("road_id")))
        if not road:
            continue
        coords = road.get("coordinates", [])
        line = [[float(item[0]), float(item[1])] for item in coords if isinstance(item, list) and len(item) >= 2]
        if len(line) < 2:
            continue
        writer.line([line])
        writer.record(
            str(row.get("road_id", ""))[:40],
            str(row.get("road_name", ""))[:120],
            int(row.get("point_count", 0) or 0),
            *metric_record_values(row),
        )
    writer.close()
    zf.writestr("06_metrics_指标结果/shp/road_metrics.shp", shp_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/road_metrics.shx", shx_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/road_metrics.dbf", dbf_io.getvalue())
    zf.writestr(
        "06_metrics_指标结果/shp/road_metrics.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


def write_admin_metrics_shapefile(zf: zipfile.ZipFile, admin_metrics: List[Dict[str, object]], boundary: Optional[Dict[str, object]]) -> None:
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POLYGON)
    writer.autoBalance = 1
    writer.field("admin", "C", size=80)
    writer.field("pt_count", "N", size=10)
    write_metric_fields(writer)
    if boundary:
        west = float(boundary["west"])
        east = float(boundary["east"])
        south = float(boundary["south"])
        north = float(boundary["north"])
        ring = [[west, south], [west, north], [east, north], [east, south], [west, south]]
        for row in admin_metrics:
            writer.poly([ring])
            writer.record(
                str(row.get("admin_name", ""))[:80],
                int(row.get("point_count", 0) or 0),
                *metric_record_values(row),
            )
    writer.close()
    zf.writestr("06_metrics_指标结果/shp/admin_metrics.shp", shp_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/admin_metrics.shx", shx_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/admin_metrics.dbf", dbf_io.getvalue())
    zf.writestr(
        "06_metrics_指标结果/shp/admin_metrics.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


def write_grid_metrics_shapefile(zf: zipfile.ZipFile, grid_metrics: List[Dict[str, object]]) -> None:
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()
    writer = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io, shapeType=shapefile.POLYGON)
    writer.autoBalance = 1
    writer.field("grid_id", "C", size=40)
    writer.field("pt_count", "N", size=10)
    write_metric_fields(writer)
    for row in grid_metrics:
        try:
            west = float(row.get("west", 0) or 0)
            east = float(row.get("east", 0) or 0)
            south = float(row.get("south", 0) or 0)
            north = float(row.get("north", 0) or 0)
        except (TypeError, ValueError):
            continue
        ring = [[west, south], [west, north], [east, north], [east, south], [west, south]]
        writer.poly([ring])
        writer.record(str(row.get("grid_id", ""))[:40], int(row.get("point_count", 0) or 0), *metric_record_values(row))
    writer.close()
    zf.writestr("06_metrics_指标结果/shp/grid_metrics.shp", shp_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/grid_metrics.shx", shx_io.getvalue())
    zf.writestr("06_metrics_指标结果/shp/grid_metrics.dbf", dbf_io.getvalue())
    zf.writestr(
        "06_metrics_指标结果/shp/grid_metrics.prj",
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
    )


SEGMENT_CLASSES = [
    ("vegetation", "gvi", (42, 157, 78)),
    ("sky", "sky_ratio", (111, 177, 252)),
    ("water", "water_ratio", (37, 99, 235)),
    ("building", "building_ratio", (142, 142, 147)),
    ("road", "road_ratio", (78, 83, 92)),
    ("sidewalk", "sidewalk_ratio", (210, 185, 145)),
    ("vehicle", "vehicle_ratio", (239, 68, 68)),
    ("person", "person_ratio", (245, 158, 11)),
    ("other", "other_ratio", (238, 238, 238)),
]


def class_ratio_rows(records: List[Dict[str, object]], width: int = 1024, height: int = 512) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    valid_pixels = width * height
    for record in records:
        known = sum(float(record.get(key, 0) or 0) for _, key, _ in SEGMENT_CLASSES if key != "other_ratio")
        ratios = {key: float(record.get(key, 0) or 0) for _, key, _ in SEGMENT_CLASSES if key != "other_ratio"}
        ratios["other_ratio"] = max(0.0, 1.0 - known)
        total = sum(ratios.values()) or 1.0
        for class_name, key, _ in SEGMENT_CLASSES:
            ratio = ratios[key] / total
            rows.append(
                {
                    "image_id": record.get("image_id", ""),
                    "point_id": record.get("point_id", ""),
                    "heading": record.get("heading", ""),
                    "model_name": record.get("model_name", ""),
                    "class_name": class_name,
                    "pixel_count": round(valid_pixels * ratio),
                    "pixel_ratio": round(ratio, 6),
                    "valid_pixel_count": valid_pixels,
                }
            )
    return rows


def element_ratio_row(record: Dict[str, object]) -> Dict[str, object]:
    row = {
        "image_id": record.get("image_id", ""),
        "point_id": record.get("point_id", ""),
        "heading": record.get("heading", ""),
        "heading_label": record.get("heading_label", ""),
        "image_type": record.get("image_type", ""),
        "model_name": record.get("model_name", ""),
        "file_name": record.get("file_name", ""),
    }
    for class_name, key, _ in SEGMENT_CLASSES:
        if key == "other_ratio":
            known = sum(float(record.get(metric_key, 0) or 0) for _, metric_key, _ in SEGMENT_CLASSES if metric_key != "other_ratio")
            row[class_name] = round(max(0.0, 1.0 - known), 6)
        else:
            row[class_name] = round(float(record.get(key, 0) or 0), 6)
    row["blue_view"] = round(float(record.get("bvi", 0) or 0), 6)
    row["natural"] = round(float(record.get("natural_ratio", 0) or 0), 6)
    row["hardscape"] = round(float(record.get("hardscape_ratio", 0) or 0), 6)
    row["enclosure"] = round(float(record.get("enclosure_ratio", 0) or 0), 6)
    row["visual_entropy"] = round(float(record.get("visual_entropy", 0) or 0), 6)
    row["color_richness"] = round(float(record.get("cvi", 0) or 0), 6)
    return row


def element_ratio_rows(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    return [element_ratio_row(record) for record in records]


def segmentation_png(record: Dict[str, object], overlay: bool = False, width: int = 320, height: int = 180) -> bytes:
    known = sum(float(record.get(key, 0) or 0) for _, key, _ in SEGMENT_CLASSES if key != "other_ratio")
    ratios = {key: float(record.get(key, 0) or 0) for _, key, _ in SEGMENT_CLASSES if key != "other_ratio"}
    ratios["other_ratio"] = max(0.0, 1.0 - known)
    total = sum(ratios.values()) or 1.0
    image = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    x = 0
    for class_name, key, color in SEGMENT_CLASSES:
        ratio = ratios[key] / total
        segment_width = width - x if class_name == "other" else max(1, round(width * ratio))
        draw.rectangle([x, 0, min(width, x + segment_width), height], fill=color)
        x += segment_width
        if x >= width:
            break
    if overlay:
        base = Image.new("RGB", (width, height), (235, 238, 241))
        base_draw = ImageDraw.Draw(base)
        for y in range(0, height, 18):
            base_draw.line([(0, y), (width, y)], fill=(210, 216, 224))
        for xline in range(0, width, 32):
            base_draw.line([(xline, 0), (xline, height)], fill=(210, 216, 224))
        image = Image.blend(base, image, 0.58)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


@app.get("/api/export/{task_id}")
def export_task(task_id: str, request: Request) -> StreamingResponse:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not user_can_access_task(task, request):
        raise HTTPException(status_code=403, detail="不能导出其他账号的数据包")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        points = task.meta.get("points", [])
        point_list = points if isinstance(points, list) else []
        boundary = task.meta.get("boundary")
        boundary_dict = boundary if isinstance(boundary, dict) else None
        roads = task.meta.get("roads", [])
        road_list = roads if isinstance(roads, list) else []
        zf.writestr("README_数据说明.md", f"# StreetScope 论文数据包\n\n任务类型：{task.kind}\n状态：{task.status}\n记录数：{len(task.records)}\n")
        zf.writestr("metadata/project_config.json", json.dumps(task.meta, ensure_ascii=False, indent=2).encode("utf-8"))
        zf.writestr("metadata/data_dictionary.csv", data_dictionary_bytes())
        zf.writestr("01_boundary_研究区/boundary.geojson", boundary_geojson_bytes(boundary_dict))
        write_boundary_shapefile(zf, boundary_dict)
        zf.writestr("02_road_network_路网/roads.geojson", roads_geojson_bytes(road_list))
        write_roads_shapefile(zf, road_list)
        zf.writestr("03_sample_points_采样点/sample_points.csv", points_csv_bytes(point_list))
        zf.writestr("03_sample_points_采样点/sample_points.geojson", points_geojson_bytes(point_list))
        write_points_shapefile(zf, point_list)
        if task.kind == "download":
            zf.writestr("04_streetview_images_街景图像/image_manifest.csv", csv_bytes(task.records))
            failed_downloads = [r for r in task.records if r.get("status") == "failed"]
            if failed_downloads:
                zf.writestr("99_failed_or_excluded_失败与剔除/failed_downloads.csv", csv_bytes(failed_downloads))
            for record in task.records:
                if record.get("status") != "success":
                    continue
                file_path = Path(str(record.get("file_path", "")))
                if file_path.exists() and file_path.is_file():
                    folder = "native_panoramas" if str(record.get("image_type") or "") == "panorama" else "direction_images"
                    zf.write(file_path, f"04_streetview_images_街景图像/{folder}/{file_path.name}")
                full_path = Path(str(record.get("native_full_file_path", "")))
                if full_path.exists() and full_path.is_file():
                    zf.write(full_path, f"04_streetview_images_街景图像/native_panoramas/{full_path.name}")
        else:
            zf.writestr("04_streetview_images_街景图像/image_manifest.csv", csv_bytes(task.records))
            zf.writestr("05_segmentation_语义分割/segmentation_manifest.csv", csv_bytes(task.records))
            zf.writestr("06_metrics_指标结果/image_metrics.csv", csv_bytes(task.records))
            zf.writestr("06_metrics_指标结果/segmentation_class_ratio.csv", csv_bytes(class_ratio_rows(task.records)))
            zf.writestr("06_metrics_指标结果/element_ratios_by_image.csv", csv_bytes(element_ratio_rows(task.records)))
            for image in task.meta.get("uploaded_images", []):
                if not isinstance(image, dict):
                    continue
                file_path = Path(str(image.get("file_path", "")))
                if file_path.exists() and file_path.is_file():
                    zf.write(file_path, f"04_streetview_images_街景图像/uploaded_images/{file_path.name}")
            for record in task.records:
                file_path = Path(str(record.get("file_path", "")))
                if file_path.exists() and file_path.is_file():
                    image_type = str(record.get("image_type") or "")
                    if image_type == "cube_horizontal_faces":
                        folder = "horizontal_faces"
                    elif image_type == "stitched_panorama":
                        folder = "stitched_panoramas"
                    elif image_type in {"direction_preview", "direction_set_average"}:
                        folder = "quality_previews"
                    elif image_type == "panorama":
                        folder = "native_panoramas"
                    else:
                        folder = "direction_images"
                    zf.write(file_path, f"04_streetview_images_街景图像/{folder}/{file_path.name}")
                full_path = Path(str(record.get("native_full_file_path", "")))
                if full_path.exists() and full_path.is_file():
                    zf.write(full_path, f"04_streetview_images_街景图像/native_panoramas/{full_path.name}")
                for direction_path_text in str(record.get("direction_file_paths") or "").split(";"):
                    if not direction_path_text:
                        continue
                    direction_path = Path(direction_path_text)
                    if direction_path.exists() and direction_path.is_file():
                        zf.write(direction_path, f"04_streetview_images_街景图像/direction_images/{direction_path.name}")
                mask_file = str(record.get("mask_file") or f"{record.get('image_id')}_mask.png")
                overlay_file = str(record.get("overlay_file") or f"{record.get('image_id')}_overlay.png")
                mask_path = DATA_DIR / task.task_id / "segmentation_masks" / "masks" / mask_file
                overlay_path = DATA_DIR / task.task_id / "segmentation_masks" / "overlays" / overlay_file
                if mask_path.exists():
                    zf.write(mask_path, f"05_segmentation_语义分割/masks/{mask_file}")
                if overlay_path.exists():
                    zf.write(overlay_path, f"05_segmentation_语义分割/overlays/{overlay_file}")
                for extra_mask in str(record.get("direction_mask_files") or "").split(";"):
                    if not extra_mask:
                        continue
                    extra_mask_path = DATA_DIR / task.task_id / "segmentation_masks" / "masks" / extra_mask
                    if extra_mask_path.exists():
                        zf.write(extra_mask_path, f"05_segmentation_语义分割/masks/{extra_mask}")
                for extra_overlay in str(record.get("direction_overlay_files") or "").split(";"):
                    if not extra_overlay:
                        continue
                    extra_overlay_path = DATA_DIR / task.task_id / "segmentation_masks" / "overlays" / extra_overlay
                    if extra_overlay_path.exists():
                        zf.write(extra_overlay_path, f"05_segmentation_语义分割/overlays/{extra_overlay}")
            point_lookup = {str(point.get("point_id")): point for point in point_list if isinstance(point, dict)}
            point_rows: Dict[str, Dict[str, float]] = {}
            metric_keys = METRIC_KEYS
            for row in task.records:
                pid = str(row["point_id"])
                bucket = point_rows.setdefault(
                    pid,
                    {
                        "count": 0,
                        "lng": row.get("lng", ""),
                        "lat": row.get("lat", ""),
                        "road_id": row.get("road_id", ""),
                        "road_name": row.get("road_name", ""),
                        "admin_name": row.get("admin_name", ""),
                        **{key: 0 for key in metric_keys},
                    },
                )
                bucket["count"] += 1
                for key in metric_keys:
                    bucket[key] += float(row.get(key, 0) or 0)
            point_metrics = []
            for pid, values in point_rows.items():
                count = max(values.pop("count"), 1)
                source_point = point_lookup.get(pid, {})
                point_metrics.append(
                    {
                        "point_id": pid,
                        "lng": source_point.get("lng", values.get("lng", "")),
                        "lat": source_point.get("lat", values.get("lat", "")),
                        "road_id": source_point.get("road_id", values.get("road_id", "")),
                        "road_name": source_point.get("road_name", values.get("road_name", "")),
                        "admin_name": source_point.get("admin_name", values.get("admin_name", "")),
                        **{key: round(float(values.get(key, 0) or 0) / count, 4) for key in metric_keys},
                    }
                )
            zf.writestr("06_metrics_指标结果/point_metrics.csv", csv_bytes(point_metrics))
            zf.writestr("06_metrics_指标结果/element_ratios_by_point.csv", csv_bytes(element_ratio_rows(point_metrics)))
            zf.writestr("06_metrics_指标结果/point_metrics.geojson", metric_points_geojson_bytes(point_metrics))
            write_metric_points_shapefile(zf, point_metrics)

            def aggregate(rows: List[Dict[str, object]], group_keys: List[str]) -> List[Dict[str, object]]:
                buckets: Dict[str, Dict[str, object]] = {}
                for row in rows:
                    group_id = "|".join(str(row.get(key, "")) for key in group_keys)
                    bucket = buckets.setdefault(group_id, {key: row.get(key, "") for key in group_keys} | {"count": 0, **{key: 0.0 for key in metric_keys}})
                    bucket["count"] = int(bucket["count"]) + 1
                    for key in metric_keys:
                        bucket[key] = float(bucket[key]) + float(row.get(key, 0) or 0)
                output = []
                for bucket in buckets.values():
                    count = max(int(bucket.pop("count")), 1)
                    output.append({**bucket, **{key: round(float(bucket[key]) / count, 4) for key in metric_keys}, "point_count": count})
                return output

            road_metrics = aggregate(point_metrics, ["road_id", "road_name"])
            admin_metrics = aggregate(point_metrics, ["admin_name"])
            grid_size = 0.0045
            grid_rows: List[Dict[str, object]] = []
            grid_buckets: Dict[str, Dict[str, object]] = {}
            for row in point_metrics:
                try:
                    lng = float(row.get("lng", 0) or 0)
                    lat = float(row.get("lat", 0) or 0)
                except (TypeError, ValueError):
                    continue
                grid_x = math.floor(lng / grid_size)
                grid_y = math.floor(lat / grid_size)
                grid_id = f"g_{grid_x}_{grid_y}"
                bucket = grid_buckets.setdefault(
                    grid_id,
                    {
                        "grid_id": grid_id,
                        "west": round(grid_x * grid_size, 7),
                        "east": round((grid_x + 1) * grid_size, 7),
                        "south": round(grid_y * grid_size, 7),
                        "north": round((grid_y + 1) * grid_size, 7),
                        "count": 0,
                        **{key: 0.0 for key in metric_keys},
                    },
                )
                bucket["count"] = int(bucket["count"]) + 1
                for key in metric_keys:
                    bucket[key] = float(bucket[key]) + float(row.get(key, 0) or 0)
            for bucket in grid_buckets.values():
                count = max(int(bucket.pop("count")), 1)
                grid_rows.append({**bucket, **{key: round(float(bucket[key]) / count, 4) for key in metric_keys}, "point_count": count})
            zf.writestr("06_metrics_指标结果/road_metrics.csv", csv_bytes(road_metrics))
            zf.writestr("06_metrics_指标结果/road_metrics.geojson", road_metrics_geojson_bytes(road_metrics, road_list))
            write_road_metrics_shapefile(zf, road_metrics, road_list)
            zf.writestr("06_metrics_指标结果/admin_metrics.csv", csv_bytes(admin_metrics))
            zf.writestr("06_metrics_指标结果/admin_metrics.geojson", admin_metrics_geojson_bytes(admin_metrics, boundary_dict))
            write_admin_metrics_shapefile(zf, admin_metrics, boundary_dict)
            zf.writestr("06_metrics_指标结果/grid_metrics.csv", csv_bytes(grid_rows))
            zf.writestr("06_metrics_指标结果/grid_metrics.geojson", grid_metrics_geojson_bytes(grid_rows))
            write_grid_metrics_shapefile(zf, grid_rows)
            failed_segmentations = [r for r in task.records if r.get("status") == "failed"]
            excluded_images = [r for r in task.records if r.get("quality_status") == "excluded"]
            if failed_segmentations:
                zf.writestr("99_failed_or_excluded_失败与剔除/failed_segmentations.csv", csv_bytes(failed_segmentations))
            if excluded_images:
                zf.writestr("99_failed_or_excluded_失败与剔除/excluded_images.csv", csv_bytes(excluded_images))
        zf.writestr(
            "metadata/method_description.md",
            "\n".join(
                [
                    "# 方法说明",
                    "",
                    "本数据包由 StreetScope Research MVP 生成。",
                    "",
                    f"- 任务类型：{task.kind}",
                    f"- 项目名称：{task.meta.get('project_name', '')}",
                    f"- 采样点数量：{len(point_list)}",
                    f"- 方向：{task.meta.get('headings', [])}",
                    f"- 图像参数：{task.meta.get('image_params', {})}",
                    f"- 模型：{task.meta.get('model_name', '未运行语义分割')}",
                    f"- 推理模式：{task.meta.get('inference_mode', 'external')}",
                    f"- 外部分割服务：{task.meta.get('segmentation_service_url', '')}",
                    f"- 本次选择指标：{task.meta.get('selected_metrics', '全部默认指标')}",
                    "",
                    "主要指标口径：",
                    "",
                    "- GVI = 植被像素 / 有效像素",
                    "- BVI/蓝视率 = (天空像素 + 水体像素) / 有效像素",
                    "- 天空开阔度 = 天空像素 / 有效像素",
                    "- 建筑占比 = 建筑像素 / 有效像素",
                    "- 道路占比 = 道路像素 / 有效像素",
                    "- 车行空间占比 = 道路像素 + 车辆像素",
                    "- 硬质铺装占比 = 道路 + 人行道 + 地面铺装 + 墙面等硬质界面",
                    "- 人车密度 = 行人 + 车辆 + 骑行者等动态交通要素占比",
                    "- 色彩丰富度 CVI = RGB 方差与 HSV 饱和度综合指标",
                    "- 视觉熵 = -Σ p_i * log(p_i)",
                    "",
                    "聚合层级：image_metrics 为方向图像级；point_metrics 为同一点多方向均值；road/admin/grid_metrics 分别按道路、行政区与约 500m 网格聚合。",
                    "",
                    "生产模式下，指标来自外部分割服务返回的类别像素占比或标准化指标。",
                    "",
                ]
            ),
        )
    buffer.seek(0)
    filename = archive_filename(task)
    quoted_filename = quote(filename)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}"},
    )


FRONTEND_DIST = Path(os.getenv("STREETSCOPE_FRONTEND_DIST", str(ROOT.parent / "frontend" / "dist"))).expanduser().resolve()
if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


    @app.get("/favicon.svg", include_in_schema=False)
    def frontend_favicon() -> FileResponse:
        favicon = FRONTEND_DIST / "favicon.svg"
        if not favicon.exists():
            raise HTTPException(status_code=404, detail="favicon not found")
        return FileResponse(favicon)


    @app.get("/icons.svg", include_in_schema=False)
    def frontend_icons() -> FileResponse:
        icons = FRONTEND_DIST / "icons.svg"
        if not icons.exists():
            raise HTTPException(status_code=404, detail="icons not found")
        return FileResponse(icons)


    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_app(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        return FileResponse(FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")), reload=True)
