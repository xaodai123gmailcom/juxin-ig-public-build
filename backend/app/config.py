from __future__ import annotations

import ipaddress
import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "JuxinIGCollector"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "juxin-ig-collector"


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def validate_cloud_configuration(enabled: bool, project_url: str, publishable_key: str) -> tuple[bool, str, str]:
    """Validate explicit cloud opt-in without contacting a service or echoing keys."""
    if type(enabled) is not bool or type(project_url) is not str or type(publishable_key) is not str:
        raise ValueError("Cloud configuration must contain an enabled flag, HTTPS project origin and publishable key")
    url, key = project_url.strip(), publishable_key.strip()
    if url:
        try:
            parsed = urlparse(url)
            if (len(url) > 2048 or not url.isascii() or any(ord(c) <= 32 for c in url)
                    or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment
                    or "\\" in url or parsed.port not in {None, 443}):
                raise ValueError()
            hostname = parsed.hostname.lower()
            if re.search(r"(?:^|\.)(?:localhost|local|internal)$", hostname):
                raise ValueError()
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname) or "." not in hostname:
                    raise ValueError()
            else:
                # Match the desktop control: accept a configured DNS origin,
                # never an IP literal or a machine-local service address.
                raise ValueError()
            url = "https://" + ("[" + hostname + "]" if ":" in hostname else hostname)
        except ValueError:
            raise ValueError("Cloud project URL must be a public HTTPS origin without credentials, path or query") from None
    if key:
        if len(key) > 4096 or not key.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
            raise ValueError("Cloud key must be a Supabase publishable key or legacy anon key")
        if not re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]+", key):
            try:
                parts = key.split(".")
                if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
                    raise ValueError()
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                if not isinstance(claims, dict) or claims.get("role") != "anon":
                    raise ValueError()
            except (ValueError, TypeError, UnicodeDecodeError):
                raise ValueError("Cloud key must be a Supabase publishable key or legacy anon key; privileged keys are not supported") from None
    if enabled and not (url and key):
        raise ValueError("Enabling cloud requires an explicit project URL and publishable key")
    return enabled, url, key


@dataclass(frozen=True, slots=True)
class Settings:
    startup_token: str
    database_path: Path
    data_dir: Path
    bitbrowser_url: str = "http://127.0.0.1:54345"
    bitbrowser_api_key: str | None = None
    session_hours: int = 168
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    cloud_enabled: bool = False
    supabase_url: str = ""
    supabase_publishable_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("IGAC_STARTUP_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("IGAC_STARTUP_TOKEN must contain at least 32 characters")

        data_dir = Path(os.environ.get("IGAC_DATA_DIR", _default_data_dir()))
        db_path = Path(os.environ.get("IGAC_DB_PATH", data_dir / "collector.sqlite3"))
        bitbrowser_url = os.environ.get("IGAC_BITBROWSER_URL", "http://127.0.0.1:54345")
        if not _is_loopback_url(bitbrowser_url):
            raise RuntimeError("IGAC_BITBROWSER_URL must point to a loopback address")

        session_hours = int(os.environ.get("IGAC_SESSION_HOURS", "168"))
        if not 1 <= session_hours <= 24 * 365:
            raise RuntimeError("IGAC_SESSION_HOURS must be between 1 and 8760")

        bind_port = int(os.environ.get("IGAC_PORT", os.environ.get("COLLECTOR_CORE_PORT", "8765")))
        if not 1 <= bind_port <= 65535:
            raise RuntimeError("IGAC_PORT must be between 1 and 65535")

        cloud_flag = os.environ.get("IGAC_CLOUD_ENABLED", "false").strip().lower()
        if cloud_flag not in {"", "0", "false", "1", "true"}:
            raise RuntimeError("IGAC_CLOUD_ENABLED must be true or false")
        try:
            cloud_enabled, supabase_url, supabase_key = validate_cloud_configuration(
                cloud_flag in {"1", "true"}, os.environ.get("IGAC_SUPABASE_URL", ""),
                os.environ.get("IGAC_SUPABASE_PUBLISHABLE_KEY", ""))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None

        return cls(
            startup_token=token,
            database_path=db_path,
            data_dir=data_dir,
            bitbrowser_url=bitbrowser_url.rstrip("/"),
            bitbrowser_api_key=os.environ.get("IGAC_BITBROWSER_API_KEY") or None,
            cloud_enabled=cloud_enabled,
            supabase_url=supabase_url,
            supabase_publishable_key=supabase_key,
            session_hours=session_hours,
            bind_port=bind_port,
        )

    def validate_bind(self) -> None:
        try:
            address = ipaddress.ip_address(self.bind_host)
        except ValueError as exc:
            raise RuntimeError("The core service bind host must be a numeric loopback address") from exc
        if not address.is_loopback:
            raise RuntimeError("The core service may only bind to a loopback address")
