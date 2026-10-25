# Testing

## Layers

| Layer | Where | What it proves |
| --- | --- | --- |
| Unit + integration (pytest, 56 tests) | Linux sandbox and the Windows CI runner | parser, mapping, tools safety, tool loop, jobs, gestures, motion, config, offscreen rendering |
| `--selfcheck` from source | Windows CI runner, real desktop | real Win32 hooks/styles, real capture, full turn pipeline against a mock Worker |
| `--selfcheck` of `HeyBuddy.exe` | Windows CI runner | the PyInstaller build works the same as the source |
| Installer round trip | Windows CI runner | silent per-user install, Start menu shortcut, self-check of the *installed* exe, silent uninstall |

The mock Worker (`src/tools/selfcheck.py::_MockWorker`) speaks the real
Worker contract (`/chat` Anthropic SSE including `tool_use` rounds, `/tts`,
`/transcribe-token`), so no API keys are needed anywhere in CI.

## Self-check checks

| Check | Required | Meaning |
| --- | --- | --- |
| settings_written, monitors_enumerated, screen_capture | yes | data dir, monitors, ≤1280 px JPEGs |
| ffmpeg_mp3_decode | yes | bundled ffmpeg decodes TTS audio |
| claude_request_shape, point_resolved, spoken_text_has_no_tags, point_maps_to_screen_center | yes | legacy POINT path still exact |
| buddy_flew_to_target, overlay_window_styles | yes | flight + click-through/no-activate windows |
| buddy_rendered_on_screen | yes | real pixels on the desktop |
| overlay_excluded_from_capture, buddy_hidden_from_capture | warn | WDA_EXCLUDEFROMCAPTURE works |
| tool_round_trip | yes | tools offered, `tool_use` executed, `tool_result` sent back |
| buddy_program_beats, ink_marks_live, target_hit_test | yes | program → beats → ink; target click detection |
| ink_rendered_on_screen | yes | drawings are real pixels on the desktop |
| ink_hidden_from_capture | warn | drawings never reach the model's screenshots |
| noted_fact_saved | yes | `noted()` → memory.md |
| background_job_done | yes | job runs `write_file`, returns ARTIFACTS, file exists |
| pipeline_reached_idle_or_responding, no_pipeline_errors | yes | no errors end to end |

## Not covered automatically

* A live turn with real Anthropic, AssemblyAI and ElevenLabs keys (needs the
  user's deployed Worker). Run `--selfcheck`, then a real question, after
  deploying.
* A real human click on a `target()` box (the hit test and the follow-up
  turn are tested; the mouse itself is simulated).
* Dictation into third-party apps (paste path tested with a fake; SendInput
  is exercised only on Windows).

## Latest results

Filled in from CI for each release; see the release page for
`selfcheck.json` and the evidence PNGs.
