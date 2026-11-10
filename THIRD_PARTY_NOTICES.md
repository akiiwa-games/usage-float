# Third-party software

## mpv

Usage Float uses `mpv.exe` as an external media playback process for the
dynamic-wallpaper mode. Video decoding, image display, playlist looping, GPU
rendering, and Windows `HWND` embedding are provided by mpv; Usage Float does
not implement its own media decoder.

- Project: https://github.com/mpv-player/mpv
- Windows build: shinchiro/mpv-winbuild-cmake release `20261108`
  (https://github.com/shinchiro/mpv-winbuild-cmake/releases/tag/20261108, build
  commit `bda10a2816ce9908d419b3f4b564227330457e76`)
- mpv source: https://github.com/mpv-player/mpv/tree/55d3feb45c3dda1afd02b3087d3463c57d6514f7
- Download sources, in order (each verified against its own SHA-256; both pinned in `mpv-pin.json`):
  1. `mpv-x86_64-20261108-git-55d3feb45c-slim.7z` from the `mpv-20261108` release of
     this repository: the upstream `mpv.exe`, byte for byte, plus `LICENSE.GPL` and
     `Copyright`. SHA-256 `8f8d71974ad9aa2c885eb56afa1b423135dc98bec478d71c3d62aa86798596b0`.
  2. The original archive `mpv-x86_64-20261108-git-55d3feb45c.7z` from the SourceForge
     mirror. SHA-256 `465faafbf90527421ccc304870f02e59f3f47d3361a6f526ef95f40f8f2c2459`
     (the digest GitHub publishes for the upstream asset).
- Only `mpv.exe` is installed; Windows 10 and later provide the D3D shader compiler
  (`d3dcompiler_47.dll`) it loads.
- `mpv.exe` statically links FFmpeg, libplacebo and the other libraries of the build;
  the `mpv-20261108` release carries the corresponding source of every one of them,
  each pinned to the revision used, with the build scripts.
- The source repository does not contain the binary: Usage Float downloads and
  verifies it into `~/.usage-float/runtime/mpv` the first time a wallpaper needs it
  (`mpv_provision.py`), or `install-mpv.ps1` puts it in `vendor/mpv`.
- License: GPL-2.0-or-later by default; see `licenses/mpv/LICENSE.GPL` and
  `licenses/mpv/Copyright` (copied next to `mpv.exe` on install).

The architecture follows Lively Wallpaper's documented choice of mpv as its
default, recommended video-wallpaper backend:
https://github.com/rocksdanister/lively/wiki/Video-Guide
