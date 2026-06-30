from __future__ import annotations

import os
from pathlib import Path

import requests


def load_env(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Missing env file: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_env(root / "worker" / ".env")
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    local_api = os.environ.get("STREETSCOPE_LOCAL_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    if not supabase_url or not service_key:
        raise SystemExit("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing")

    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    checks = [
        ("Supabase projects table", f"{supabase_url}/rest/v1/streetscope_projects?select=id&limit=1"),
        ("Supabase tasks table", f"{supabase_url}/rest/v1/streetscope_tasks?select=id&limit=1"),
        ("Supabase artifact bucket", f"{supabase_url}/storage/v1/bucket/streetscope-artifacts"),
    ]
    for label, url in checks:
        response = requests.get(url, headers=headers, timeout=20)
        print(f"{label}: HTTP {response.status_code}")
        response.raise_for_status()

    try:
        response = requests.get(f"{local_api}/api/health", timeout=5)
        print(f"Local StreetScope backend: HTTP {response.status_code}")
    except requests.RequestException:
        print("Local StreetScope backend: not running yet")

    print("Worker environment check complete.")


if __name__ == "__main__":
    main()

