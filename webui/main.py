import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Optional

import yaml  # type: ignore
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from subscription import (
    add_subscription,
    delete_subscription,
    get_subscription_content,
    list_subscriptions,
    refresh_subscription,
)
from clash2xray import parse_clash_yaml

APP_DIR = "/opt/router-webui"
CONFIG_DIR = "/etc/router-webui"
STATE_DIR = "/etc/router-webui/state"
SECRET_FILE = "/etc/router-webui/secret"
XRAY_CONFIG = "/etc/xray/client.json"
HOSTAPD_CONFIG = "/etc/hostapd/hostapd.conf"
PROXY_STATE_FILE = "/etc/router-webui/state/proxy"
BACKUP_SCRIPT = "/usr/local/bin/router-backup.sh"
ROLLBACK_SCRIPT = "/usr/local/bin/router-rollback.sh"
TOGGLE_SCRIPT = "/usr/local/bin/router-proxy-toggle.sh"

ALLOWED_SERVICES = {
    "router-firewall",
    "router-ap",
    "router-dns",
    "router-xray",
    "router-webui",
    "hostapd",
    "dnsmasq",
}

app = FastAPI(title="NUC Router WebUI")

WEBUI_DIR = Path(__file__).parent / "static"
if WEBUI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")


def _get_token() -> str:
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    if not os.path.exists(SECRET_FILE):
        token = secrets.token_hex(32)
        with open(SECRET_FILE, "w") as f:
            f.write(token)
        os.chmod(SECRET_FILE, 0o600)
    with open(SECRET_FILE) as f:
        return f.read().strip()


def _verify_token(x_auth_token: Optional[str] = Header(None)) -> str:
    expected = _get_token()
    if not x_auth_token or x_auth_token != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing auth token")
    return x_auth_token


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _read_state(key: str) -> str:
    path = os.path.join(STATE_DIR, key)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def _write_state(key: str, value: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, key), "w") as f:
        f.write(value)


def _load_json(path: str, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


@app.get("/")
async def index():
    index_path = WEBUI_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "NUC Router WebUI", "docs": "/docs"}


@app.get("/api/status")
async def get_status(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)

    # WAN IP
    rc, wan_out, _ = _run(["ip", "-4", "addr", "show", "eno1"])
    wan_ip = ""
    for line in wan_out.split("\n"):
        if "inet " in line:
            wan_ip = line.strip().split()[1].split("/")[0]
            break

    # AP status
    ap_active = subprocess.run(
        ["systemctl", "is-active", "router-ap"], capture_output=True, text=True
    ).stdout.strip() == "active"

    # Xray status
    xray_active = subprocess.run(
        ["systemctl", "is-active", "router-xray"], capture_output=True, text=True
    ).stdout.strip() == "active"

    # Proxy toggle state
    proxy_enabled = _read_state("proxy") == "on"

    # Client count
    rc, clients_out, _ = _run(["iw", "dev", "wlp0s20f3", "station", "dump"])
    client_count = clients_out.count("Station ") if rc == 0 else 0

    return {
        "wan_ip": wan_ip,
        "ap_active": ap_active,
        "xray_active": xray_active,
        "proxy_enabled": proxy_enabled,
        "client_count": client_count,
        "timestamp": time.time(),
    }


@app.get("/api/clients")
async def get_clients(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)

    rc, out, err = _run(["iw", "dev", "wlp0s20f3", "station", "dump"])
    if rc != 0:
        return {"clients": [], "error": err.strip()}

    clients = []
    current: dict = {}
    for line in out.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Station "):
            if current:
                clients.append(current)
            mac = stripped.split()[1]
            current = {"mac": mac, "signal": "", "ip": ""}
        elif "signal:" in stripped and current:
            current["signal"] = stripped.split(":")[-1].strip()
    if current:
        clients.append(current)

    # Enrich with DHCP leases if possible
    if os.path.exists("/var/lib/misc/dnsmasq.leases"):
        with open("/var/lib/misc/dnsmasq.leases") as f:
            for lease_line in f:
                parts = lease_line.strip().split()
                if len(parts) >= 3:
                    lease_mac = parts[1].lower()
                    lease_ip = parts[2]
                    for c in clients:
                        if c.get("mac", "").lower() == lease_mac and not c["ip"]:
                            c["ip"] = lease_ip

    # Try arp for IPs
    for c in clients:
        if not c["ip"] and c["mac"]:
            rc2, arp_out, _ = _run(["ip", "neigh", "show", "dev", "wlp0s20f3"])
            for arp_line in arp_out.split("\n"):
                if c["mac"].lower() in arp_line.lower():
                    parts = arp_line.strip().split()
                    if parts:
                        c["ip"] = parts[0]
                    break

    return {"clients": clients}


@app.get("/api/config/hotspot")
async def get_hotspot(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    config = {}
    try:
        with open(HOSTAPD_CONFIG) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return {"config": config}


@app.post("/api/config/hotspot")
async def post_hotspot(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()
    ssid = body.get("ssid")
    password = body.get("password")
    channel = body.get("channel")

    if not os.path.exists(HOSTAPD_CONFIG):
        raise HTTPException(status_code=500, detail="hostapd config not found")

    with open(HOSTAPD_CONFIG) as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        if ssid and line.strip().startswith("ssid="):
            new_lines.append(f"ssid={ssid}\n")
        elif password and line.strip().startswith("wpa_passphrase="):
            new_lines.append(f"wpa_passphrase={password}\n")
        elif channel and line.strip().startswith("channel="):
            new_lines.append(f"channel={channel}\n")
        else:
            new_lines.append(line)

    with open(HOSTAPD_CONFIG, "w") as f:
        f.writelines(new_lines)

    _run(["systemctl", "restart", "router-ap"])
    return {"status": "ok", "message": "Hotspot config updated, AP restarting"}


@app.get("/api/proxy")
async def get_proxy(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    enabled = _read_state("proxy") == "on"

    # Parse xray config for node list
    xconfig = _load_json(XRAY_CONFIG, {"outbounds": []})
    nodes = []
    active_node = ""
    for ob in xconfig.get("outbounds", []):
        tag = ob.get("tag", "")
        protocol = ob.get("protocol", "")
        if protocol in ("freedom", "blackhole"):
            continue
        nodes.append({
            "id": tag,
            "tag": tag,
            "protocol": protocol,
            "active": False,
        })
    # Read active node from state
    active_node = _read_state("active_node")
    for n in nodes:
        if n["tag"] == active_node:
            n["active"] = True

    return {
        "proxy_enabled": enabled,
        "active_node": active_node or "primary",
        "nodes": nodes,
    }


@app.post("/api/proxy/toggle")
async def proxy_toggle(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()
    action = body.get("action", "on")
    if action not in ("on", "off"):
        raise HTTPException(status_code=400, detail="action must be 'on' or 'off'")

    rc, out, err = _run([TOGGLE_SCRIPT, action], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Toggle failed: {err}")
    return {"status": "ok", "proxy_enabled": action == "on"}


@app.put("/api/proxy/config")
async def proxy_config(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()

    # Write temp config
    tmp_path = "/tmp/xray-config-test.json"
    _save_json(tmp_path, body)

    # Validate with xray run -test
    rc, out, err = _run(["xray", "run", "-test", "-config", tmp_path], timeout=10)
    if rc != 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=f"Config validation failed: {err}")

    # Backup existing config
    _run([BACKUP_SCRIPT], timeout=30)

    # Write new config
    os.makedirs(os.path.dirname(XRAY_CONFIG), exist_ok=True)
    _save_json(XRAY_CONFIG, body)

    # Cleanup temp
    os.unlink(tmp_path)

    # Reload xray
    _run(["systemctl", "restart", "router-xray"], timeout=10)

    return {"status": "ok", "message": "Xray config saved and reloaded"}


@app.post("/api/proxy/node/{node_id}/activate")
async def activate_node(node_id: str, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)

    xconfig = _load_json(XRAY_CONFIG)
    outbounds = xconfig.get("outbounds", [])
    found = False
    for ob in outbounds:
        if ob.get("tag") == node_id:
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")

    # Update routing to use this node as primary
    routing = xconfig.get("routing", {})
    rules = routing.get("rules", [])
    updated = False
    for rule in rules:
        if rule.get("type") == "field" and rule.get("inboundTag") == ["tproxy"]:
            rule["outboundTag"] = node_id
            rule.pop("targetTag", None)  # not a valid xray field
            updated = True

    if not any(r.get("outboundTag") == node_id for r in rules if isinstance(r, dict)):
        rules.append({"type": "field", "inboundTag": ["tproxy"], "outboundTag": node_id})
        updated = True

    if not updated:
        raise HTTPException(status_code=400, detail="No tproxy routing rule found")

    # Validate config before saving (broken config kills xray on reload)
    tmp_path = "/tmp/xray-activate-test.json"
    _save_json(tmp_path, xconfig)
    rc, out, err = _run(["xray", "run", "-test", "-config", tmp_path], timeout=10)
    os.unlink(tmp_path)
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"Config validation failed: {err}")

    xconfig["routing"] = routing
    _save_json(XRAY_CONFIG, xconfig)
    _write_state("active_node", node_id)

    _run(["systemctl", "restart", "router-xray"], timeout=10)
    return {"status": "ok", "active_node": node_id}


@app.post("/api/proxy/test")
async def proxy_test(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()
    url = body.get("url", "https://1.1.1.1/cdn-cgi/trace")
    timeout_val = body.get("timeout", 10)

    # Test via TPROXY (uses fwmark=1)
    rc, out, err = _run(
        ["curl", "-s", "--max-time", str(timeout_val), url], timeout=timeout_val + 5
    )
    return {
        "success": rc == 0,
        "output": out[:2000],
        "error": err[:500] if err else "",
    }


@app.post("/api/subscription")
async def create_subscription(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()
    url = body.get("url", "").strip()
    name = body.get("name", "").strip()

    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    try:
        entry = add_subscription(url, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "ok", "subscription": entry}


@app.get("/api/subscription")
async def get_subscriptions(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    return {"subscriptions": list_subscriptions()}


@app.post("/api/subscription/{sub_id}/refresh")
async def refresh_sub(sub_id: str, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    try:
        sub = refresh_subscription(sub_id)
        return {"status": "ok", "subscription": sub}
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscription not found")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {str(e)}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/subscription/{sub_id}")
async def delete_sub(sub_id: str, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    if delete_subscription(sub_id):
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Subscription not found")


@app.post("/api/proxy/import")
async def proxy_import(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()
    yaml_text = body.get("yaml", "")
    sub_id = body.get("subscription_id", "")

    if sub_id:
        try:
            yaml_text = get_subscription_content(sub_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Subscription not found")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    if not yaml_text:
        raise HTTPException(status_code=400, detail="No YAML content provided")

    try:
        nodes = parse_clash_yaml(yaml_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML parse error: {str(e)}")

    return {"nodes": nodes, "total": len(nodes)}


@app.post("/api/proxy/import/confirm")
async def proxy_import_confirm(request: Request, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    body = await request.json()
    nodes = body.get("nodes", [])

    if not nodes:
        raise HTTPException(status_code=400, detail="No nodes selected")

    xconfig = _load_json(XRAY_CONFIG)
    existing_outbounds = xconfig.get("outbounds", [])
    existing_tags = {ob["tag"] for ob in existing_outbounds if "tag" in ob}

    new_outbounds = []
    for node in nodes:
        tag = node.get("tag", "")
        if tag and tag in existing_tags and tag not in ("direct", "blocked"):
            tag = f"{tag}-{hashlib.md5(str(time.time()).encode()).hexdigest()[:6]}"
            node["tag"] = tag

        clean = {
            "tag": tag,
            "protocol": node.get("protocol", ""),
        }
        if "settings" in node:
            clean["settings"] = node["settings"]
        if "streamSettings" in node:
            clean["streamSettings"] = node["streamSettings"]
        new_outbounds.append(clean)

    # Keep direct and blocked outbounds
    preserved = [ob for ob in existing_outbounds if ob.get("tag") in ("direct", "blocked")]
    all_outbounds = new_outbounds + preserved

    tmp_path = "/tmp/xray-import-test.json"
    test_config = {**xconfig, "outbounds": all_outbounds}
    _save_json(tmp_path, test_config)

    rc, out, err = _run(["xray", "run", "-test", "-config", tmp_path], timeout=10)
    if rc != 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=f"Config validation failed: {err}")

    _run([BACKUP_SCRIPT], timeout=30)
    os.makedirs(os.path.dirname(XRAY_CONFIG), exist_ok=True)
    _save_json(XRAY_CONFIG, test_config)
    os.unlink(tmp_path)

    _run(["systemctl", "restart", "router-xray"], timeout=10)

    return {"status": "ok", "imported": len(new_outbounds), "nodes": new_outbounds}


@app.post("/api/service/{name}/restart")
async def restart_service(name: str, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    if name not in ALLOWED_SERVICES:
        raise HTTPException(status_code=403, detail=f"Service '{name}' not in allowed list")

    rc, out, err = _run(["systemctl", "restart", name], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Restart failed: {err}")
    return {"status": "ok", "service": name}


@app.get("/api/logs/{service}")
async def get_logs(service: str, lines: int = 100, token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    rc, out, err = _run(
        ["journalctl", "-u", service, "--no-pager", "-n", str(lines)], timeout=10
    )
    return {"service": service, "logs": out[:50000]}


@app.post("/api/backup")
async def backup(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    rc, out, err = _run([BACKUP_SCRIPT], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Backup failed: {err}")
    return {"status": "ok", "backup_path": out.strip()}


@app.post("/api/rollback")
async def rollback(token: str = Header(None, alias="X-Auth-Token")):
    _verify_token(token)
    rc, out, err = _run([ROLLBACK_SCRIPT], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {err}")
    return {"status": "ok", "message": "Rollback completed"}
