# Roadmap to 1.0 · 与初始计划的差距

[English README](../README.md) · [中文 README](../README.zh-CN.md)

Status: 2026-11-01. **Workflow completion is not model-quality acceptance.** 0.3.0 adds bilingual use and repository packaging; it is a preview.

| Original objective / 原定目标 | Implemented / 已做 | Remaining / 未完成 |
| --- | --- | --- |
| Import scattered documents and independent audio / 杂乱资料与独立录音 | Mixed PDF/images/modern Office, no-subtitle ASR, OCR, source preservation / 混合导入、转写、OCR、保留原件 | Complex Office fidelity and diagram semantics / 复杂版式、图表语义 |
| Match content without supplied timing answers / 不依赖已知同步答案 | Content-only candidates, no page/time prior, explicit confirmation / 内容候选、人审确认 | Fresh blind matching review and measured correction time; desktop embedding retrieval / 新盲测、实际修正耗时、桌面向量检索 |
| Useful summaries with core concepts / 保住重点并归纳 | Separate origin ledgers, quote checks, topic accounting and deferrals / 分来源账本、引文校验、条目去向 | Independent-material factuality, coverage and explanation review / 独立材料忠实性、覆盖及解释评审 |
| Simple Windows use / 简单桌面使用 | ZIP/EXE, progress, Stop/continue, corrections, append, portable archive / 完整操作流程 | Clean Windows and low-end/full-classroom tests, native file-picker completion, ordinary-user trial / 干净系统、低配完整课堂、原生选文件、普通用户 |
| User-owned interchangeable models / 用户自选模型 | DeepSeek/local text; local/remote ASR and OCR, independent credentials / 已有能力适配器 | More provider protocols, online recognition quality and official subscription integrations / 更多服务与线上质量、官方订阅接入 |
| Lower API cost / 节约成本 | Stable prefixes, validated step cache, bounded requests, known/unknown usage / 缓存及预算机制 | Online cold/repeated/edited/resumed usage and bill comparison / 线上费用与命中对照 |
| English and Chinese / 中英文环境 | UI, errors, new AI prompt variants, English/Chinese archive entries and guides / 界面、提示词、归档入口、说明 | Wider bilingual materials and user validation / 更广中英材料及真实使用验证 |

## Stable release gates / 正式版验收

- [ ] Freeze new independent audio/document inputs and predictions before reading scoring materials. Review source truth separately from citation validity. / 新材料先冻结结果再评分，引用和真实性分开验收。
- [ ] Review each topic's core claims, omitted concepts, conditions, examples, source confusion and unsupported additions using the [quality rubric](归纳质量评审标准.md). Report errors explicitly, rather than hiding them in an average. / 按质量标准逐主题评审并单列错误。
- [ ] Run an ordinary user's import → organize → review → archive trial on a clean Windows machine. Record setup friction, correction time and replay/portability. / 干净 Windows 普通用户四步试用，记录实际麻烦和修正时间。
- [ ] Check a full-length class on a lower-spec device, including interruption, restart, storage needs and peak memory. / 低配完整课堂、中断恢复、磁盘及峰值内存。
- [ ] Validate real remote recognition protocols and selected text models, with actual known/unknown usage and cache comparisons. / 线上识别和文字连接逐项验证与费用对照。
- [ ] Close release-blocking data-loss, incorrect confirmation and source-attribution defects. Verify both languages and final EXE artifacts from the tagged source. / 关闭丢数据、误确认及来源混淆问题，两种语言与打包产物一致。

Provider breadth, multiple recordings, cross-platform clients and collaboration remain subsequent extensions; no universal provider/diagram-quality claim is made by changing a version number. / 更多供应商、多录音、跨平台与协作继续作为扩展，不靠修改版本号宣称全部完成。
