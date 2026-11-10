# Third-party software

## mpv

Usage Float uses `mpv.exe` as an external media playback process for the
dynamic-wallpaper mode. Video decoding, image display, playlist looping, GPU
rendering, and Windows `HWND` embedding are provided by mpv; Usage Float does
not implement its own media decoder.

- Project: https://github.com/mpv-player/mpv
- Windows build: shinchiro/mpv-winbuild-cmake weekly build `20260809`, mirrored at
  https://sourceforge.net/projects/mpv-player-windows/files/64bit/
- Pinned archive: `mpv-x86_64-20260809-git-dd5d17d328.7z` (mpv commit `dd5d17d328`)
- SHA-256: `fa0dd286b24e9c927203b2a5eaa2e1948891d69eb22e9826120d4ce7fc2bc4c4`
- The pin lives in `mpv-pin.json`. The source repository does not contain the
  binary: Usage Float downloads and verifies it into `~/.usage-float/runtime/mpv`
  the first time a wallpaper needs it (`mpv_provision.py`), or `install-mpv.ps1`
  puts it in `vendor/mpv`.
- License: GPL-2.0-or-later by default; see `licenses/mpv/LICENSE.GPL` and
  `licenses/mpv/Copyright` (copied next to `mpv.exe` on install).

The architecture follows Lively Wallpaper's documented choice of mpv as its
default, recommended video-wallpaper backend:
https://github.com/rocksdanister/lively/wiki/Video-Guide
