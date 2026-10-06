"""Bounded, passive samples for troubleshooting a following-list scan.

These samples are diagnostics only. They never become a following baseline.
Raw responses, request headers, credentials and message bodies are never saved.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__


_PRIVATE_KEY = re.compile(
    r"cookie|token|authorization|password|secret|csrf|session|header|"
    r"message|inbox|direct|email|phone|biography|caption|avatar|picture|url", re.I
)
_COUNT_KEYS = {"count", "total_count", "following_count", "follower_count", "media_count"}
_FLAG_KEYS = {"has_next_page", "has_previous_page", "more_available", "big_list",
              "following", "followed_by", "outgoing_request", "incoming_request", "is_private"}
_PATH_WORDS = {"api", "v1", "friendships", "following", "followers", "users",
               "web_profile_info", "graphql", "query"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_json_body(body: bytes) -> Any:
    text = body.decode("utf-8-sig").lstrip()
    # Some JSON responses use an anti-execution prefix or deliver more than one
    # JSON document. Decode bounded documents; never evaluate response text.
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):].lstrip()
    if text.startswith(")]}'"):
        text = text.partition("\n")[2].lstrip()
    documents = []
    decoder = json.JSONDecoder()
    while text and len(documents) < 8:
        document, end = decoder.raw_decode(text)
        documents.append(document)
        text = text[end:].lstrip()
    if not documents:
        raise ValueError("empty response")
    return documents[0] if len(documents) == 1 and not text else {
        "_documents": documents, "_stream_truncated": bool(text)
    }


class RelationshipDiagnostics:
    MAX_BODY_BYTES = 2_000_000
    MAX_RESPONSES = 32
    MAX_PENDING = 4

    def __init__(self, page_provider: Callable[[], Any]) -> None:
        self.page_provider = page_provider
        self.started_at = _now()
        self.salt = secrets.token_bytes(32)
        self.samples: deque[dict[str, Any]] = deque(maxlen=self.MAX_RESPONSES // 2)
        self.first_samples: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.counters: Counter[str] = Counter()
        self.pending: set[asyncio.Task[Any]] = set()
        self.source: Any = None
        self.active = False
        self.attempt = 0
        self.sequence = 0
        self.outcome = "running"
        self.failure_type: str | None = None
        self.account_ref: str | None = None
        self.username_ref: str | None = None

    def fingerprint(self, value: Any) -> str:
        return hmac.new(self.salt, str(value).encode(), hashlib.sha256).hexdigest()[:20]

    def sanitize(self, payload: Any) -> Any:
        """Keep shape, counts and pagination equality without copying arbitrary text."""
        budget = 1200

        def visit(value: Any, key: str = "", depth: int = 0) -> Any:
            nonlocal budget
            budget -= 1
            if budget < 0 or depth > 14:
                return {"_omitted": True}
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for name, item in list(value.items())[:100]:
                    if budget < 0:
                        result["_truncated"] = True
                        break
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,99}", name):
                        continue
                    if _PRIVATE_KEY.search(name):
                        continue
                    result[name] = visit(item, name, depth + 1)
                return result
            if isinstance(value, list):
                items = []
                for item in value[:100]:
                    if budget < 0:
                        break
                    items.append(visit(item, key, depth + 1))
                return {"_array_length": len(value), "_items": items,
                        "_truncated": len(items) != len(value)}
            if value is None:
                return None
            if key in _COUNT_KEYS and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000_000:
                return value
            if key in _FLAG_KEYS and isinstance(value, bool):
                return value
            if key == "username" and isinstance(value, str):
                return {"_ref": self.fingerprint(value.strip().lower())}
            if key in {"id", "pk", "user_id", "target_user_id", "cursor", "end_cursor", "start_cursor", "next_max_id", "max_id", "after", "before"}:
                return {"_ref": self.fingerprint(value), "_empty": value == ""}
            return {"_type": type(value).__name__}

        return visit(payload)

    def start(self, source: Any) -> None:
        try:
            source.on("response", self._on_response)
        except Exception:
            self.counters["listener_unavailable"] += 1
            return
        self.source = source
        self.active = True

    def _on_response(self, response: Any) -> None:
        if not self.active:
            return
        try:
            parsed = urlparse(str(response.url))
            if parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"}:
                return
            path = parsed.path
            if not ("/graphql" in path or "/api/v1/friendships/" in path or "/api/v1/users/" in path):
                return
            request = response.request
            if request.frame.page is not self.page_provider():
                return
            if request.resource_type not in {"xhr", "fetch"}:
                return
            self.counters["candidate_responses"] += 1
            if len(self.pending) >= self.MAX_PENDING:
                self.counters["busy_skipped"] += 1
                return
            length = response.headers.get("content-length", "")
            if str(length).isdigit() and int(length) > self.MAX_BODY_BYTES:
                self.counters["oversize_skipped"] += 1
                return
            self.sequence += 1
            sequence, attempt = self.sequence, self.attempt
            endpoint = "/" + "/".join(part if part in _PATH_WORDS else ":value" for part in path.split("/") if part)
            query = parse_qs(parsed.query)
            request_fields: dict[str, Any] = {}
            for key in ("variables", "after", "before", "max_id", "count", "id"):
                if key in query:
                    value = query[key][0]
                    if len(value) < 20_000:
                        try:
                            request_fields[key] = json.loads(value)
                        except (ValueError, TypeError):
                            request_fields[key] = value
            try:
                raw = request.post_data or ""
                if len(raw) < 20_000 and raw:
                    try:
                        form = json.loads(raw)
                    except ValueError:
                        form = {key: values[0] for key, values in parse_qs(raw).items()}
                    if isinstance(form, dict):
                        variables = form.get("variables")
                        if isinstance(variables, str):
                            try:
                                variables = json.loads(variables)
                            except ValueError:
                                variables = None
                        request_fields["variables"] = variables
            except Exception:
                pass
            task = asyncio.create_task(self._capture(response, {
                "sequence": sequence, "attempt": attempt, "endpoint": endpoint,
                "path_refs": {str(index): self.fingerprint(part) for index, part in enumerate(path.split("/"))
                              if part and part not in _PATH_WORDS},
                "status": int(response.status), "request_shape": self.sanitize(request_fields),
            }))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)
        except Exception:
            self.counters["metadata_unavailable"] += 1

    async def _capture(self, response: Any, sample: dict[str, Any]) -> None:
        try:
            body = await asyncio.wait_for(response.body(), timeout=3.0)
            if len(body) > self.MAX_BODY_BYTES:
                self.counters["oversize_skipped"] += 1
                return
            payload = _decode_json_body(body)
            sample["response_shape"] = self.sanitize(payload)
            self.counters["captured"] += 1
            if len(self.first_samples) < self.MAX_RESPONSES // 2:
                self.first_samples.append(sample)
            else:
                self.samples.append(sample)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.counters["body_unavailable"] += 1

    def record_attempt(self, total: int | None, members: set[str], *, scroll: dict | None = None) -> None:
        if len(self.attempts) >= 5:
            return
        names = sorted(members)
        self.attempts.append({"attempt": self.attempt, "source_total": total,
                              "read_count": len(names), "scroll": {key:int((scroll or {}).get(key,0)) for key in ("attempts","movements","invalid")},
                              "member_refs": [self.fingerprint(name) for name in names[:20_000]],
                              "members_truncated": len(names) > 20_000})

    async def stop(self) -> None:
        self.active = False
        if self.source is not None:
            try:
                self.source.remove_listener("response", self._on_response)
            except Exception:
                pass
            self.source = None
        if self.pending:
            _, unfinished = await asyncio.wait(tuple(self.pending), timeout=3.2)
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

    def report(self) -> dict[str, Any]:
        responses = sorted([*self.first_samples, *self.samples], key=lambda item: item["sequence"])
        return {"format": "juxin-following-diagnostic-v1", "app_version": __version__,
                "started_at": self.started_at, "finished_at": _now(),
                "outcome": self.outcome, "failure_type": self.failure_type,
                "account_ref": self.account_ref, "username_ref": self.username_ref,
                "counters": dict(self.counters), "attempts": self.attempts,
                "response_samples": responses,
                "samples_dropped": max(0, self.counters["captured"] - len(responses)),
                "note": "Diagnostic samples only; strings and identifiers are redacted. Not a complete following list."}


class RelationshipDiagnosticStore:
    MAX_FILE_BYTES = 2_000_000
    MAX_EXPORT_BYTES = 8_000_000
    MAX_FILES = 10

    def __init__(self, root: Path) -> None:
        self.root = root

    def _directory(self, owner: str) -> Path:
        return self.root / hashlib.sha256(owner.encode()).hexdigest()

    def save(self, owner: str, profile: str, report: dict[str, Any]) -> None:
        # Diagnostic IO must never change the outcome of an actual scan.
        temporary: Path | None = None
        try:
            directory = self._directory(owner)
            directory.mkdir(parents=True, exist_ok=True)
            content = json.dumps(report, ensure_ascii=False).encode()
            if len(content) > self.MAX_FILE_BYTES:
                attempts = [{**item, "member_refs": item.get("member_refs", [])[:1000],
                             "members_truncated": True} for item in report.get("attempts", [])]
                report = {**report, "attempts": attempts, "response_samples": [], "file_budget_exceeded": True}
                content = json.dumps(report, ensure_ascii=False).encode()
            if len(content) > self.MAX_FILE_BYTES:
                return
            target = directory / (hashlib.sha256(profile.encode()).hexdigest() + ".json")
            temporary = target.with_suffix("." + secrets.token_hex(4) + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
            for old in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[self.MAX_FILES:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def export(self, owner: str) -> dict[str, Any]:
        reports = []
        total = 0
        try:
            paths = sorted(self._directory(owner).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        for path in paths[:self.MAX_FILES]:
            try:
                size = path.stat().st_size
                if size > self.MAX_FILE_BYTES or total + size > self.MAX_EXPORT_BYTES:
                    continue
                report = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(report, dict) and report.get("format") == "juxin-following-diagnostic-v1":
                    reports.append(report)
                    total += size
            except (OSError, ValueError):
                continue
        return {"app_version": __version__, "exported_at": _now(), "reports": reports}
