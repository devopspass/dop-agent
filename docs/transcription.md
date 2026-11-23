# Voice transcription

## Voice Transcription

Send a voice message or an audio file attachment (DM or `@mention`) and the bot transcribes it **locally** — no audio ever leaves the machine except to download it from Slack.

**Pipeline:**
1. The Slack `message` event carries a `subtype: "file_share"` with a `files[]` array; the bot detects audio by mimetype (`audio/*`), Slack's `slack_audio` subtype, or common audio file extensions (m4a, mp3, wav, ogg, webm, mp4, aac, flac).
2. The file is downloaded via its `url_private_download` URL, authenticated with the bot token (requires the `files:read` scope).
3. [`openai-whisper`](https://github.com/openai/whisper) — OpenAI's original local Whisper implementation (PyTorch) — transcribes the audio on-device.
4. A `🎧 Transcribing...` placeholder message is posted immediately, with a button underneath to flip the default behavior for *this* message (see below).
5. **Multiple voice messages in a row**: if you send several voice messages before the transcript is forwarded/before clicking the button, each is transcribed and the running transcript is merged into the *latest* message (the button is stripped from earlier ones so only the final, up-to-date combined text has an active button, if any). Whisper's `model.transcribe()` is **not safe to run concurrently** on the same model instance, so all transcriptions across every thread are serialized through a single `asyncio.Lock` (a FIFO queue): if a second voice message arrives while the first is still transcribing, its placeholder switches to `⏳ Queued — waiting for another transcription to finish...` and it's picked up automatically once the lock frees up, instead of racing the first transcription and silently failing to ever produce text.
6. Once transcription finishes, the merged text (`🎤 <text>`) is either **auto-forwarded** to the Copilot session immediately (as if you'd typed it — the response is posted back in the same channel/thread), or shown with an **"➡️ Send to agent"** button for you to click manually, depending on the effective mode below.

**Auto-send vs. preview — controlled by `AUDIO_AUTO_SEND` env var:**
- `AUDIO_AUTO_SEND=true` (**default**): transcripts are sent to Copilot automatically the moment they're ready. The `🎧 Transcribing...` placeholder shows a **"👁️ Preview before sending"** button — click it *while still transcribing* to opt that one message out of auto-send; it will instead land with the manual "➡️ Send to agent" button so you can review/edit context before forwarding.
- `AUDIO_AUTO_SEND=false`: transcripts wait for a manual "➡️ Send to agent" click. The placeholder instead shows an **"⚡ Auto-send when ready"** button to opt that one message *into* auto-send.
- The button only has an effect if clicked before transcription finishes; once the final message is posted the button (if any) is single-use, same as before.

**Durable recordings (never lost on restart).** Every recording — Web UI mic or Slack voice note — is written to `~/.dop-agent/audio/<conversation_id>/<timestamp>-<hex>.<ext>` (dirs `0700`, files `0600`) *before* any processing, and Whisper runs from that file. A sidecar `<file>.json` tracks `uploading → pending → transcribing → transcribed → done | failed`, attempts, per-segment texts, the transcript and a `delivered` flag. On startup (and every minute) `handlers.audio_maintenance_loop` resumes unfinished jobs: a stored transcript is re-delivered without re-running Whisper, finished segments are skipped, a missing placeholder is re-posted, and a job that already started 3 times is marked `failed` (audio kept; failed jobs get one automatic retry after the next restart, or `POST /api/sessions/<id>/audio-retry`). Uploads with unknown duration (browser webm) are always segmented. Audio is only deleted after `done`+`delivered`, 7 days later (`AUDIO_RETENTION_DAYS`). Uploads stream to disk (cap `AUDIO_MAX_BYTES`, default 2 GB → HTTP 413); audio over 10 min is cut by ffmpeg into 10-min 16 kHz wav segments that each take the shared Whisper lock separately (placeholder shows `segment k/n`; a word at a cut can occasionally be clipped). The Web UI uploads MediaRecorder chunks every 15 s (`POST .../audio-upload`, `PUT .../chunk?seq=N`, `POST .../finish`) so a closed tab keeps what was captured — an upload idle for ~90 s is promoted to a normal job automatically; if chunking fails the browser falls back to a one-shot upload.

**Model**: controlled by the `WHISPER_MODEL` env var (default `large` — best accuracy, ~3GB download, ~7s to transcribe a short clip; other options: `tiny`, `base`, `small`, `medium`, `turbo`, or `.en` variants). The model weights are downloaded from OpenAI's official Azure CDN (`openaipublic.azureedge.net`) and cached under `~/.cache/whisper` on first use — **requires internet access the first time only**; after that it runs fully offline. Note: this is a different (and more firewall-friendly) download host than HuggingFace, which some corporate networks block by SNI.

**Dependencies**: `openai-whisper` (pure PyTorch — works on any macOS, Apple Silicon or Intel) and the `ffmpeg` CLI (used to decode audio). Install with:
```bash
pip install openai-whisper
brew install ffmpeg
```

**Slack scope**: requires `files:read` (see [Required OAuth Scopes](#required-oauth-scopes-bot-token)) — reinstall the app to Workspace after adding it.

**Web UI parity**: the same pipeline is available from the Web UI, not just
Slack. This is implemented at the `Adapter` level (`adapters.py`'s
`SlackAdapter`/`WebAdapter`) rather than duplicated per-platform — the core
`transcribe_audio_bytes()` (in `transcription.py`) and the
`_arm_transcript_mode()`/placeholder/button-rewrite logic in `handlers.py`
are shared verbatim between Slack and Web; only how the message gets
delivered to the user differs.
- **Mic button**: click the 🎙️ button next to Send in an **already-open**
  session (starting a brand-new conversation purely from a voice recording
  isn't supported yet — start it with a short text message first) to record
  via the browser's `MediaRecorder` API, then click again to stop and
  upload. The clip is POSTed to
  `POST /api/sessions/{conversation_id}/transcribe` and runs through the
  exact same Whisper pipeline/placeholder/auto-send logic as a Slack voice
  message.
- **Transcript action buttons**: the `🎧 Transcribing...`/`➡️ Send to
  Copilot`/`⚡ Auto-send when ready`/`👁️ Preview before sending` buttons
  Slack shows via block-kit are rendered as real inline buttons in the Web
  UI too (driven by each message's `actions` field, stored in
  `message_store.py`'s `actions` column), wired to
  `POST /api/sessions/{conversation_id}/messages/{message_id}/transcript-action`.
- Uploads are capped the same way as Slack (`transcription.MAX_AUDIO_BYTES`)
  — oversized clips get a `413` instead of silently hanging.
