"""Static checks over the web app, compose file and deploy docs (no database, no Node).

Each test pins one finding from the owner-login review so it cannot quietly come back:
- first-run setup refused for the NAME in the address bar must show the server's reason and
  the ways round it, not "use Tailscale";
- FORWARDED_ALLOW_IPS is the proxy network's subnet, never one container IP;
- the mcp service receives CLIMATE_AGENT_TOKEN;
- the Vite dev proxy honours CLIMATE_API_PROXY (DEV_API_PORT).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_setup_not_allowed_shows_the_servers_message():
    src = _read("web/src/lib/device.ts")
    case = re.search(r"case 'SETUP_NOT_ALLOWED':(.*?)case '", src, re.S)
    assert case, "authErrorText must still handle SETUP_NOT_ALLOWED"
    body = case.group(1)
    assert "e.message" in body, "the server explains why setup was refused; show its words"
    assert "Tailscale)" not in body, "do not answer a refused host name with 'use Tailscale'"


def test_setup_blocked_panel_explains_the_address_bar_name():
    hint = re.search(r"export const SETUP_HOST_HINT =(.*?)\n\n", _read("web/src/lib/device.ts"), re.S)
    assert hint, "SETUP_HOST_HINT is the shared explanation"
    text = hint.group(1)
    for must in ("IP address", "localhost", ".local", "CLIMATE_SETUP_HOSTS", "make password"):
        assert must in text, must
    view = _read("web/src/views/LoginView.vue")
    panel = re.search(r'<template v-if="setupBlocked">(.*?)</template>', view, re.S)
    assert panel, "the setup-blocked panel"
    assert "SETUP_HOST_HINT" in panel.group(1)
    assert "{{ error }}" in panel.group(1), "a refused setup's reason must be visible in the panel too"


def test_docs_say_use_the_ip_over_tailscale():
    for rel in ("docs/DEPLOY.md", "docs/LOCAL_DEVELOPMENT.md"):
        text = " ".join(_read(rel).split())
        assert "Over Tailscale, use the IP address (or add the MagicDNS name to `CLIMATE_SETUP_HOSTS`" in text, rel


def test_forwarded_allow_ips_is_the_subnet_not_a_container_ip():
    env = _read(".env.example")
    block = env[env.index("# --- Reverse proxy"):env.index("# FORWARDED_ALLOW_IPS=")]
    assert "SUBNET" in block and "never one container's IP" in block
    assert "set this to the nginx container's IP" not in block
    deploy = " ".join(_read("docs/DEPLOY.md").split())
    assert "use the nginx container's IP" not in deploy
    assert "to the **subnet** of the Docker network nginx connects from" in deploy
    assert "never to one container's IP" in deploy
    public = " ".join(_read("docs/PUBLIC_ACCESS.md").split())
    assert "FORWARDED_ALLOW_IPS=<the subnet of apprelay_gateway>" in public


def test_mcp_service_gets_the_agent_token():
    compose = yaml.safe_load(_read("docker-compose.yml"))
    env = compose["services"]["mcp"]["environment"]
    assert env.get("CLIMATE_AGENT_TOKEN") == "${CLIMATE_AGENT_TOKEN:-}"
    assert env.get("CLIMATE_MCP_TOKEN") == "${CLIMATE_MCP_TOKEN:-}"


def test_docs_say_a_cai_token_can_be_the_agent_token():
    for rel in ("docs/DEPLOY.md", "docs/LOCAL_DEVELOPMENT.md"):
        text = " ".join(_read(rel).split())
        assert "`CLIMATE_AGENT_TOKEN`" in text and "cai_" in text and "revocable in the app" in text, rel


def test_vite_proxy_honours_climate_api_proxy():
    cfg = _read("web/vite.config.ts")
    assert "process.env.CLIMATE_API_PROXY ?? 'http://127.0.0.1:8000'" in cfg
    proxy = re.search(r"'/api':\s*\{([^}]*)\}", cfg)
    assert proxy and "target: apiProxy" in proxy.group(1) and "ws: true" in proxy.group(1)
    script = _read("scripts/web-dev.sh")
    assert 'export CLIMATE_API_PROXY="http://127.0.0.1:$port"' in script
    assert "edit its proxy target" not in script, "the stale warning no longer applies"
