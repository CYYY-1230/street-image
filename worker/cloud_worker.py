from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
LOCAL_API_BASE = os.getenv("STREETSCOPE_LOCAL_API_BASE", "http://127.0.0.1:8000").rstrip("/")
WORKER_ID = os.getenv("STREETSCOPE_WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}")
POLL_SECONDS = float(os.getenv("STREETSCOPE_WORKER_POLL_SECONDS", "5"))
ARTIFACT_BUCKET = os.getenv("STREETSCOPE_ARTIFACT_BUCKET", "streetscope-artifacts")
LOCAL_BASIC_AUTH = os.getenv("STREETSCOPE_LOCAL_BASIC_AUTH", "")
ARTIFACT_MAX_BYTES = int(os.getenv("STREETSCOPE_ARTIFACT_MAX_BYTES", str(45 * 1024 * 1024)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_env() -> None:
    missing = []
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")


def supabase_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def local_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if LOCAL_BASIC_AUTH:
        headers["Authorization"] = f"Basic {LOCAL_BASIC_AUTH}"
    return headers


def sb_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method, f"{SUPABASE_URL}{path}", headers=supabase_headers(kwargs.pop("extra_headers", None)), timeout=60, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {path} failed: {response.status_code} {response.text[:500]}")
    return response


def update_cloud_task(task_id: str, patch: dict[str, Any]) -> None:
    patch.setdefault("updated_at", utc_now())
    sb_request(
        "PATCH",
        f"/rest/v1/streetscope_tasks?id=eq.{task_id}",
        data=json.dumps(patch, ensure_ascii=False),
        extra_headers={"Prefer": "return=minimal"},
    )


def fetch_next_task() -> dict[str, Any] | None:
    response = sb_request(
        "GET",
        "/rest/v1/streetscope_tasks?status=eq.queued&order=created_at.asc&limit=1",
    )
    tasks = response.json()
    if not tasks:
        return None
    task = tasks[0]
    task_id = task["id"]
    claim_response = sb_request(
        "PATCH",
        f"/rest/v1/streetscope_tasks?id=eq.{task_id}&status=eq.queued",
        data=json.dumps({"status": "running", "worker_id": WORKER_ID, "started_at": utc_now(), "message": "Windows Worker 已领取任务"}, ensure_ascii=False),
        extra_headers={"Prefer": "return=representation"},
    )
    claimed = claim_response.json()
    return claimed[0] if claimed else None


def local_api(method: str, path: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method, f"{LOCAL_API_BASE}{path}", headers=local_headers(), timeout=120, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"Local API {method} {path} failed: {response.status_code} {response.text[:800]}")
    return response


def create_local_task(path: str, payload: dict[str, Any]) -> str:
    response = local_api("POST", path, json=payload)
    task_id = response.json().get("task_id")
    if not task_id:
        raise RuntimeError(f"Local API {path} did not return task_id")
    return str(task_id)


def wait_local_task(local_task_id: str, cloud_task_id: str, local_kind_key: str) -> dict[str, Any]:
    last_progress = -1
    while True:
        task = local_api("GET", f"/api/tasks/{local_task_id}").json()
        progress = int(task.get("progress") or 0)
        status = str(task.get("status") or "")
        if progress != last_progress or status in {"completed", "failed", "canceled"}:
            last_progress = progress
            update_cloud_task(
                cloud_task_id,
                {
                    local_kind_key: local_task_id,
                    "progress": progress,
                    "total": int(task.get("total") or 0),
                    "succeeded": int(task.get("succeeded") or 0),
                    "failed": int(task.get("failed") or 0),
                    "message": str(task.get("message") or ""),
                    "records_preview": list(task.get("records") or [])[:50],
                },
            )
        if status == "completed":
            return task
        if status in {"failed", "canceled"}:
            raise RuntimeError(f"Local task {local_task_id} ended with status={status}: {task.get('message')}")
        time.sleep(2)


def download_export(local_task_id: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    response = local_api("GET", f"/api/export/{local_task_id}", stream=True)
    disposition = response.headers.get("content-disposition", "")
    filename = f"StreetScope_{local_task_id}.zip"
    if "filename=" in disposition:
        filename = disposition.split("filename=", 1)[-1].strip().strip('"')
    output_path = output_dir / filename
    with output_path.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)
    return output_path


def upload_single_artifact(user_id: str, cloud_task_id: str, file_path: Path) -> str:
    object_path = f"{user_id}/{cloud_task_id}/{file_path.name}"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{ARTIFACT_BUCKET}/{object_path}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/zip",
        "x-upsert": "true",
    }
    with file_path.open("rb") as fh:
        response = requests.post(upload_url, headers=headers, data=fh, timeout=600)
    if response.status_code >= 400:
        raise RuntimeError(f"Upload artifact failed: {response.status_code} {response.text[:800]}")
    return object_path


def split_zip_archive(file_path: Path, max_bytes: int) -> list[Path]:
    import zipfile

    if file_path.stat().st_size <= max_bytes:
        return [file_path]

    parts_dir = file_path.with_suffix("")
    parts_dir.mkdir(parents=True, exist_ok=True)
    groups: list[list[zipfile.ZipInfo]] = []
    current: list[zipfile.ZipInfo] = []
    current_size = 0
    with zipfile.ZipFile(file_path, "r") as source:
        for info in source.infolist():
            if info.is_dir():
                continue
            estimated_size = max(info.compress_size, info.file_size, 1) + 1024
            if current and current_size + estimated_size > max_bytes:
                groups.append(current)
                current = []
                current_size = 0
            current.append(info)
            current_size += estimated_size
        if current:
            groups.append(current)

        part_paths: list[Path] = []
        total_parts = len(groups)
        for index, group in enumerate(groups, start=1):
            part_path = parts_dir / f"{file_path.stem}.part{index:02d}-of-{total_parts:02d}.zip"
            with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED) as target:
                for info in group:
                    target.writestr(info, source.read(info.filename))
            part_paths.append(part_path)
    return part_paths


def upload_artifacts(user_id: str, cloud_task_id: str, file_path: Path) -> list[str]:
    artifact_paths = split_zip_archive(file_path, ARTIFACT_MAX_BYTES)
    return [upload_single_artifact(user_id, cloud_task_id, path) for path in artifact_paths]


def handle_task(task: dict[str, Any]) -> None:
    task_id = str(task["id"])
    user_id = str(task["user_id"])
    kind = str(task["kind"])
    payload = task.get("payload") or {}
    if not isinstance(payload, dict):
        raise RuntimeError("Task payload must be a JSON object")

    output_dir = Path(os.getenv("STREETSCOPE_WORKER_OUTPUT_DIR", "worker_artifacts")) / task_id

    if kind == "download":
        download_payload = payload.get("download_request") or payload
        local_download_id = create_local_task("/api/download-task", download_payload)
        wait_local_task(local_download_id, task_id, "local_download_task_id")
        artifact = download_export(local_download_id, output_dir)
    elif kind == "metrics":
        metrics_payload = payload.get("metrics_request") or payload
        local_metrics_id = create_local_task("/api/metrics-task", metrics_payload)
        wait_local_task(local_metrics_id, task_id, "local_metrics_task_id")
        artifact = download_export(local_metrics_id, output_dir)
    elif kind == "download_then_metrics":
        download_payload = payload["download_request"]
        metrics_payload = dict(payload["metrics_request"])
        local_download_id = create_local_task("/api/download-task", download_payload)
        wait_local_task(local_download_id, task_id, "local_download_task_id")
        metrics_payload["source_download_task_id"] = local_download_id
        local_metrics_id = create_local_task("/api/metrics-task", metrics_payload)
        wait_local_task(local_metrics_id, task_id, "local_metrics_task_id")
        artifact = download_export(local_metrics_id, output_dir)
    else:
        raise RuntimeError(f"Unsupported task kind: {kind}")

    object_paths = upload_artifacts(user_id, task_id, artifact)
    update_cloud_task(
        task_id,
        {
            "status": "completed",
            "progress": 100,
            "message": "任务完成，成果 ZIP 已上传" if len(object_paths) == 1 else f"任务完成，成果已拆成 {len(object_paths)} 个 ZIP 上传",
            "artifact_bucket": ARTIFACT_BUCKET,
            "artifact_path": object_paths[0] if len(object_paths) == 1 else json.dumps(object_paths, ensure_ascii=False),
            "artifact_size_bytes": artifact.stat().st_size,
            "completed_at": utc_now(),
        },
    )


def main() -> None:
    require_env()
    print(f"StreetScope cloud worker started: {WORKER_ID}")
    print(f"Local API: {LOCAL_API_BASE}")
    while True:
        try:
            task = fetch_next_task()
            if not task:
                time.sleep(POLL_SECONDS)
                continue
            print(f"Claimed task {task['id']} ({task['kind']})")
            handle_task(task)
            print(f"Completed task {task['id']}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"Worker error: {exc}", file=sys.stderr)
            task_id = locals().get("task", {}).get("id") if isinstance(locals().get("task"), dict) else None
            if task_id:
                try:
                    update_cloud_task(str(task_id), {"status": "failed", "error": str(exc), "message": "Worker 执行失败", "completed_at": utc_now()})
                except Exception as update_exc:
                    print(f"Failed to update failed task status: {update_exc}", file=sys.stderr)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
