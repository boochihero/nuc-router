import yaml  # type: ignore
from typing import Any


UNSUPPORTED_PROTOCOLS = {"ssr", "tuic"}
SUPPORTED_PROTOCOLS = {"vless", "vmess", "ss", "trojan", "hysteria2"}


def parse_clash_yaml(yaml_text: str) -> list[dict]:
    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        raise ValueError("Invalid YAML: root must be a mapping")

    proxies = data.get("proxies", [])
    if not isinstance(proxies, list):
        raise ValueError("Invalid YAML: 'proxies' must be a list")

    results = []
    for proxy in proxies:
        if not isinstance(proxy, dict):
            results.append({"error": "Proxy entry is not a mapping", "raw": str(proxy)})
            continue

        ptype = proxy.get("type", "").lower()
        if ptype in UNSUPPORTED_PROTOCOLS:
            results.append({
                "name": proxy.get("name", "unknown"),
                "type": ptype,
                "supported": False,
                "error": f"Protocol '{ptype}' is not supported by Xray",
            })
            continue

        if ptype not in SUPPORTED_PROTOCOLS:
            results.append({
                "name": proxy.get("name", "unknown"),
                "type": ptype,
                "supported": False,
                "error": f"Unknown protocol: {ptype}",
            })
            continue

        try:
            xray_outbound = _convert_proxy(proxy, ptype)
            xray_outbound["supported"] = True
            results.append(xray_outbound)
        except Exception as e:
            results.append({
                "name": proxy.get("name", "unknown"),
                "type": ptype,
                "supported": False,
                "error": str(e),
            })

    return results


def _convert_proxy(proxy: dict, ptype: str) -> dict:
    converters = {
        "vless": _convert_vless,
        "vmess": _convert_vmess,
        "ss": _convert_ss,
        "trojan": _convert_trojan,
        "hysteria2": _convert_hysteria2,
    }
    converter = converters.get(ptype)
    if not converter:
        raise ValueError(f"No converter for protocol: {ptype}")
    return converter(proxy)


def _common_stream_settings(proxy: dict) -> dict:
    settings: dict[str, Any] = {}

    network = proxy.get("network", "tcp")
    settings["network"] = network

    if network == "ws":
        ws_opts = {}
        if "ws-opts" in proxy:
            wo = proxy["ws-opts"]
            if "path" in wo:
                ws_opts["path"] = wo["path"]
            if "headers" in wo and "Host" in wo["headers"]:
                ws_opts["headers"] = {"Host": wo["headers"]["Host"]}
        if ws_opts:
            settings["wsSettings"] = ws_opts

    if network == "grpc":
        grpc_opts = {}
        if "grpc-opts" in proxy:
            go = proxy["grpc-opts"]
            if "grpc-service-name" in go:
                grpc_opts["serviceName"] = go["grpc-service-name"]
        if grpc_opts:
            settings["grpcSettings"] = grpc_opts

    tls_val = proxy.get("tls", False)
    if tls_val is True or str(tls_val).lower() == "true":
        settings["security"] = "tls"
        sni = proxy.get("servername", proxy.get("sni", ""))
        if sni:
            settings["tlsSettings"] = {"serverName": sni}

    reality_opts = _parse_reality(proxy)
    if reality_opts:
        settings["security"] = "reality"
        settings["realitySettings"] = reality_opts

    return settings


def _parse_reality(proxy: dict) -> dict | None:
    keys = ["reality-opts", "reality"]
    for k in keys:
        if k in proxy:
            ro = proxy[k]
            if isinstance(ro, dict):
                result = {}
                if "public-key" in ro:
                    result["publicKey"] = ro["public-key"]
                if "short-id" in ro:
                    result["shortId"] = ro["short-id"]
                if "server-name" in ro or "servername" in ro:
                    result["serverName"] = ro.get("server-name", ro.get("servername", ""))
                if "fingerprint" in ro:
                    result["fingerprint"] = ro["fingerprint"]
                return result
    return None


def _convert_vless(proxy: dict) -> dict:
    outbound: dict[str, Any] = {
        "name": proxy.get("name", "unnamed"),
        "type": "vless",
        "tag": proxy.get("name", "vless-node"),
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": proxy.get("server", ""),
                    "port": int(proxy.get("port", 443)),
                    "users": [
                        {
                            "id": proxy.get("uuid", ""),
                            "encryption": proxy.get("encryption", "none"),
                            "flow": proxy.get("flow", ""),
                        }
                    ],
                }
            ]
        },
    }
    stream = _common_stream_settings(proxy)
    if stream:
        outbound["streamSettings"] = stream
    return outbound


def _convert_vmess(proxy: dict) -> dict:
    outbound: dict[str, Any] = {
        "name": proxy.get("name", "unnamed"),
        "type": "vmess",
        "tag": proxy.get("name", "vmess-node"),
        "protocol": "vmess",
        "settings": {
            "vnext": [
                {
                    "address": proxy.get("server", ""),
                    "port": int(proxy.get("port", 443)),
                    "users": [
                        {
                            "id": proxy.get("uuid", ""),
                            "security": proxy.get("cipher", "auto"),
                            "alterId": int(proxy.get("alterId", proxy.get("alter-id", 0))),
                        }
                    ],
                }
            ]
        },
    }
    stream = _common_stream_settings(proxy)
    if stream:
        outbound["streamSettings"] = stream
    return outbound


def _convert_ss(proxy: dict) -> dict:
    cipher = proxy.get("cipher", "aes-256-gcm")
    password = proxy.get("password", "")
    outbound: dict[str, Any] = {
        "name": proxy.get("name", "unnamed"),
        "type": "ss",
        "tag": proxy.get("name", "ss-node"),
        "protocol": "shadowsocks",
        "settings": {
            "servers": [
                {
                    "address": proxy.get("server", ""),
                    "port": int(proxy.get("port", 443)),
                    "method": cipher,
                    "password": password,
                }
            ]
        },
    }
    stream = _common_stream_settings(proxy)
    if stream:
        outbound["streamSettings"] = stream
    return outbound


def _convert_trojan(proxy: dict) -> dict:
    outbound: dict[str, Any] = {
        "name": proxy.get("name", "unnamed"),
        "type": "trojan",
        "tag": proxy.get("name", "trojan-node"),
        "protocol": "trojan",
        "settings": {
            "servers": [
                {
                    "address": proxy.get("server", ""),
                    "port": int(proxy.get("port", 443)),
                    "password": proxy.get("password", ""),
                }
            ]
        },
    }
    stream = _common_stream_settings(proxy)
    if stream:
        outbound["streamSettings"] = stream
    return outbound


def _convert_hysteria2(proxy: dict) -> dict:
    outbound: dict[str, Any] = {
        "name": proxy.get("name", "unnamed"),
        "type": "hysteria2",
        "tag": proxy.get("name", "hysteria2-node"),
        "protocol": "hysteria2",
        "settings": {
            "servers": [
                {
                    "address": proxy.get("server", ""),
                    "port": int(proxy.get("port", 443)),
                    "password": proxy.get("password", ""),
                }
            ]
        },
    }
    return outbound
