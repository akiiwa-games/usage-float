# Third-party software

## mpv

Usage Float uses `mpv.exe` as an external media playback process for the
dynamic-wallpaper mode. Video decoding, image display, playlist looping, GPU
rendering, and Windows `HWND` embedding are provided by mpv; Usage Float does
not implement its own media decoder.

- Project: https://github.com/mpv-player/mpv
- Windows build: https://github.com/shinchiro/mpv-winbuild-cmake/releases/tag/20260814
- Pinned archive: `mpv-x86_64-20260814-git-7b8915bc1d.7z`
- SHA-256: `1bf3b029da2c98e605e00e85f21ee3142f22a1dcc4ceb5c827b5c51e36e390f9`
- License: GPL-2.0-or-later by default; see `vendor/mpv/LICENSE.GPL` and
  `vendor/mpv/Copyright` after running `install-mpv.ps1`.

The architecture follows Lively Wallpaper's documented choice of mpv as its
default, recommended video-wallpaper backend:
https://github.com/rocksdanister/lively/wiki/Video-Guide
