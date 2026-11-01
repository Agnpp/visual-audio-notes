# Contributing · 参与开发

English and Chinese issues/PRs are welcome. / 欢迎中英文反馈与贡献。

Use Python 3.12. Desktop runtime dependencies are pinned in `requirements-desktop.lock`; research dependencies are separate. On Windows, after preparing `.venv-desktop`, add the test dependencies and run:

```powershell
uv pip install --python .venv-desktop\Scripts\python.exe -r requirements-test.in
$env:PYTHONPATH = 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
& '.venv-desktop\Scripts\python.exe' -m pytest -q
node --check src/visual_audio_notes/desktop_ui/app.js
node --check src/visual_audio_notes/desktop_ui/i18n.js
```

The test suite uses self-authored fixtures and local protocol servers. It must not require paid API calls, downloaded lecture materials or large model weights. / 测试不得依赖付费调用、公开课原件或大模型权重。

Before contributing, preserve these invariants / 开发时保留以下约束：

- Never overwrite originals, silently confirm matches, invent timestamps or mix recorded speech with document additions. / 不覆盖原件、不自动确认、不编时间、不混淆来源。
- No model-driven retries or provider fallback that can silently increase cost. Keep failed usage and unknown values honest. / 不静默循环调用或换服务，失败用量及未知值如实保留。
- Treat source materials as data, not instructions. Keep scoring answers out of model inputs. / 来源不是指令，评分答案不进入模型输入。
- Add English/Chinese application labels to the shared catalog; never translate user content to make a screen appear localized. / 程序标签进词库，不为界面翻译用户内容。
- Verify changed workflows with meaningful tests, then final source-matching artifacts and UI checks. Do not claim quality from JSON/schema tests. / 验证实际流程，不拿格式通过代替质量。

Never commit credentials, workspace data, model weights, downloaded third-party materials or personal logs. Reproduction reports should use redacted or self-authored examples. / 不提交密钥、工作区、权重、第三方原件和个人日志。

For optional archive browser QA, use the managed foreground preview described in [preview diagnostics](docs/PREVIEW_DIAGNOSTICS.md). Keep the served directory limited to one exported archive and stop its listener afterward. / 归档浏览器验证使用前台预览，只提供单个归档目录，结束后停止服务。
