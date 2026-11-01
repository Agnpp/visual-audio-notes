# Archive preview diagnosis · 归档预览排查

Date / 日期: 2026-11-01. Applies to / 适用于: the 0.3.0 browser-test gap and the 0.3.1 patch.

## Confirmed cause boundary / 已确认的原因范围

The failed command combined `Start-Process`, writing a PID file and opening a Playwright tab. The execution tool rejected it **before creating a process**, returning only `CreateProcess ... Rejected ... blocked by policy`. There was no Python traceback, HTTP response or reviewer rationale. The recorded turn used `approval_policy = "never"` and `danger-full-access`; no matching deny rule was found in the local command rules. The precise internal rule or classifier decision is unavailable.

上次命令把后台启动、写 PID 与打开浏览器放在一起，创建进程前就被执行策略拒绝。日志只有 `blocked by policy`，不能据此判断为程序崩溃、端口冲突、Windows 防火墙或某个具体审批规则。也没有证据证明 `Start-Process` 本身被禁止。

The earlier description “automatic approval rejected it” was too specific. [Official OpenAI documentation](https://learn.chatgpt.com/docs/sandboxing/auto-review) distinguishes Auto-review from other execution controls and says that `approval_policy = "never"` does not create an interactive Auto-review request. The accurate incident description is **execution-policy rejection without a recorded rationale**. / 原先“自动审批拒绝”的说法过于确定，应更正为“执行策略拒绝，未记录具体原因”。

## Reproducible preview / 可复用的预览方式

Run the existing helper directly in a managed foreground terminal, from the repository root. Pass the **single exported archive directory** being checked. Start the browser separately after the ready URL is printed. / 在项目根目录直接运行现有脚本，目录限定到本次导出的归档；看到地址后再单独打开浏览器。

```powershell
& '.venv-desktop\Scripts\python.exe' -u 'tools\preview_archive.py' --directory 'C:\path\to\one-exported-archive' --port 38830
```

Open `http://127.0.0.1:38830/index.html` for English or `http://127.0.0.1:38830/阅读.html` for Chinese. The helper binds only `127.0.0.1`, serves files without modifying them and supports audio byte ranges. Press Ctrl+C in the owning terminal when finished, then verify that the listener has stopped. This needs no global permission, firewall or Windows policy change. / 服务仅监听本机、不修改归档，支持音频跳转；结束时 Ctrl+C 并确认端口释放，不需要修改全局权限或防火墙。

The QA helper now sends `Cache-Control: no-store`. When diagnosing a previously cached URL, a fresh browser context or a unique query string avoids confusing a previous archive with the current one. / 预览服务增加不缓存响应，避免复用端口时误看上一个归档；已有浏览器缓存用新会话或不同查询参数区分。

This is an optional developer QA step. Normal users can open the exported HTML directly; they do not need Python or a preview service. The managed browser's direct `file:` route remains unverified here. / 这是开发者可选验证步骤，普通用户仍可直接打开归档 HTML，无需安装 Python 或启动服务；本轮不把受管浏览器的 `file:` 路径计为通过。

## Additional defect found / 补测发现的独立问题

A deeper test workspace reproduced a separate Windows path-length defect: AI cache files use a full hash and a `.json.lock` sibling, which can exceed 260 characters even when the project directory is valid. Evidence extraction failed before a model request, and the generic task error could be mistaken for a connection/key failure. This was **not** the cause of the earlier policy rejection.

较深目录会使 AI 缓存锁文件超过 Windows 传统路径长度，模型尚未请求就失败。0.3.1 在缓存内部文件操作使用 Windows 扩展路径，保持缓存所在位置、完整哈希、原件及跨进程锁；不改注册表，不自动重试付费请求。

Regression tests exercise fresh extraction, normal reuse, incomplete diagnostic reuse and explicit refresh at the failing depth, including the original bilingual desktop scenario. The fixtures use local synthetic model responses, not a quality score or paid provider call. / 回归覆盖深路径的首次整理、复用、不完整诊断及显式刷新，保留失败运行记录；协议夹具不作为归纳质量成绩。

Both packaged 0.3.1 EXEs also passed a **283-character cache-lock** integration check, with four loopback fixture requests initially and zero new requests on reuse. With development/test dependencies available, reproduce after building each flavor using `& '.venv\Scripts\python.exe' 'tools\check_frozen_ai_cache.py' full` or `api`. This test uses self-authored sources and no downloaded course/model files. / 实际 EXE 也覆盖该缺陷；脚本只用原创材料和本机协议夹具，不调用付费服务。
