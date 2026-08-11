import json
import os
import re
import time
import hashlib
import ipaddress
from pathlib import Path
from urllib.parse import urlparse

import requests  # type: ignore

SUBSCRIPTIONS_FILE = "/etc/router-webui/subscriptions.json"
MAX_SUB_SIZE = 2 * 1024 * 1024  # 2MB


def _load_subs() -> list[dict]:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return []
    with open(SUBSCRIPTIONS_FILE, "r") as f:
        return json.load(f)


def _save_subs(subs: list[dict]) -> None:
    os.makedirs(os.path.dirname(SUBSCRIPTIONS_FILE), exist_ok=True)
    with open(SUBSCRIPTIONS_FILE, "w") as f:
        json.dump(subs, f, indent=2)


def _sanitize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if addr.is_loopback or addr.is_private or addr.is_reserved:
            raise ValueError(f"URL points to internal/reserved address: {hostname}")

    return url


def _generate_id() -> str:
    return hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]


def add_subscription(url: str, name: str = "") -> dict:
    url = _sanitize_url(url)
    subs = _load_subs()
    sub_id = _generate_id()
    entry = {
        "id": sub_id,
        "url": url,
        "name": name or url,
        "added_at": time.time(),
        "last_refresh": None,
        "node_count": 0,
    }
    subs.append(entry)
    _save_subs(subs)
    return entry


def list_subscriptions() -> list[dict]:
    return _load_subs()


def delete_subscription(sub_id: str) -> bool:
    subs = _load_subs()
    filtered = [s for s in subs if s["id"] != sub_id]
    if len(filtered) == len(subs):
        return False
    _save_subs(filtered)
    return True


def refresh_subscription(sub_id: str) -> dict:
    subs = _load_subs()
    for sub in subs:
        if sub["id"] == sub_id:
            url = _sanitize_url(sub["url"])
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()

            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > MAX_SUB_SIZE:
                    raise ValueError(f"Subscription response exceeds {MAX_SUB_SIZE // 1024 // 1024}MB limit")

            sub["last_refresh"] = time.time()
            sub["raw_content"] = content.decode("utf-8", errors="replace")
            _save_subs(subs)
            return sub
    raise KeyError(f"Subscription {sub_id} not found")


def get_subscription_content(sub_id: str) -> str:
    subs = _load_subs()
    for sub in subs:
        if sub["id"] == sub_id:
            if "raw_content" not in sub:
                sub = refresh_subscription(sub_id)
            return sub["raw_content"]
    raise KeyError(f"Subscription {sub_id} not found")
