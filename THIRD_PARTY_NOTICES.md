# Third-party notices

HeyBuddy is MIT-licensed (see `LICENSE`). The Windows build bundles or
depends on the following open-source components. Versions are pinned in
`requirements-desktop.txt`, `requirements-dev.txt` and `worker/package.json`.

| Component | Version | License | Used for |
|---|---|---|---|
| Python | 3.12 | PSF License | runtime (embedded by PyInstaller) |
| PyQt6 / Qt 6 | 6.7.1 | GPL v3 / commercial (PyQt6), LGPL v3 (Qt) | UI, overlays, notch, painting |
| pystray | 0.19.5 | LGPL v3 | system tray icon |
| sounddevice / PortAudio | 0.5.1 | MIT / MIT | microphone and speaker audio |
| NumPy | 2.1.3 | BSD-3-Clause | audio buffers |
| mss | 9.0.2 | MIT | screen capture |
| Pillow | 11.0.0 | MIT-CMU (HPND) | image resize and JPEG encoding |
| requests | 2.32.3 | Apache-2.0 | HTTP to the Worker |
| websocket-client | 1.8.0 | Apache-2.0 | AssemblyAI realtime streaming |
| imageio-ffmpeg (ffmpeg binary) | 0.5.1 | BSD-2-Clause (wrapper); ffmpeg build is GPL v3 | decoding ElevenLabs MP3 replies |
| PyInstaller (bootloader) | 6.11.1 | GPL v2 with bootloader exception | one-file .exe |
| Inno Setup | 6.x | Inno Setup License (free, permissive) | installer (build-time only) |
| Wrangler / Cloudflare Workers types | see worker/package.json | MIT / Apache-2.0 | Worker deployment (user-side) |

## License note on PyQt6

PyQt6 is available under the GPL v3 or a commercial license. HeyBuddy is
open source, and its binary distribution is offered together with the full
source code at https://github.com/Hem456anth/HeyBuddy. If you redistribute
a modified closed-source build you need a commercial PyQt license.

## Services (not bundled)

HeyBuddy talks to Anthropic (Claude), AssemblyAI and ElevenLabs **only**
through the Cloudflare Worker you deploy with your own API keys. Their terms
of service apply to your account.

## No HeyClicky material

HeyBuddy contains no code, prompts, scripts, sounds, fonts, images, videos
or skills from HeyClicky. See `docs/HEYCLICKY_TEARDOWN.md` for the
clean-room method.
