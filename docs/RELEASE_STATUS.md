# Release status · 发布验收

Date / 日期: 2026-11-01. Version / 版本: **0.3.1 preview**.

0.3.1 resolves the archive browser-test gap and fixes a Windows deep AI-cache path error found during that check. It remains a bilingual preview; original quality and ordinary-user acceptance gates are still open. / 已补齐归档浏览器验证，修复补测发现的 Windows 深路径 AI 缓存问题；仍为双语预览版。

## Validation records / 验证记录

- The preceding 0.2.0 baseline passed 588 tests and actual frozen EXE recognition/append/correction/archive/restart checks. / 前版已验证核心流程，不把结果当模型准确率。
- 0.3.0 adds a shared application-label catalog, remembered language, independent English prompts and an English offline reader. Tests check preservation of original content, explicit matches, language-specific cache reuse and English/Chinese API routing. / 新增本地化及不改用户内容、语言缓存、归档等回归。
- Real browser checks cover English import, source correction, manual notes, explicit confirmation, archive export and switching back to Chinese. Original Chinese text remains unchanged. / 浏览器实际验证两种语言操作及原文保留。
- Final 0.3.1 source suite: **598 passed in 92.82 seconds**; both JavaScript entry files passed syntax checks. / 新增两项深路径缓存回归，全量及 JS 语法通过。
- Full frozen EXE: independent 30-second audio without subtitles → 10 segments + one OCR page → add PPTX → correct transcript → English reader and original/revised VTT archive → restart; four original hashes unchanged. The run took 6.070 seconds on the development machine, not a full-classroom performance benchmark. / 完整识别版实际 EXE 闭环通过，不泛化为完整课堂性能。
- API frozen EXE: the same workflow used local speech/vision protocol fixtures, including a 231-character cache path and separate audio-duration/text-token usage. It is not an online accuracy score. / API 实际 EXE 的本机协议回归通过。
- Both bundles match 55 current source/UI/prompt resources, provide English/Chinese guides, exclude large speech weights, and retain third-party licenses. / 两包源码资源一致、双语说明及许可完整，不含大语音权重。
- Archive browser interaction is now **passed over loopback HTTP**: English/Chinese labels, separate recorded/document sections, original evidence, document previews, original-file links, time seeking, actual playback and explicit/unconfirmed-match messages. A 30-second recording sought to 10 seconds and played forward without a media error; the English 1024×700 reader had no horizontal overflow. The only console failures were missing `favicon.ico` requests. Managed-browser direct `file:` navigation is still unverified. / 本机 HTTP 浏览器交互已补测通过；直接文件导航仍不计为通过，缺图标不作为脚本或播放故障。
- Both actual 0.3.1 EXEs completed AI protocol-fixture tasks with **283-character cache lock paths**. Each made four local fixture requests on the first run and **zero new requests** on the second run. Originals were unchanged; recorded speech and document additions stayed separate. These are protocol/cache tests, not model-quality results. / 两包实际 EXE 深路径整理及缓存复用通过，不以协议夹具证明归纳质量。

The earlier launch failed before process creation with `blocked by policy`; no exact rationale was available. “Automatic approval rejection” was an over-specific attribution. A managed foreground preview succeeded without changing permissions. See [diagnosis and reproducible commands](PREVIEW_DIAGNOSTICS.md). / 已纠正拒绝归因，前台受管预览可用，无需修改全局权限。

| Artifact / 产物 | ZIP bytes | SHA256 |
| --- | --- | --- |
| Full / 完整版 | 174,477,537 | `b60ea459f5e28c6a3ad3c108362fb5be843b38dad7a9facb45e4d3e23da80f73` |
| API | 67,807,015 | `b1164d3c38c42ebb7f9aa799fe31ba348e7d3d3c8d9b8d7078f7ef56241b0e51` |

The prior [0.3.0 Windows CI](https://github.com/Agnpp/visual-audio-notes/actions/runs/41285621486) passed 596 tests. Current source CI is recorded in the 0.3.1 release verification after the new run completes. Both 0.3.1 ZIPs were rebuilt and rechecked against the current 55 bundled source/UI/prompt resources. / 前版 CI 为历史记录，本次独立 CI 另附发布记录；两包已重建并核对资源。

No new paid API quality test or fresh blind-evaluation score is claimed by this release. The earlier real OCR smoke check retained `API → APl`; only two of its three exact text lines passed. Candidate coverage was not an accuracy score. / 本轮不产生新的线上质量或盲测成绩，保留前次OCR错字及评估边界。

Clean Windows/low-end/full-classroom checks, native picker completion, independent user trials, more provider compatibility, diagram semantics and measured currency savings remain in the [1.0 checklist](ROADMAP.md). / 剩余工作见正式版清单。
