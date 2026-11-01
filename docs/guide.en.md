# Quick start · English

[简体中文](guide.zh-CN.md) · [Project](../README.md)

## Open the app

1. Extract the entire Windows x64 ZIP. Double-click **VisualAudioNotes.exe**, leaving `_internal` beside it.
2. Select **English** in the top bar. Language is saved on this computer; source files and existing notes are never translated by switching it.
3. Click **Import materials**, selecting documents, images and one recording together. SRT/VTT is optional. Add later files to the same project with **Add materials**.
4. Click **Organize**, review the results, then **Save archive**.

No developer tools are required. The app uses the system's Microsoft Edge WebView2 Runtime and .NET Framework 4.x. If WebView2 is missing, install it from the [Microsoft page](https://developer.microsoft.com/microsoft-edge/webview2/). The preview is unsigned; do not disable security software to run it.

## Configure once

The settings dialog has three independent capability tabs:

- **Speech recognition:** Full edition includes a local Whisper engine. Click **Prepare local speech model** to download about **486 MB**, with progress, checksum validation, resume and Stop. Nothing downloads just because the app starts. Then local transcription can run offline. Alternatively supply your compatible speech API endpoint, model name and key. It must support Whisper-style segment timestamps; a chat model alone is insufficient. Choose English, Chinese or automatic language detection.
- **Image OCR:** Full edition includes local Chinese/English RapidOCR. Only pages without native text are recognized. Alternatively configure the DeepSeek vision OCR adapter. OCR reads text; it does not guarantee interpretation of diagrams or flows.
- **Key point summary:** Supply a DeepSeek text API connection or your local llama.cpp service. DeepSeek defaults to thinking off. Text summarization is optional; recognition and manual archives work without it. New AI drafts use the selected interface language, retaining original quotations and code.

The API edition excludes local recognition engines. Use your speech and image endpoints or download the full edition. Each capability has its own key. A text key is not silently sent to a speech endpoint. Keys are session-only unless you explicitly choose Windows user-scoped secure storage.

## Review and keep your work

- Preview a suggested page without confirming it. **Confirm match** or **Confirm no match** records your decision. No page matching is automatically confirmed.
- **Correct this transcript segment** and **Correct extracted text** retain originals and raw recognition records. Changed content makes previous matches and topic drafts require review. Valid unchanged model steps can be reused.
- Save or discard a text correction before switching language, adding files or archiving. Stop a running task before changing language or connections.
- **Stop processing** prevents subsequent requests. An active request cannot be revoked and may still be billed. Continue later to reuse completed results.
- In English archives open **index.html** or **notes.md**. Keep the entire folder to preserve media, relative links, source hashes, original/revised subtitles, evidence and usage records. Original-format records and the compatible Chinese reader are retained alongside the English entry. Existing model drafts are not automatically translated on export.

## Supported files and limits

PDF, PNG/JPG, PPTX, DOCX, XLSX, VSDX, WAV/MP3/M4A, SRT/VTT. Each project supports one original recording and one user subtitle file. A later user subtitle can replace an automatic transcript while retaining the latter in history. Same-name/different-content additions are rejected without overwriting originals.

Modern Office text and supported images are read directly. Built-in previews are reading views, not a complete Office layout engine. DOCX preview pages are not Word pages; spreadsheet formulas are not recalculated; Visio connector endpoints do not establish arrow meaning. Legacy `.ppt/.doc/.xls/.vsd` requires an installed converter.

Data defaults to `%LOCALAPPDATA%\VisualAudioNotes`. Original import files are never modified. Deleting the extracted app folder does not delete the workspace; export what you need before removing workspace data.

Validated cache reuse, stable prefixes and bounded requests reduce unnecessary calls. There is a shared ceiling of 32 new recognition/summary requests and a requested-output budget per run. Reported duration, text tokens and unknown usage stay distinct. Actual charges follow your provider; currency savings have not yet been benchmarked.

This is a preview. Follow the [1.0 checklist](ROADMAP.md) for outstanding quality and ordinary-user validation.
