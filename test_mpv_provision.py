import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import mpv_provision as mp

HERE = Path(__file__).resolve().parent


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, length: bool = True) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))} if length else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Opener:
    """Serves payloads per URL; records every request."""

    def __init__(self, responses: dict, delay: float = 0.0) -> None:
        self.responses = responses
        self.delay = delay
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, request, timeout=None):
        url = request.full_url
        with self.lock:
            self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        payload = self.responses[url]
        if isinstance(payload, Exception):
            raise payload
        return _Response(payload)


def _fake_extractor(archive: Path, destination: Path, files: list[str]) -> None:
    for name in files:
        (destination / name).write_bytes(b"extracted:" + name.encode())


def _pin(payload: bytes, urls=("https://a.example/mpv.7z", "https://b.example/mpv.7z")) -> dict:
    return {
        "build": "test",
        "archive": "mpv-test.7z",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "urls": list(urls),
        "files": ["mpv.exe", "d3dcompiler_43.dll"],
    }


class EnsureMpvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = self.root / "runtime" / "mpv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in (self.root / "runtime").iterdir() if p.name.startswith(".mpv-") and p.name != ".mpv-provision.lock")

    def test_verified_download_installs_files_licenses_and_marker(self) -> None:
        payload = b"archive-bytes" * 1000
        pin = _pin(payload)
        opener = _Opener({pin["urls"][0]: payload})
        progress: list[tuple[int, int]] = []

        exe = mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor,
                            progress=lambda done, total: progress.append((done, total)))

        self.assertEqual(exe, self.target / "mpv.exe")
        self.assertEqual((self.target / "mpv.exe").read_bytes(), b"extracted:mpv.exe")
        self.assertTrue((self.target / "d3dcompiler_43.dll").is_file())
        self.assertEqual((self.target / "LICENSE.GPL").read_bytes(), (HERE / "licenses" / "mpv" / "LICENSE.GPL").read_bytes())
        self.assertTrue((self.target / "Copyright").is_file())
        marker = json.loads((self.target / mp.MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(marker["sha256"], pin["sha256"])
        self.assertTrue(mp.installed(self.target, pin))
        self.assertEqual(progress[-1], (len(payload), len(payload)))
        self.assertEqual(opener.calls, [pin["urls"][0]])
        self.assertEqual(self.leftovers(), [])

    def test_checksum_mismatch_on_every_mirror_fails_and_installs_nothing(self) -> None:
        pin = _pin(b"the real archive")
        opener = _Opener({url: b"tampered or an HTML error page" for url in pin["urls"]})

        with self.assertRaises(mp.ProvisionError) as caught:
            mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor)

        self.assertIn("SHA-256 mismatch", str(caught.exception))
        self.assertFalse(self.target.exists())
        self.assertFalse(mp.installed(self.target, pin))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(opener.calls, pin["urls"])

    def test_failed_mirror_falls_back_to_the_next_one(self) -> None:
        payload = b"good"
        pin = _pin(payload)
        opener = _Opener({pin["urls"][0]: OSError("connection reset"), pin["urls"][1]: payload})

        mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor, fallback=None)

        self.assertTrue(mp.installed(self.target, pin))
        self.assertEqual(opener.calls, pin["urls"])

    def test_network_failure_message_names_each_mirror(self) -> None:
        pin = _pin(b"x")
        opener = _Opener({url: OSError("getaddrinfo failed") for url in pin["urls"]})

        with self.assertRaises(mp.ProvisionError) as caught:
            mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor, fallback=None)

        for host in ("a.example", "b.example"):
            self.assertIn(host, str(caught.exception))
        self.assertIn("getaddrinfo failed", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_tls_failure_falls_back_to_curl_and_still_checks_the_hash(self) -> None:
        """Fresh Windows: Python cannot verify the mirror's chain, Schannel curl.exe can."""
        import ssl
        import urllib.error

        payload = b"archive via curl"
        pin = _pin(payload)
        tls = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate"))
        opener = _Opener({url: tls for url in pin["urls"]})
        fetched: list[str] = []

        def curl(url, destination, progress, total, timeout):
            fetched.append(url)
            destination.write_bytes(payload)

        mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor, fallback=curl)
        self.assertTrue(mp.installed(self.target, pin))
        self.assertEqual(fetched, [pin["urls"][0]])

        bad_target = self.root / "runtime" / "other"

        def tampered(url, destination, progress, total, timeout):
            destination.write_bytes(b"not the pinned archive")

        with self.assertRaises(mp.ProvisionError) as caught:
            mp.ensure_mpv(bad_target, pin=pin, opener=opener, extractor=_fake_extractor, fallback=tampered)
        self.assertIn("SHA-256 mismatch", str(caught.exception))
        self.assertFalse(bad_target.exists())

    def test_tls_and_curl_failures_are_both_reported_briefly(self) -> None:
        pin = _pin(b"x")
        opener = _Opener({url: OSError("certificate verify failed") for url in pin["urls"]})

        def curl(url, destination, progress, total, timeout):
            raise mp.ProvisionError("curl exit 6: Could not resolve host")

        with self.assertRaises(mp.ProvisionError) as caught:
            mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor, fallback=curl)
        message = str(caught.exception)
        self.assertIn("certificate verify failed", message)
        self.assertIn("curl exit 6", message)
        self.assertLess(len(message), 600)

    def test_extraction_failure_leaves_no_partial_install(self) -> None:
        payload = b"good"
        pin = _pin(payload)

        def broken(archive, destination, files):
            raise mp.ProvisionError("mpv extraction failed (tar exit 1): bad archive")

        with self.assertRaises(mp.ProvisionError):
            mp.ensure_mpv(self.target, pin=pin, opener=_Opener({pin["urls"][0]: payload}), extractor=broken)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leftovers(), [])

    def test_second_call_does_not_download_again(self) -> None:
        payload = b"good"
        pin = _pin(payload)
        opener = _Opener({pin["urls"][0]: payload})

        mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor)
        mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor)

        self.assertEqual(len(opener.calls), 1)

    def test_concurrent_callers_download_once(self) -> None:
        payload = b"good"
        pin = _pin(payload)
        opener = _Opener({pin["urls"][0]: payload}, delay=0.3)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                mp.ensure_mpv(self.target, pin=pin, opener=opener, extractor=_fake_extractor)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(errors, [])
        self.assertEqual(len(opener.calls), 1)

    def test_an_older_provisioned_build_is_replaced(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "mpv.exe").write_bytes(b"old")
        (self.target / "d3dcompiler_43.dll").write_bytes(b"old")
        (self.target / mp.MARKER_NAME).write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
        payload = b"new"
        pin = _pin(payload)
        self.assertFalse(mp.installed(self.target, pin))

        mp.ensure_mpv(self.target, pin=pin, opener=_Opener({pin["urls"][0]: payload}), extractor=_fake_extractor)

        self.assertEqual((self.target / "mpv.exe").read_bytes(), b"extracted:mpv.exe")
        self.assertTrue(mp.installed(self.target, pin))
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(sys.platform == "win32" and Path(r"C:\Windows\System32\tar.exe").is_file(), "needs Windows bsdtar")
    def test_real_tar_extracts_only_the_listed_files_from_a_7z(self) -> None:
        src = self.root / "src"
        src.mkdir()
        for name in ("mpv.exe", "d3dcompiler_43.dll", "updater.bat"):
            (src / name).write_bytes(name.encode())
        archive = self.root / "fixture.7z"
        subprocess.run([mp._tar_executable(), "--format", "7zip", "-cf", str(archive), "mpv.exe", "d3dcompiler_43.dll",
                        "updater.bat"], cwd=src, check=True, capture_output=True)
        payload = archive.read_bytes()
        pin = _pin(payload)

        mp.ensure_mpv(self.target, pin=pin, opener=_Opener({pin["urls"][0]: payload}))

        self.assertEqual((self.target / "mpv.exe").read_bytes(), b"mpv.exe")
        self.assertFalse((self.target / "updater.bat").exists())


class BackgroundProvisionerTests(unittest.TestCase):
    def test_start_returns_at_once_and_does_not_double_start(self) -> None:
        release = threading.Event()
        calls: list[Path | None] = []

        def runner(target, progress=None):
            calls.append(target)
            progress(5, 10)
            release.wait(5)
            return Path("mpv.exe")

        provisioner = mp.BackgroundProvisioner(target=Path("t"), runner=runner)
        began = time.monotonic()
        self.assertTrue(provisioner.start())
        self.assertLess(time.monotonic() - began, 0.2, "start() must not block the Tk thread")
        self.assertFalse(provisioner.start(), "a second start while running must be ignored")
        deadline = time.monotonic() + 2
        while provisioner.snapshot()["done"] != 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = provisioner.snapshot()
        self.assertEqual((snapshot["status"], snapshot["done"], snapshot["total"]), ("running", 5, 10))
        release.set()
        provisioner.join(5)
        self.assertEqual(provisioner.snapshot()["status"], "done")
        self.assertFalse(provisioner.start(), "nothing to do after success")
        self.assertEqual(len(calls), 1)

    def test_failure_is_reported_and_only_retried_on_request(self) -> None:
        attempts: list[int] = []

        def runner(target, progress=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise mp.ProvisionError("mpv download failed: offline")
            return Path("mpv.exe")

        seen: list[dict] = []
        provisioner = mp.BackgroundProvisioner(runner=runner)
        provisioner.add_listener(seen.append)
        provisioner.start()
        provisioner.join(5)
        self.assertEqual(provisioner.snapshot()["status"], "failed")
        self.assertIn("offline", provisioner.snapshot()["error"])
        self.assertFalse(provisioner.start(), "a failure is not retried behind the user's back")
        self.assertTrue(provisioner.start(retry=True))
        provisioner.join(5)
        self.assertEqual(provisioner.snapshot()["status"], "done")
        self.assertEqual([s["status"] for s in seen], ["failed", "done"])


class PinTests(unittest.TestCase):
    def test_pin_is_complete_and_shared_with_install_script(self) -> None:
        pin = mp.load_pin()
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(all(url.startswith("https://") for url in pin["urls"]))
        self.assertIn(pin["archive"], pin["urls"][0])
        self.assertEqual(pin["files"], ["mpv.exe", "d3dcompiler_43.dll"])
        script = (HERE / "install-mpv.ps1").read_text(encoding="utf-8")
        self.assertIn("mpv-pin.json", script)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", script), "the hash belongs in mpv-pin.json only")
        notices = (HERE / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn(pin["sha256"], notices)
        self.assertIn(pin["archive"], notices)

    def test_license_texts_ship_with_the_repo(self) -> None:
        self.assertIn("GNU GENERAL PUBLIC LICENSE", (HERE / "licenses" / "mpv" / "LICENSE.GPL").read_text(encoding="utf-8"))
        self.assertIn("mpv", (HERE / "licenses" / "mpv" / "Copyright").read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def test_cli_reports_failure_with_exit_code(self) -> None:
        with TemporaryDirectory() as tmp:
            original = mp.ensure_mpv

            def failing(*_a, **_k):
                raise mp.ProvisionError("mpv download failed: offline")

            mp.ensure_mpv = failing
            try:
                self.assertEqual(mp.main(["--target", tmp]), 1)
            finally:
                mp.ensure_mpv = original


if __name__ == "__main__":
    unittest.main()
