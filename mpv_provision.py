"""Download the pinned, SHA-256-verified mpv build that usage-float plays wallpapers with.

The repository does not ship mpv (GPL). The pin (URLs, size, SHA-256) lives in mpv-pin.json, shared with
install-mpv.ps1. The default target is ~/.usage-float/runtime/mpv, which usage-float already searches and which
survives a re-clone of the plugin. Standard library only, so AgentCompass can load this file from the plugin
directory and call ``ensure_mpv()`` after installing or updating usage-float:

    python mpv_provision.py [--target DIR] [--force]
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
PIN_PATH = HERE / "mpv-pin.json"
LICENSE_DIR = HERE / "licenses" / "mpv"
MARKER_NAME = ".usage-float-mpv.json"
USER_AGENT = "usage-float-mpv-provision"
CHUNK = 256 * 1024

Progress = Callable[[int, int], None]


class ProvisionError(RuntimeError):
    """A provisioning failure whose message can be shown to the user as is."""


def default_target() -> Path:
    return Path.home() / ".usage-float" / "runtime" / "mpv"


def load_pin(path: Path | None = None) -> dict[str, Any]:
    pin = json.loads((path or PIN_PATH).read_text(encoding="utf-8"))
    for key in ("archive", "sha256", "urls", "files"):
        if not pin.get(key):
            raise ProvisionError(f"mpv-pin.json is missing {key}")
    return pin


def installed(target: Path | None = None, pin: dict[str, Any] | None = None) -> bool:
    """True when target holds this pin's mpv (an older provisioned build counts as missing)."""
    target = Path(target or default_target())
    pin = pin or load_pin()
    try:
        marker = json.loads((target / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return marker.get("sha256") == pin["sha256"] and all((target / name).is_file() for name in pin["files"])


def _short_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None) or exc
    text = str(reason).strip() or exc.__class__.__name__
    return text if len(text) <= 160 else text[:157] + "..."


def _curl_executable() -> str | None:
    """Windows 10+ ships curl.exe built on Schannel."""
    system_curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if system_curl.is_file():
        return str(system_curl)
    return shutil.which("curl")


def _download_with_curl(url: str, destination: Path, progress: Progress | None, total: int, timeout: float) -> None:
    """Fallback for machines whose Python cannot verify the mirror's certificate.

    A fresh Windows ships a minimal root store and downloads further roots on demand, but
    only for Schannel/CryptoAPI clients; Python's OpenSSL never triggers that, so urllib
    fails with CERTIFICATE_VERIFY_FAILED. curl.exe uses Schannel. The SHA-256 is still
    checked by the caller.
    """
    curl = _curl_executable()
    if not curl:
        raise ProvisionError("curl.exe not available")
    process = subprocess.Popen(
        [curl, "-fsSL", "--retry", "2", "--connect-timeout", str(int(timeout)), "-A", USER_AGENT,
         "-o", str(destination), url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + max(600.0, timeout * 30)
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                process.kill()
                raise ProvisionError("curl timed out")
            if progress is not None:
                try:
                    progress(destination.stat().st_size, total)
                except OSError:
                    pass
            time.sleep(0.25)
    finally:
        if process.poll() is None:
            process.kill()
    stderr = (process.stderr.read() if process.stderr else b"").decode("utf-8", "replace").strip()
    if process.stderr:
        process.stderr.close()
    if process.returncode != 0:
        raise ProvisionError(f"curl exit {process.returncode}: {stderr[:160]}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(pin: dict[str, Any], destination: Path, progress: Progress | None, opener: Callable[..., Any],
              timeout: float, fallback: Callable[..., None] | None = _download_with_curl) -> None:
    expected = str(pin["sha256"]).lower()
    total_hint = int(pin.get("size") or 0)
    errors: list[str] = []
    for url in pin["urls"]:
        host = urllib.parse.urlsplit(url).netloc or url
        done = 0
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=timeout) as response, destination.open("wb") as out:
                total = int(response.headers.get("Content-Length") or 0) or total_hint
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress is not None:
                        progress(done, total)
        except Exception as exc:  # network and TLS errors differ per machine; try curl.exe, then the next mirror
            reason = _short_reason(exc)
            if fallback is None:
                errors.append(f"{host}: {reason}")
                continue
            try:
                fallback(url, destination, progress, total_hint, timeout)
            except Exception as curl_exc:
                errors.append(f"{host}: {reason}; curl: {_short_reason(curl_exc)}")
                continue
        actual = _sha256_file(destination)
        if actual != expected:
            errors.append(f"{host}: SHA-256 mismatch ({actual[:16]}…)")
            continue
        return
    raise ProvisionError("mpv download failed: " + " | ".join(errors))


def _tar_executable() -> str:
    system_tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    return str(system_tar) if system_tar.is_file() else (shutil.which("tar") or "tar")


def _extract(archive: Path, destination: Path, files: list[str]) -> None:
    """Windows 10+ ships bsdtar (libarchive), which reads 7z."""
    done = subprocess.run(
        [_tar_executable(), "-xf", str(archive), "-C", str(destination), *files],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=300,
        check=False,
    )
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()[:300]
        raise ProvisionError(f"mpv extraction failed (tar exit {done.returncode}): {detail}")
    missing = [name for name in files if not (destination / name).is_file()]
    if missing:
        raise ProvisionError(f"mpv archive did not contain {', '.join(missing)}")


class _ProcessLock:
    """Cross-process lock so two usage-float/AgentCompass processes never download at once."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.stream: Any = None

    def __enter__(self) -> "_ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.stream.seek(0, os.SEEK_END) == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.stream.close()
                    raise ProvisionError("another process is still installing mpv")
                time.sleep(0.25)

    def __exit__(self, *_exc: Any) -> None:
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.stream.close()


_THREAD_LOCK = threading.Lock()


def ensure_mpv(
    target: Path | str | None = None,
    *,
    progress: Progress | None = None,
    force: bool = False,
    pin: dict[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    fallback: Callable[..., None] | None = _download_with_curl,
    extractor: Callable[[Path, Path, list[str]], None] | None = None,
    timeout: float = 60.0,
    lock_timeout: float = 900.0,
) -> Path:
    """Install the pinned mpv into target (once) and return the path of mpv.exe.

    Blocking; call it from a worker thread. Raises ProvisionError with a user-facing message.
    """
    target = Path(target or default_target())
    pin = pin or load_pin()
    extractor = extractor or _extract
    with _THREAD_LOCK, _ProcessLock(target.parent / ".mpv-provision.lock", lock_timeout):
        if not force and installed(target, pin):
            return target / "mpv.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".mpv-provision-", dir=str(target.parent)))
        try:
            archive = work / str(pin["archive"])
            _download(pin, archive, progress, opener, timeout, fallback)
            staged = work / "mpv"
            staged.mkdir()
            extractor(archive, staged, list(pin["files"]))
            for name in ("LICENSE.GPL", "Copyright"):
                source = LICENSE_DIR / name
                if source.is_file():
                    shutil.copyfile(source, staged / name)
            (staged / MARKER_NAME).write_text(
                json.dumps({"build": pin.get("build"), "archive": pin["archive"], "sha256": pin["sha256"],
                            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                retired = target.with_name(f".mpv-old-{uuid.uuid4().hex}")
                os.replace(target, retired)
                shutil.rmtree(retired, ignore_errors=True)
            os.replace(staged, target)
        except ProvisionError:
            raise
        except Exception as exc:
            raise ProvisionError(f"mpv install failed: {exc}") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return target / "mpv.exe"


class BackgroundProvisioner:
    """Runs ensure_mpv on a daemon thread; the Tk side only polls snapshot()."""

    def __init__(self, target: Path | None = None, runner: Callable[..., Path] = ensure_mpv) -> None:
        self.target = target
        self._runner = runner
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"status": "idle", "done": 0, "total": 0, "error": "", "path": None}
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """callback(snapshot) runs on the worker thread when it finishes; marshal to Tk with root.after."""
        with self._lock:
            self._listeners.append(callback)

    def start(self, *, retry: bool = False) -> bool:
        """Start once. Returns False while running, after success, or after a failure unless retry=True."""
        with self._lock:
            status = self._state["status"]
            if status in ("running", "done") or (status == "failed" and not retry):
                return False
            self._state = {"status": "running", "done": 0, "total": 0, "error": "", "path": None}
            self._thread = threading.Thread(target=self._run, daemon=True, name="mpv-provision")
            self._thread.start()
            return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self._state["done"] = done
            self._state["total"] = total

    def _run(self) -> None:
        try:
            path = self._runner(self.target, progress=self._progress)
            update = {"status": "done", "path": str(path), "error": ""}
        except Exception as exc:
            update = {"status": "failed", "error": str(exc) or exc.__class__.__name__}
        with self._lock:
            self._state.update(update)
            snapshot = dict(self._state)
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(snapshot)
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Install the pinned mpv build for usage-float.")
    parser.add_argument("--target", type=Path, default=None, help="default: ~/.usage-float/runtime/mpv")
    parser.add_argument("--force", action="store_true", help="download again even if this build is installed")
    args = parser.parse_args(argv)
    last = [-1]

    def report(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 0
        if percent != last[0] and percent % 10 == 0:
            last[0] = percent
            print(f"mpv: {percent}% ({done // 1048576} MiB)", flush=True)

    try:
        path = ensure_mpv(args.target, progress=report, force=args.force)
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"mpv installed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
