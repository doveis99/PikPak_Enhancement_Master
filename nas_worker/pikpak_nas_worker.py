#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def env_int(name: str, default: int, minimum: int, maximum: Optional[int] = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


TOKEN = os.getenv("PIKPAK_NAS_TOKEN", "")
BIND = os.getenv("PIKPAK_NAS_BIND", "0.0.0.0")
PORT = env_int("PIKPAK_NAS_PORT", 18082, 1, 65535)
ROOT = Path(os.getenv("PIKPAK_NAS_DOWNLOAD_ROOT", "/downloads")).resolve()
STATE_DIR = Path(os.getenv("PIKPAK_NAS_STATE_DIR", "/state")).resolve()
TEMP_DIR = Path(os.getenv("PIKPAK_NAS_TEMP_DIR", str(STATE_DIR / "tmp"))).resolve()
MAX_CONCURRENCY = env_int("PIKPAK_NAS_MAX_CONCURRENCY", 8, 1, 16)
CONCURRENCY = max(1, min(MAX_CONCURRENCY, env_int("PIKPAK_NAS_CONCURRENCY", 2, 1, MAX_CONCURRENCY)))
JOB_CONCURRENCY = env_int("PIKPAK_NAS_JOB_CONCURRENCY", 16, 1, 16)
MAX_ITEMS = env_int("PIKPAK_NAS_MAX_ITEMS", 200, 1, 1000)
OVERWRITE = os.getenv("PIKPAK_NAS_OVERWRITE", "0").lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS_RAW = os.getenv("PIKPAK_NAS_ALLOWED_HOSTS", "").strip()
BODY_LIMIT_BYTES = env_int("PIKPAK_NAS_BODY_LIMIT_BYTES", 32 * 1024 * 1024, 1024)
READ_CHUNK_BYTES = 1024 * 1024
SEGMENTED_MODE = os.getenv("PIKPAK_NAS_SEGMENTED", "auto").strip().lower()
if SEGMENTED_MODE not in {"auto", "on", "1", "true", "yes", "force", "off", "0", "false", "no", "disabled"}:
    SEGMENTED_MODE = "auto"
SEGMENT_COUNT = env_int("PIKPAK_NAS_SEGMENTS", 4, 1, 8)
SEGMENT_MIN_BYTES = env_int("PIKPAK_NAS_SEGMENT_MIN_BYTES", 64 * 1024 * 1024, 0)
SEGMENT_MIN_SPLIT_BYTES = env_int("PIKPAK_NAS_SEGMENT_MIN_SPLIT_BYTES", 32 * 1024 * 1024, 1024 * 1024, 1024 * 1024 * 1024)

STATE_FILE = STATE_DIR / "jobs.json"
TERMINAL_JOB_STATUSES = {"done", "partial", "failed", "canceled"}
DONE_ITEM_STATUSES = {"done", "skipped_existing"}
TERMINAL_ITEM_STATUSES = DONE_ITEM_STATUSES | {"failed", "canceled"}
INVALID_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def now_ts() -> float:
    return round(time.time(), 3)


def parse_allowed_hosts(raw: str) -> List[str]:
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


ALLOWED_HOSTS = parse_allowed_hosts(ALLOWED_HOSTS_RAW)


def host_allowed(host: str) -> bool:
    if not ALLOWED_HOSTS:
        return True
    host = (host or "").lower().rstrip(".")
    for pattern in ALLOWED_HOSTS:
        pattern = pattern.rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False


def validate_url(url: str) -> str:
    url = str(url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("item url must be http or https")
    if not host_allowed(parsed.hostname or ""):
        raise ValueError(f"host is not allowed: {parsed.hostname}")
    return url


class HostAllowRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


URL_OPENER = urllib.request.build_opener(HostAllowRedirectHandler)


def safe_component(component: Any) -> str:
    text = str(component or "").replace("\\", "/").split("/")[-1]
    text = INVALID_CHARS_RE.sub("_", text).strip().strip(".")
    if not text:
        text = "_"
    return text[:180]


def safe_relative_path(raw: Any) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        raise ValueError("relativePath is empty")
    if text.startswith("/") or DRIVE_RE.match(text):
        raise ValueError("relativePath must not be absolute")

    parts: List[str] = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("relativePath must not contain ..")
        parts.append(safe_component(part))
    if not parts:
        raise ValueError("relativePath is empty")
    return posixpath.join(*parts)


def resolve_target(relative_path: str) -> Path:
    target = (ROOT / Path(*relative_path.split("/"))).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ValueError("resolved target escapes download root")
    return target


def parse_size(value: Any) -> int:
    try:
        size = int(value)
        return size if size >= 0 else 0
    except Exception:
        return 0


CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


def parse_content_range(value: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    match = CONTENT_RANGE_RE.match(str(value or "").strip())
    if not match:
        return None, None, None
    start = parse_size(match.group(1))
    end = parse_size(match.group(2))
    total_raw = match.group(3)
    total = None if total_raw == "*" else parse_size(total_raw)
    return start, end, total


def clean_headers(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    blocked = {"host", "connection", "content-length", "transfer-encoding", "accept-encoding"}
    headers: Dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name or name.lower() in blocked or re.search(r"[\r\n:]", name):
            continue
        val = str(value or "").strip()
        if re.search(r"[\r\n]", val):
            continue
        headers[name] = val
    return headers


def item_for_state(item: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "id",
        "cloudId",
        "cloudParentId",
        "fileId",
        "parent_id",
        "name",
        "relativePath",
        "status",
        "bytesDone",
        "bytesTotal",
        "size",
        "error",
        "existing",
        "empty",
        "createdAt",
        "updatedAt",
        "downloadMode",
        "segments",
        "rangeParts",
    }
    return {k: item.get(k) for k in allowed if k in item}


def job_for_state(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "total": job.get("total", 0),
        "done": job.get("done", 0),
        "failed": job.get("failed", 0),
        "canceled": job.get("canceled", 0),
        "queued": job.get("queued", 0),
        "running": job.get("running", 0),
        "bytesDone": job.get("bytesDone", 0),
        "bytesTotal": job.get("bytesTotal", 0),
        "createdAt": job.get("createdAt"),
        "updatedAt": job.get("updatedAt"),
        "items": [item_for_state(item) for item in job.get("items", [])],
    }


class RuntimeLimiter:
    def __init__(self, concurrency: int) -> None:
        self.concurrency = max(1, min(MAX_CONCURRENCY, concurrency))
        self.active = 0
        self.cond = threading.Condition()

    def set_concurrency(self, value: Any) -> int:
        concurrency = max(1, min(MAX_CONCURRENCY, parse_size(value)))
        with self.cond:
            self.concurrency = concurrency
            self.cond.notify_all()
            return self.concurrency

    def acquire(self, cancel_event: threading.Event) -> bool:
        with self.cond:
            while self.active >= self.concurrency:
                if cancel_event.is_set():
                    return False
                self.cond.wait(0.5)
            if cancel_event.is_set():
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self.cond:
            if self.active > 0:
                self.active -= 1
            self.cond.notify_all()

    def snapshot(self) -> Dict[str, int]:
        with self.cond:
            return {
                "concurrency": self.concurrency,
                "activeDownloads": self.active,
                "maxConcurrency": MAX_CONCURRENCY,
                "jobConcurrency": JOB_CONCURRENCY,
            }


class DownloadWorker:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.cancel_events: Dict[str, threading.Event] = {}
        self.limiter = RuntimeLimiter(CONCURRENCY)
        self.job_executor = ThreadPoolExecutor(max_workers=JOB_CONCURRENCY, thread_name_prefix="job")
        self._last_persist = 0.0
        self._load_state()

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for job in data.get("jobs", []):
                job_id = str(job.get("id") or "")
                if not job_id:
                    continue
                job = dict(job)
                job["items"] = [dict(item) for item in job.get("items", [])]
                if job.get("status") not in TERMINAL_JOB_STATUSES:
                    job["status"] = "failed"
                    job["error"] = "worker restarted before job completed"
                    for item in job["items"]:
                        if item.get("status") not in TERMINAL_ITEM_STATUSES:
                            item["status"] = "failed"
                            item["error"] = "worker restarted before item completed"
                self.jobs[job_id] = job
                self.cancel_events[job_id] = threading.Event()
                self._refresh_job_counts(job)
        except Exception as exc:
            print(f"[state] failed to load state: {exc}", file=sys.stderr, flush=True)

    def _persist_state_locked(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        payload = {"version": 1, "jobs": [job_for_state(job) for job in self.jobs.values()]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
        self._last_persist = time.time()

    def persist_state(self, force: bool = False) -> None:
        with self.lock:
            if force or time.time() - self._last_persist >= 2.0:
                self._persist_state_locked()

    def _refresh_job_counts(self, job: Dict[str, Any]) -> None:
        items = job.get("items", [])
        job["total"] = len(items)
        job["done"] = sum(1 for item in items if item.get("status") in DONE_ITEM_STATUSES)
        job["failed"] = sum(1 for item in items if item.get("status") == "failed")
        job["canceled"] = sum(1 for item in items if item.get("status") == "canceled")
        job["queued"] = sum(1 for item in items if item.get("status") == "queued")
        job["running"] = sum(1 for item in items if item.get("status") == "running")
        job["bytesDone"] = sum(parse_size(item.get("bytesDone", 0)) for item in items)
        job["bytesTotal"] = sum(parse_size(item.get("bytesTotal", item.get("size", 0))) for item in items)
        job["updatedAt"] = now_ts()

    def create_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("items must be a non-empty array")
        if len(raw_items) > MAX_ITEMS:
            raise ValueError(f"too many items: {len(raw_items)} > {MAX_ITEMS}")
        if "concurrency" in payload:
            self.limiter.set_concurrency(payload.get("concurrency"))

        job_id = uuid.uuid4().hex
        items: List[Dict[str, Any]] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ValueError(f"item {index} must be an object")
            name = safe_component(raw.get("name") or f"download_{index + 1}")
            rel_raw = raw.get("relativePath") or name
            relative_path = safe_relative_path(rel_raw)
            target = resolve_target(relative_path)
            size = parse_size(raw.get("size", 0))
            is_empty = bool(raw.get("empty")) or size == 0
            url = str(raw.get("url") or "").strip()
            if not is_empty:
                validate_url(url)
            segments = max(1, min(8, parse_size(raw.get("segments", SEGMENT_COUNT))))
            items.append(
                {
                    "id": str(raw.get("id") or f"{job_id}_{index}"),
                    "cloudId": str(raw.get("cloudId") or raw.get("fileId") or raw.get("file_id") or ""),
                    "cloudParentId": str(raw.get("cloudParentId") or raw.get("parent_id") or raw.get("parentId") or ""),
                    "fileId": str(raw.get("fileId") or raw.get("file_id") or raw.get("cloudId") or ""),
                    "parent_id": str(raw.get("parent_id") or raw.get("parentId") or raw.get("cloudParentId") or ""),
                    "name": name,
                    "relativePath": relative_path,
                    "targetPath": str(target),
                    "url": url,
                    "empty": is_empty,
                    "headers": clean_headers(raw.get("headers")),
                    "size": size,
                    "bytesTotal": size,
                    "bytesDone": 0,
                    "status": "queued",
                    "downloadMode": "empty" if is_empty else "queued",
                    "segments": 1 if is_empty else segments,
                    "createdAt": now_ts(),
                    "updatedAt": now_ts(),
                }
            )

        job = {
            "id": job_id,
            "status": "queued",
            "total": len(items),
            "items": items,
            "createdAt": now_ts(),
            "updatedAt": now_ts(),
        }

        with self.lock:
            self.jobs[job_id] = job
            self.cancel_events[job_id] = threading.Event()
            self._refresh_job_counts(job)
            self._persist_state_locked()

        self.job_executor.submit(self._run_job, job_id)
        return self.summary(job_id)

    def summary(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            self._refresh_job_counts(job)
            return job_for_state(job)

    def list_jobs(self) -> Dict[str, Any]:
        with self.lock:
            jobs = []
            for job_id in sorted(self.jobs, key=lambda k: self.jobs[k].get("createdAt", 0), reverse=True):
                self._refresh_job_counts(self.jobs[job_id])
                jobs.append(job_for_state(self.jobs[job_id]))
            return {"jobs": jobs}

    def get_config(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = self.limiter.snapshot()
        payload.update({
            "ok": True,
            "maxItems": MAX_ITEMS,
            "allowedHostsConfigured": bool(ALLOWED_HOSTS),
            "segmented": SEGMENTED_MODE,
            "segments": SEGMENT_COUNT,
            "segmentMinBytes": SEGMENT_MIN_BYTES,
            "segmentMinSplitBytes": SEGMENT_MIN_SPLIT_BYTES,
        })
        return payload

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if "concurrency" not in payload:
            return self.get_config()
        self.limiter.set_concurrency(payload.get("concurrency"))
        return self.get_config()

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            event = self.cancel_events.setdefault(job_id, threading.Event())
            event.set()
            if job.get("status") == "queued":
                job["status"] = "canceled"
                for item in job.get("items", []):
                    if item.get("status") == "queued":
                        item["status"] = "canceled"
                        item["error"] = "job canceled"
                self._refresh_job_counts(job)
                self._persist_state_locked()
            return job_for_state(job)

    def _run_job(self, job_id: str) -> None:
        cancel_event = self.cancel_events[job_id]
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            if cancel_event.is_set():
                job["status"] = "canceled"
                self._refresh_job_counts(job)
                self._persist_state_locked()
                return
            job["status"] = "running"
            self._refresh_job_counts(job)
            self._persist_state_locked()

        futures = []
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix=f"item-{job_id[:8]}") as pool:
            with self.lock:
                items = list(self.jobs[job_id].get("items", []))
            for item in items:
                if cancel_event.is_set():
                    with self.lock:
                        if item.get("status") == "queued":
                            item["status"] = "canceled"
                            item["error"] = "job canceled"
                    continue
                futures.append(pool.submit(self._download_item, job_id, item["id"]))

            for future in as_completed(futures):
                if cancel_event.is_set():
                    for pending in futures:
                        pending.cancel()
                try:
                    future.result()
                except Exception as exc:
                    print(f"[job {job_id}] unhandled item error: {exc}", file=sys.stderr, flush=True)

        with self.lock:
            job = self.jobs[job_id]
            for item in job.get("items", []):
                if item.get("status") == "queued":
                    item["status"] = "canceled" if cancel_event.is_set() else "failed"
                    item["error"] = "job canceled" if cancel_event.is_set() else "item was not started"
                elif item.get("status") == "running":
                    item["status"] = "canceled" if cancel_event.is_set() else "failed"
                    item["error"] = "job canceled" if cancel_event.is_set() else "item was interrupted"
            self._refresh_job_counts(job)
            if cancel_event.is_set():
                job["status"] = "canceled"
            elif job["failed"] == 0 and job["canceled"] == 0:
                job["status"] = "done"
            elif job["done"] == 0:
                job["status"] = "failed"
            else:
                job["status"] = "partial"
            self._refresh_job_counts(job)
            self._persist_state_locked()

    def _find_item_locked(self, job_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        for item in job.get("items", []):
            if item.get("id") == item_id:
                return item
        return None

    def _set_item_fields(self, job_id: str, item_id: str, **fields: Any) -> None:
        with self.lock:
            item = self._find_item_locked(job_id, item_id)
            if not item:
                return
            item.update(fields)
            item["updatedAt"] = now_ts()
            job = self.jobs[job_id]
            self._refresh_job_counts(job)
        self.persist_state(force=False)

    def _item_segment_count(self, item: Dict[str, Any]) -> int:
        return max(1, min(8, parse_size(item.get("segments", SEGMENT_COUNT))))

    def _should_segment_item(self, item: Dict[str, Any], total_size: int) -> bool:
        if SEGMENTED_MODE in {"off", "0", "false", "no", "disabled"}:
            return False
        if self._item_segment_count(item) <= 1:
            return False
        if total_size <= 0:
            return False
        if SEGMENTED_MODE == "force":
            return True
        return total_size >= SEGMENT_MIN_BYTES

    def _probe_range_support(self, url: str, headers: Dict[str, str], expected_size: int) -> int:
        request_headers = dict(headers)
        request_headers["Range"] = "bytes=0-0"
        try:
            request = urllib.request.Request(url, headers=request_headers, method="GET")
            with URL_OPENER.open(request, timeout=30) as response:
                status_code = int(getattr(response, "status", response.getcode()))
                if status_code != 206:
                    return 0
                range_start, range_end, range_total = parse_content_range(response.headers.get("Content-Range"))
                response.read(1)
                if range_start != 0 or range_end != 0 or not range_total:
                    return 0
                if expected_size > 0 and range_total != expected_size:
                    return 0
                return range_total
        except Exception:
            return 0

    def _download_item_single(
        self,
        job_id: str,
        item_id: str,
        item: Dict[str, Any],
        part: Path,
        headers: Dict[str, str],
        expected_size: int,
        cancel_event: threading.Event,
    ) -> int:
        self._set_item_fields(job_id, item_id, downloadMode="single", segments=1)
        downloaded = 0
        target_total = expected_size
        with part.open("wb") as out:
            while True:
                if cancel_event.is_set():
                    raise InterruptedError("job canceled")

                request_headers = dict(headers)
                if expected_size > 0 or downloaded > 0:
                    request_headers["Range"] = f"bytes={downloaded}-"
                request = urllib.request.Request(item["url"], headers=request_headers, method="GET")
                before_request = downloaded

                with URL_OPENER.open(request, timeout=30) as response:
                    status_code = int(getattr(response, "status", response.getcode()))
                    range_start, _range_end, range_total = parse_content_range(response.headers.get("Content-Range"))
                    if downloaded > 0 and status_code != 206:
                        raise IOError(f"server did not resume from byte {downloaded} (HTTP {status_code})")
                    if status_code == 206 and range_start is not None and range_start != downloaded:
                        raise IOError(f"server resumed from byte {range_start}, expected {downloaded}")

                    response_length = parse_size(response.headers.get("Content-Length", 0))
                    if range_total:
                        target_total = max(target_total, range_total)
                    elif response_length and not target_total:
                        target_total = downloaded + response_length
                    self._set_item_fields(job_id, item_id, bytesTotal=target_total)

                    while True:
                        if cancel_event.is_set():
                            raise InterruptedError("job canceled")
                        chunk = response.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        self._set_item_fields(job_id, item_id, bytesDone=downloaded, bytesTotal=target_total)

                if downloaded == before_request:
                    break
                if target_total and downloaded >= target_total:
                    break
                if not target_total:
                    break
        return downloaded

    def _download_item_segmented(
        self,
        job_id: str,
        item_id: str,
        item: Dict[str, Any],
        part: Path,
        headers: Dict[str, str],
        total_size: int,
        segment_count: int,
        cancel_event: threading.Event,
    ) -> int:
        worker_count = max(1, min(segment_count, total_size))
        segments: List[Dict[str, Any]] = []
        base_size = total_size // worker_count
        remainder = total_size % worker_count
        cursor = 0
        for index in range(worker_count):
            length = base_size + (1 if index < remainder else 0)
            start = cursor
            end = cursor + length - 1
            segments.append({
                "id": index,
                "start": start,
                "end": end,
                "cursor": start,
                "path": part.with_name(f"{part.name}.seg{index:06d}"),
                "done": False,
            })
            cursor = end + 1

        segments_lock = threading.Lock()
        progress_lock = threading.Lock()
        segment_stop = threading.Event()
        downloaded_total = 0

        def add_progress(amount: int) -> int:
            nonlocal downloaded_total
            with progress_lock:
                downloaded_total += amount
                done = downloaded_total
            self._set_item_fields(job_id, item_id, bytesDone=done, bytesTotal=total_size)
            return done

        self._set_item_fields(
            job_id,
            item_id,
            downloadMode="segmented",
            segments=worker_count,
            rangeParts=len(segments),
            bytesDone=downloaded_total,
            bytesTotal=total_size,
        )

        def segment_remaining(seg: Dict[str, Any]) -> int:
            return max(0, int(seg["end"]) - int(seg["cursor"]) + 1)

        def take_split_segment() -> Optional[Dict[str, Any]]:
            with segments_lock:
                active = [seg for seg in segments if not seg.get("done") and segment_remaining(seg) > 0]
                if not active:
                    return None
                target_seg = max(active, key=segment_remaining)
                remaining = segment_remaining(target_seg)
                min_remaining = max(SEGMENT_MIN_SPLIT_BYTES * 2, READ_CHUNK_BYTES * 4)
                if remaining < min_remaining:
                    return None

                old_end = int(target_seg["end"])
                current = int(target_seg["cursor"])
                split_start = current + (remaining // 2)
                if split_start - current < SEGMENT_MIN_SPLIT_BYTES or old_end - split_start + 1 < SEGMENT_MIN_SPLIT_BYTES:
                    return None

                target_seg["end"] = split_start - 1
                new_id = len(segments)
                new_seg = {
                    "id": new_id,
                    "start": split_start,
                    "end": old_end,
                    "cursor": split_start,
                    "path": part.with_name(f"{part.name}.seg{new_id:06d}"),
                    "done": False,
                }
                segments.append(new_seg)
                self._set_item_fields(job_id, item_id, rangeParts=len(segments))
                return new_seg

        def fetch_segment(seg: Dict[str, Any]) -> None:
            seg_path = Path(seg["path"])
            expected_len = int(seg["end"]) - int(seg["start"]) + 1
            if seg_path.exists():
                seg_path.unlink()
            if expected_len <= 0:
                with segments_lock:
                    seg["done"] = True
                return
            with seg_path.open("ab") as out:
                while True:
                    if cancel_event.is_set():
                        raise InterruptedError("job canceled")
                    if segment_stop.is_set():
                        raise InterruptedError("segment stopped")

                    with segments_lock:
                        absolute_start = int(seg["cursor"])
                        request_end = int(seg["end"])
                    if absolute_start > request_end:
                        break

                    request_headers = dict(headers)
                    request_headers["Range"] = f"bytes={absolute_start}-{request_end}"
                    request = urllib.request.Request(item["url"], headers=request_headers, method="GET")
                    before_request = absolute_start

                    with URL_OPENER.open(request, timeout=30) as response:
                        status_code = int(getattr(response, "status", response.getcode()))
                        range_start, _range_end, _range_total = parse_content_range(response.headers.get("Content-Range"))
                        if status_code != 206:
                            raise IOError(f"server did not serve range {absolute_start}-{request_end} (HTTP {status_code})")
                        if range_start != absolute_start:
                            raise IOError(f"server served byte {range_start}, expected {absolute_start}")

                        while True:
                            if cancel_event.is_set():
                                raise InterruptedError("job canceled")
                            if segment_stop.is_set():
                                raise InterruptedError("segment stopped")
                            with segments_lock:
                                current_end = int(seg["end"])
                                current_cursor = int(seg["cursor"])
                            remaining = current_end - current_cursor + 1
                            if remaining <= 0:
                                break
                            chunk = response.read(min(READ_CHUNK_BYTES, remaining))
                            if not chunk:
                                break
                            out.write(chunk)
                            with segments_lock:
                                seg["cursor"] = int(seg["cursor"]) + len(chunk)
                            add_progress(len(chunk))

                    with segments_lock:
                        current_cursor = int(seg["cursor"])
                        current_end = int(seg["end"])
                    if current_cursor == before_request:
                        raise IOError(f"segment {int(seg['id']) + 1} stopped at byte {current_cursor}")
                    if current_cursor > current_end:
                        break

            with segments_lock:
                final_end = int(seg["end"])
                final_len = final_end - int(seg["start"]) + 1
                seg["done"] = True
            actual_len = seg_path.stat().st_size if seg_path.exists() else 0
            if actual_len != final_len:
                raise IOError(f"segment {int(seg['id']) + 1} size mismatch ({actual_len} != {final_len})")

        def worker_loop(initial_seg: Dict[str, Any]) -> None:
            seg: Optional[Dict[str, Any]] = initial_seg
            while seg is not None:
                if cancel_event.is_set():
                    raise InterruptedError("job canceled")
                if segment_stop.is_set():
                    raise InterruptedError("segment stopped")
                fetch_segment(seg)
                seg = take_split_segment()

        first_error: Optional[BaseException] = None
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"seg-{item_id[:8]}") as pool:
            futures = [pool.submit(worker_loop, seg) for seg in list(segments)]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    segment_stop.set()
                    for pending in futures:
                        pending.cancel()
        if first_error is not None:
            raise first_error

        with part.open("wb") as out:
            ordered_segments = sorted(segments, key=lambda s: int(s["start"]))
            expected_start = 0
            for seg in ordered_segments:
                if cancel_event.is_set():
                    raise InterruptedError("job canceled")
                start = int(seg["start"])
                end = int(seg["end"])
                seg_path = Path(seg["path"])
                if start != expected_start:
                    raise IOError(f"segment coverage gap at byte {expected_start}, got {start}")
                expected_len = end - start + 1
                if not seg_path.exists() or seg_path.stat().st_size != expected_len:
                    actual_len = seg_path.stat().st_size if seg_path.exists() else 0
                    raise IOError(f"segment {int(seg['id']) + 1} size mismatch ({actual_len} != {expected_len})")
                with seg_path.open("rb") as src:
                    while True:
                        chunk = src.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        out.write(chunk)
                expected_start = end + 1
            if expected_start != total_size:
                raise IOError(f"segment coverage ended at byte {expected_start}, expected {total_size}")

        downloaded = part.stat().st_size
        if downloaded != total_size:
            raise IOError(f"segmented size mismatch ({downloaded} != {total_size})")

        for seg in segments:
            try:
                Path(seg["path"]).unlink()
            except Exception:
                pass
        self._set_item_fields(job_id, item_id, bytesDone=downloaded, bytesTotal=total_size)
        return downloaded

    def _cleanup_part_files(self, part: Path) -> None:
        try:
            if part.exists():
                part.unlink()
        except Exception:
            pass
        try:
            prefix = f"{part.name}.seg"
            for seg_path in part.parent.iterdir():
                if not seg_path.name.startswith(prefix):
                    continue
                try:
                    seg_path.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            part.parent.rmdir()
        except Exception:
            pass

    def _build_part_path(self, job_id: str, item_id: str, target: Path) -> Path:
        part_dir = TEMP_DIR / job_id[:8]
        return part_dir / f"{safe_component(target.name)}.part-{job_id[:8]}-{item_id[:8]}"

    def _move_part_to_target(self, part: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not OVERWRITE:
                raise FileExistsError(f"target appeared before completion: {target}")
            if target.is_dir():
                raise IsADirectoryError(f"target is a directory: {target}")
            target.unlink()
        try:
            part.replace(target)
        except OSError:
            shutil.move(str(part), str(target))
        try:
            part.parent.rmdir()
        except Exception:
            pass

    def _download_item(self, job_id: str, item_id: str) -> None:
        cancel_event = self.cancel_events[job_id]
        with self.lock:
            item = dict(self._find_item_locked(job_id, item_id) or {})
        if not item:
            return

        target = Path(item["targetPath"])
        part = self._build_part_path(job_id, item_id, target)
        expected_size = parse_size(item.get("size", 0))
        is_empty_item = bool(item.get("empty")) or (expected_size == 0 and not item.get("url"))
        acquired_slot = False

        try:
            acquired_slot = self.limiter.acquire(cancel_event)
            if not acquired_slot:
                self._set_item_fields(job_id, item_id, status="canceled", error="job canceled")
                return

            if cancel_event.is_set():
                self._set_item_fields(job_id, item_id, status="canceled", error="job canceled")
                return

            if target.exists() and not OVERWRITE:
                existing_size = target.stat().st_size
                if is_empty_item and existing_size != 0:
                    self._set_item_fields(
                        job_id,
                        item_id,
                        status="failed",
                        bytesDone=existing_size,
                        bytesTotal=0,
                        error=f"target exists with different size ({existing_size} != 0)",
                    )
                    return
                if is_empty_item or expected_size <= 0 or existing_size == expected_size:
                    self._set_item_fields(
                        job_id,
                        item_id,
                        status="skipped_existing",
                        existing=True,
                        bytesDone=existing_size,
                        bytesTotal=existing_size,
                        error="",
                    )
                    return
                self._set_item_fields(
                    job_id,
                    item_id,
                    status="failed",
                    bytesDone=existing_size,
                    error=f"target exists with different size ({existing_size} != {expected_size})",
                )
                return

            target.parent.mkdir(parents=True, exist_ok=True)
            self._cleanup_part_files(part)
            part.parent.mkdir(parents=True, exist_ok=True)

            if is_empty_item:
                target.touch()
                self._set_item_fields(
                    job_id,
                    item_id,
                    status="done",
                    bytesDone=0,
                    bytesTotal=0,
                    error="",
                )
                return

            headers = dict(item.get("headers") or {})
            headers.setdefault("Accept-Encoding", "identity")
            self._set_item_fields(
                job_id,
                item_id,
                status="running",
                bytesDone=0,
                bytesTotal=expected_size,
                error="",
            )

            downloaded = 0
            segment_count = self._item_segment_count(item)
            range_total = 0
            if self._should_segment_item(item, expected_size):
                range_total = self._probe_range_support(item["url"], headers, expected_size)

            if range_total > 0 and self._should_segment_item(item, range_total):
                downloaded = self._download_item_segmented(
                    job_id,
                    item_id,
                    item,
                    part,
                    headers,
                    range_total,
                    segment_count,
                    cancel_event,
                )
            else:
                downloaded = self._download_item_single(
                    job_id,
                    item_id,
                    item,
                    part,
                    headers,
                    expected_size,
                    cancel_event,
                )

            if expected_size > 0 and downloaded != expected_size:
                raise IOError(f"downloaded size mismatch ({downloaded} != {expected_size})")

            self._move_part_to_target(part, target)
            self._set_item_fields(
                job_id,
                item_id,
                status="done",
                bytesDone=downloaded,
                bytesTotal=downloaded or expected_size,
                error="",
            )
        except InterruptedError as exc:
            self._cleanup_part_files(part)
            self._set_item_fields(job_id, item_id, status="canceled", error=str(exc))
        except urllib.error.HTTPError as exc:
            self._cleanup_part_files(part)
            self._set_item_fields(job_id, item_id, status="failed", error=f"HTTP {exc.code}: {exc.reason}")
        except Exception as exc:
            self._cleanup_part_files(part)
            self._set_item_fields(job_id, item_id, status="failed", error=str(exc)[:300])
        finally:
            if acquired_slot:
                self.limiter.release()


worker = DownloadWorker()


class Handler(BaseHTTPRequestHandler):
    server_version = "PikPakNASWorker/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PikPak-NAS-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PikPak-NAS-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _authorized(self) -> bool:
        header_token = self.headers.get("X-PikPak-NAS-Token", "")
        return bool(TOKEN) and secrets.compare_digest(header_token, TOKEN)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > BODY_LIMIT_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid json: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("json body must be an object")
        return data

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            runtime = worker.get_config()
            payload = {
                "ok": True,
                "configured": bool(TOKEN),
                "concurrency": runtime["concurrency"],
                "activeDownloads": runtime["activeDownloads"],
                "maxConcurrency": runtime["maxConcurrency"],
                "jobConcurrency": runtime["jobConcurrency"],
                "maxItems": MAX_ITEMS,
                "allowedHostsConfigured": bool(ALLOWED_HOSTS),
                "segmented": runtime["segmented"],
                "segments": runtime["segments"],
                "segmentMinBytes": runtime["segmentMinBytes"],
                "segmentMinSplitBytes": runtime["segmentMinSplitBytes"],
            }
            if self._authorized():
                payload.update({"downloadRoot": str(ROOT), "stateDir": str(STATE_DIR), "tempDir": str(TEMP_DIR), "overwrite": OVERWRITE})
            self._send_json(200, payload)
            return

        if not self._require_auth():
            return

        try:
            if path == "/config":
                self._send_json(200, worker.get_config())
                return
            if path == "/jobs":
                self._send_json(200, worker.list_jobs())
                return
            if path.startswith("/jobs/"):
                job_id = path.split("/", 2)[2]
                self._send_json(200, worker.summary(job_id))
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        except KeyError:
            self._send_json(404, {"ok": False, "error": "job not found"})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if not self._require_auth():
            return

        try:
            if path == "/config":
                payload = self._read_json()
                self._send_json(200, worker.update_config(payload))
                return
            if path == "/jobs":
                payload = self._read_json()
                self._send_json(201, worker.create_job(payload))
                return
            if path.startswith("/jobs/") and path.endswith("/cancel"):
                parts = path.split("/")
                if len(parts) != 4:
                    self._send_json(404, {"ok": False, "error": "not found"})
                    return
                self._send_json(200, worker.cancel_job(parts[2]))
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        except KeyError:
            self._send_json(404, {"ok": False, "error": "job not found"})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if not TOKEN:
        print("[config] PIKPAK_NAS_TOKEN is required before accepting jobs", file=sys.stderr, flush=True)

    server = ThreadingHTTPServer((BIND, PORT), Handler)
    stop = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        print(f"[signal] received {signum}, shutting down", flush=True)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(
        f"[start] listening on {BIND}:{PORT}, root={ROOT}, state={STATE_DIR}, temp={TEMP_DIR}, concurrency={CONCURRENCY}, max_concurrency={MAX_CONCURRENCY}, job_concurrency={JOB_CONCURRENCY}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        with worker.lock:
            worker._persist_state_locked()
        worker.job_executor.shutdown(wait=False, cancel_futures=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
