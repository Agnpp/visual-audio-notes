# Release status · 发布验收

Date / 日期: 2026-11-01. Version / 版本: **0.3.0 preview**.

0.3.0 follows the bilingual-preview route. It is not a claim that the original quality and ordinary-user acceptance plan is complete. / 本轮选择中英文预览路线，不宣称初始质量与普通用户验收已全部完成。

## Validation records / 验证记录

- The preceding 0.2.0 baseline passed 588 tests and actual frozen EXE recognition/append/correction/archive/restart checks. / 前版已验证核心流程，不把结果当模型准确率。
- 0.3.0 adds a shared application-label catalog, remembered language, independent English prompts and an English offline reader. Tests check preservation of original content, explicit matches, language-specific cache reuse and English/Chinese API routing. / 新增本地化及不改用户内容、语言缓存、归档等回归。
- Real browser checks cover English import, source correction, manual notes, explicit confirmation, archive export and switching back to Chinese. Original Chinese text remains unchanged. / 浏览器实际验证两种语言操作及原文保留。
- Final source suite: **596 passed in 48.58 seconds**; both JavaScript entry files passed syntax checks. / 最终源码测试及 JS 语法通过。
- Full frozen EXE: independent 30-second audio without subtitles → 10 segments + one OCR page → add PPTX → correct transcript → English reader and original/revised VTT archive → restart; four original hashes unchanged. The run took 5.923 seconds on the development machine, not a full-classroom performance benchmark. / 完整识别版实际 EXE 闭环通过，不泛化为完整课堂性能。
- API frozen EXE: the same workflow used local speech/vision protocol fixtures, including a 231-character cache path and separate audio-duration/text-token usage. It is not an online accuracy score. / API 实际 EXE 的本机协议回归通过。
- Both bundles match 55 current source/UI/prompt resources, provide English/Chinese guides, exclude large speech weights, and retain third-party licenses. / 两包源码资源一致、双语说明及许可完整，不含大语音权重。
- Browser checks validate the desktop UI. Direct file navigation and a local archive preview were blocked by the automation environment; English archive browser interaction is **not** counted as passed. / 浏览器验证仅涵盖桌面界面，英文归档的浏览器交互未计为通过。

| Artifact / 产物 | ZIP bytes | SHA256 |
| --- | --- | --- |
| Full / 完整版 | 174,477,984 | `010c30c76ab1fbfeb7b0a386cb786676048739896506c021aa38c27b6a8f3748` |
| API | 67,805,188 | `0787aafa77ef91c0c2077bbeb51569b16d8de5882086d6babbd07abf014a6796` |

Independent [Windows source CI](https://github.com/Agnpp/visual-audio-notes/actions/runs/41285621486) passed. The first run exposed missing optional research-package metadata, a missing protocol document and a test-only resource-monitor dependency. These were fixed without skipping tests or adding large research model libraries to the desktop runtime. Both ZIPs were rebuilt and rechecked afterward. / 独立 Windows CI 已通过；首次暴露的源码分发问题已修复，未跳过测试，未把大模型研究库塞进桌面依赖，两包已重新构建。

No new paid API quality test or fresh blind-evaluation score is claimed by this release. The earlier real OCR smoke check retained `API → APl`; only two of its three exact text lines passed. Candidate coverage was not an accuracy score. / 本轮不产生新的线上质量或盲测成绩，保留前次OCR错字及评估边界。

Clean Windows/low-end/full-classroom checks, native picker completion, independent user trials, more provider compatibility, diagram semantics and measured currency savings remain in the [1.0 checklist](ROADMAP.md). / 剩余工作见正式版清单。
