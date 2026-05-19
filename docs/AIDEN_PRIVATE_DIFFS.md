# Aiden 私有分支与官方 Hermes Agent 差异记录

> 生成时间：2026-05-08  
> 仓库：`/Users/dev/.hermes/hermes-agent`  
> 本地私有分支：`main` / `origin/main` (`git@github.com:aiden-lightning/hermes-agent.git`)  
> 官方上游：`upstream/main` (`https://github.com/NousResearch/hermes-agent.git`)
> 最后同步：2026-05-18 — `git merge upstream/main` 完成，413 commits merged。

本文记录 `biden-agent` 与 `aiden-lightning` 两个 Git 作者在私有分支上相对官方代码保留的主要改动，用于后续同步上游、排查行为差异、以及判断哪些补丁需要继续维护或尝试 upstream。

## 1. 当前分叉状态

基于 `git fetch --all --prune` 后的结果：

- `main` 相对 `upstream/main`（同步后）：ahead **50+** 私有 commits（含 merge commit），behind **0**（已合并所有上游改动）。
- 工作区已暂存所有冲突解决，等待提交。
- 私有 ahead commits 作者分布：
  - `Lightning@Aiden <aiden-lightning@users.noreply.github.com>`：33 个 commit
  - `Biden@Aiden <biden-agent@users.noreply.github.com>`：17 个 commit
- 注意：差异里有大量上游前进带来的文件级差异（例如上游删除/重构、测试清理等），不一定都是 Aiden 私有功能；本文重点按私有作者 commit 主题归纳。

## 2. 私有改动总览

### 2.0 Aiden 分发入口与安装脚本

私有分支将面向用户的最小安装/仓库入口指向 Aiden fork：

- `README.md`
- `scripts/install.sh`
- `scripts/install.ps1`
- `scripts/install.cmd`

这些文件中的安装命令、clone URL、Windows fallback zip URL、README issues/license 链接使用 `aiden-lightning/hermes-agent`，避免用户通过 Aiden 私有分支 README 或安装脚本继续安装官方上游仓库。

### 2.1 Feishu / Lark 深度增强

这是私有分支最大的差异区域，核心文件主要是：

- `gateway/platforms/feishu.py`
- `gateway/run.py`
- `gateway/platforms/base.py`
- `gateway/session.py`
- `gateway/session_context.py`
- `tools/feishu_doc_tool.py`
- `tools/feishu_bitable_tool.py`
- `tools/send_message_tool.py`
- `toolsets.py`
- `website/docs/user-guide/messaging/feishu.md`
- 相关测试：`tests/gateway/test_feishu*.py`、`tests/tools/test_feishu_tools.py` 等

主要差异：

1. **群聊触发策略与开放策略**
   - 支持 `FEISHU_REQUIRE_MENTION` / `require_mention`，允许配置群聊是否必须 @ 机器人才触发。
   - 支持开放群策略（历史记录中称为 `FEISHU_GROUP_POLICY=open`）。
   - 默认忽略 Feishu/Lark `@_all`（@everyone），避免群公告触发机器人；可通过 `FEISHU_IGNORE_AT_ALL=false` 或 `platforms.feishu.extra.ignore_at_all: false` 恢复旧行为。
   - 相关 commit：`c83d9a64b feat(feishu): configurable requireMention, admin-only approval, open group policy`。

2. **审批卡片与安全交互**
   - 增强 Feishu 危险命令审批卡片行为。
   - 私有分支经历过“admin-only approval”到“remove approval admin gate”，后续又加入 **审批卡点击鉴权**。
   - 卡片点击按原请求者 / 管理员 / stable ID 匹配校验，后续补充 union_id 匹配。
   - 相关 commit：
     - `4e7af834a fix(feishu): remove approval admin gate`
     - `91bff3d65 fix(feishu): gate approval card actions`
     - `947bb8290 fix(feishu): match approval clicks by union id`
     - `7da61a377 test(feishu): close approval callback coroutines`

3. **Feishu 原生媒体、线程和消息路由**
   - 改进 Feishu native media delivery：文件、图片、语音等 `MEDIA:` 路由更贴近 Feishu adapter 的原生发送能力。
   - 对从模型文本、历史记录或调试输出中自动提取的 outbound `MEDIA:` / 本地文件附件增加可配置安全 allowlist，默认仅允许 `/tmp/`、`~/.hermes/audio_cache/`、`~/.hermes/image_cache/`、`~/.hermes/generated/`；显式工具附件发送不受该文本泄漏防护拦截。
   - 改进 thread routing / parent message 处理。
   - 对 `edit_message` 临时传输错误增加重试。
   - 相关 commit：
     - `ffb77ebe1 fix: improve Feishu native media delivery and thread routing`
     - `d74575453 fix: retry transient Feishu edit_message transport failures`

4. **Feishu 文档 / 多维表格工具**
   - `feishu_doc_read` 能脱离 comment context 使用。
   - 支持解析 Feishu wiki token 到真实 doc/bitable token。
   - 新增 `feishu_bitable_read` 工具，读取 Feishu/Lark Bitable。
   - 相关 commit：
     - `3a41a0995 fix: make feishu_doc_read work outside comment context`
     - `1fef6af79 fix: resolve wiki tokens in feishu_doc_read`
     - `08352cb0d feat(feishu): add bitable read tool`

5. **群上下文、附件、批处理与多用户隔离**
   - 在 mention gate 前缓存 Feishu group context，避免因为未触发而丢失后续可用上下文。
   - 修复群附件上下文保存。
   - 修复跨用户 text batch merge，避免不同用户快速消息被合并到同一批。
   - 修复快速 DM 批处理顺序。
   - 相关 commit：
     - `c2b43450e feat: cache feishu group context before mention gate`
     - `45f7ae97e fix(feishu): preserve group attachment context`
     - `70ba5a1fe fix(feishu): prevent cross-user text batch merging`
     - `0120a899b fix(feishu): preserve rapid DM batch ordering`

6. **Feishu session/search/bot history 隔离**
   - 隔离 session search，防止跨用户或跨上下文泄漏。
   - 保留 bot history，改善 Feishu 群聊/会话连续性。
   - 相关 commit：`0781016af fix(feishu): isolate session search and preserve bot history`。

7. **Feishu reaction 行为**
   - 只对 Hermes 自己发送并记录过的消息路由 reaction，避免对同一 Feishu app 发出的无关卡片误触发。
   - 引入 no-reply sentinel：reaction 或普通 turn 经 agent 判断返回精确 sentinel 时抑制可见发送，并过滤 transcript / Feishu group history 中的 sentinel，避免污染后续上下文。
   - 相关 commit：
     - `8baaaf97b fix(feishu): restrict reaction routing to sent messages`
     - `2b7472e3d fix(feishu): suppress no-op reaction replies`

8. **出站 @mention 渲染**
   - 记录 inbound mention refs，用于后续 outbound `@name` 渲染为 Feishu 真正的 `<at>` 元素。
   - 处理只包含 mention 的消息。
   - 相关 commit：
     - `b5ee187a0 fix(feishu): render known outbound mentions`
     - `448b8b3fb fix(feishu): handle mention-only messages`
     - `211b9037c feat(feishu): persist inbound mention refs for outbound at`

9. **Feishu 用户级命令 / 工具权限**
   - 增加 per-user command and tool permissions，允许对 Feishu 用户维度限制命令或工具访问。
   - 相关 commit：`dde29c4d2 feat(feishu): add per-user command and tool permissions`。

### 2.2 Gateway 生命周期、重启、排队与可观测性

相关文件主要是：

- `gateway/run.py`
- `gateway/platforms/base.py`
- `gateway/session_context.py`
- `hermes_logging.py`
- `run_agent.py`
- `tools/approval.py`
- `tools/terminal_tool.py`
- `tools/process_registry.py`

主要差异：

1. **Gateway watcher / background process delivery / teardown 加固**
   - 后台进程通知和 watcher delivery 更稳健。
   - 关闭/清理路径更健壮。
   - 相关 commit：`47f1b3abb fix: harden gateway watcher delivery and teardown`。

2. **Gateway restart/resume 行为**
   - 加固 gateway restart。
   - preserve restart resume state，减少重启中断后 session 状态丢失。
   - 相关 commit：
     - `0b3f74876 fix: harden gateway restart and opencode delegation`
     - `abdc3b16a fix(gateway): preserve restart resume state`
     - `5ab54fe46 test(gateway): isolate systemd preflight in service tests`

3. **Queued handoff interrupt 修复**
   - 清理 queued handoff interrupt，避免 pending/queued 消息处理时泄漏旧中断状态。
   - 相关 commit：`e8671a99f fix(gateway): clear queued handoff interrupt`。

4. **Gateway 状态显示**
   - `/status` 或 gateway status 中显示模型信息。
   - 相关 commit：`7b35d5da1`、`55ad90a97`，同名 `fix: show model info in gateway status`。

5. **日志级别与原始可观测性 tracing**
   - `gateway.log` 恢复/覆盖 logging coverage，并尊重 gateway debug level。
   - 增加 raw observability tracing，改进审批、Feishu、agent 运行路径的诊断能力。
   - 相关 commit：
     - `d9b95fd64 fix(logging): restore logging coverage and honor gateway debug level`
     - `0c5f107f4 fix(gateway): add raw observability tracing`
     - `7e336375e test(logging): cover observability redaction`

### 2.3 Cron job 网关安全隔离

相关文件：

- `cron/jobs.py`
- `gateway/run.py`
- `gateway/session_context.py`
- `tools/cronjob_tools.py`
- `tests/cron/test_scheduler.py`
- `tests/tools/test_cronjob_tools.py`

主要差异：

- Gateway 创建的 cron jobs 记录 owner metadata。
- 普通 gateway 用户只能管理自己的 job。
- Future run 继承并限制原会话 toolsets，避免 cronjob 递归调度或权限膨胀。
- 相关 commit：`76ef4f4d1 fix: isolate gateway cron jobs by user`。

### 2.4 Safe curl 工具与私网 URL 安全语义

相关文件：

- `tools/curl_tool.py`
- `tools/url_safety.py`
- `tools/browser_tool.py`
- `hermes_cli/tools_config.py`
- `toolsets.py`
- `website/docs/user-guide/features/browser.md`

主要差异：

1. **新增 safe curl tool**
   - 新增 `curl` 工具，用于安全 HTTP/HTTPS 请求。
   - 默认阻止 localhost、私网、metadata/link-local 等敏感目标。
   - 相关 commit：`c5b069450 feat(tools): add safe curl tool`。

2. **私网 URL allow 配置统一**
   - curl 与 browser SSRF/private URL 安全策略统一。
   - 支持 `security.allow_private_urls`，兼容旧 `browser.allow_private_urls`。
   - 即便允许私网，metadata 与 `169.254.0.0/16` 仍阻止。
   - 相关 commit：
     - `416e871b9 fix(curl): honor private URL allow config`
     - `39ccf72ce fix(security): unify private URL allow semantics`

### 2.5 Browser / CDP 稳定性

相关文件：

- `tools/browser_tool.py`
- `tools/browser_cdp_tool.py`
- `tui_gateway/server.py`
- `tests/tools/test_browser_connect_tool.py`
- `tests/tools/test_browser_cdp_tool.py`
- `tests/tools/test_browser_console.py`

主要差异：

- 改进 CDP-backed browser sessions。
- 支持 async CDP websocket factories，兼容不同 `websockets.connect` 运行时行为。
- 增强 browser connect / console 相关测试。
- 相关 commit：
  - `e3fcdf221 fix(browser): improve CDP-backed sessions`
  - `ac66429d6 fix(browser): support async CDP websocket factories`

### 2.6 TTS：MOSS-TTS-Nano 本地 ONNX Provider

相关文件：

- `.gitmodules`
- `moss_tts_nano_repo`（submodule）
- `tools/tts_tool.py`
- `hermes_cli/config.py`
- `hermes_cli/setup.py`
- `hermes_cli/web_server.py`
- `tests/tools/test_tts_moss.py`
- `tests/tools/test_tts_max_text_length.py`

主要差异：

- 新增 `MOSS-TTS-Nano` 本地 ONNX TTS provider。
- 加入合成输出校验与 ffmpeg failure 处理。
- 保持 MOSS OGG 输出 Telegram-safe。
- 增加输出验证测试。
- 相关 commit：
  - `a1ed70d05 feat(tts): add MOSS-TTS-Nano local ONNX provider support`
  - `8a79a070a fix(tts): validate MOSS synthesize output and handle ffmpeg failures`
  - `aca83f416 fix(tts): keep MOSS ogg output Telegram-safe`
  - `63a7dc16d test(tts): add output validation tests for MOSS provider`

### 2.7 Image generation：OpenRouter GPT-5.4 Image 2

相关文件：

- `plugins/image_gen/openai/__init__.py`
- `tests/plugins/image_gen/test_openai_provider.py`

主要差异：

- OpenAI-compatible image provider 支持 OpenRouter `GPT-5.4 Image 2`。
- 相关 commit：`a14734e1d fix(image-gen): support OpenRouter GPT-5.4 Image 2`。

### 2.8 Model metadata / custom endpoint probe 缓存

相关文件：

- `agent/model_metadata.py`
- `tests/agent/test_model_metadata_local_ctx.py`

主要差异：

- 对 custom endpoint context length probe 失败结果做持久化缓存，降低重复失败探测和日志噪音。
- 相关 commit：`dd2e14bd7 fix: persist failed custom endpoint context probes`。

### 2.9 OpenCode / delegation / terminal PTY 相关加固

相关文件：

- `agent/copilot_acp_client.py`
- `skills/autonomous-ai-agents/opencode/SKILL.md`
- `tools/terminal_tool.py`
- `gateway/run.py`
- 相关测试

主要差异：

- 加固 gateway restart 相关路径时，同时更新 OpenCode delegation 使用说明和 terminal/PTY 行为测试。
- 相关 commit：`0b3f74876 fix: harden gateway restart and opencode delegation`。

## 3. 作者维度 commit 清单

### 3.1 `Biden@Aiden <biden-agent@users.noreply.github.com>`

非 merge 私有 commits：

- `c83d9a64b` — `feat(feishu): configurable requireMention, admin-only approval, open group policy`
- `7b35d5da1` — `fix: show model info in gateway status`
- `55ad90a97` — `fix: show model info in gateway status`
- `ffb77ebe1` — `fix: improve Feishu native media delivery and thread routing`
- `a1ed70d05` — `feat(tts): add MOSS-TTS-Nano local ONNX provider support`
- `8a79a070a` — `fix(tts): validate MOSS synthesize output and handle ffmpeg failures`
- `aca83f416` — `fix(tts): keep MOSS ogg output Telegram-safe`
- `63a7dc16d` — `test(tts): add output validation tests for MOSS provider`
- `a14734e1d` — `fix(image-gen): support OpenRouter GPT-5.4 Image 2`
- `3a41a0995` — `fix: make feishu_doc_read work outside comment context`
- `1fef6af79` — `fix: resolve wiki tokens in feishu_doc_read`
- `08352cb0d` — `feat(feishu): add bitable read tool`
- `47f1b3abb` — `fix: harden gateway watcher delivery and teardown`
- `d74575453` — `fix: retry transient Feishu edit_message transport failures`
- `dd2e14bd7` — `fix: persist failed custom endpoint context probes`

Merge commits：

- `16ddef33a` — `Merge upstream main (124 commits) — keep feishu patches`
- `7954e46cf` — `Merge remote-tracking branch 'origin/main' into private-origin-sync`

### 3.2 `Lightning@Aiden <aiden-lightning@users.noreply.github.com>`

非 merge 私有 commits：

- `0b3f74876` — `fix: harden gateway restart and opencode delegation`
- `4e7af834a` — `fix(feishu): remove approval admin gate`
- `dde29c4d2` — `feat(feishu): add per-user command and tool permissions`
- `c2b43450e` — `feat: cache feishu group context before mention gate`
- `d9b95fd64` — `fix(logging): restore logging coverage and honor gateway debug level`
- `91bff3d65` — `fix(feishu): gate approval card actions`
- `c5b069450` — `feat(tools): add safe curl tool`
- `8baaaf97b` — `fix(feishu): restrict reaction routing to sent messages`
- `76ef4f4d1` — `fix: isolate gateway cron jobs by user`
- `70ba5a1fe` — `fix(feishu): prevent cross-user text batch merging`
- `0781016af` — `fix(feishu): isolate session search and preserve bot history`
- `45f7ae97e` — `fix(feishu): preserve group attachment context`
- `e8671a99f` — `fix(gateway): clear queued handoff interrupt`
- `947bb8290` — `fix(feishu): match approval clicks by union id`
- `7da61a377` — `test(feishu): close approval callback coroutines`
- `e3fcdf221` — `fix(browser): improve CDP-backed sessions`
- `ac66429d6` — `fix(browser): support async CDP websocket factories`
- `abdc3b16a` — `fix(gateway): preserve restart resume state`
- `416e871b9` — `fix(curl): honor private URL allow config`
- `39ccf72ce` — `fix(security): unify private URL allow semantics`
- `0120a899b` — `fix(feishu): preserve rapid DM batch ordering`
- `0c5f107f4` — `fix(gateway): add raw observability tracing`
- `7e336375e` — `test(logging): cover observability redaction`
- `5ab54fe46` — `test(gateway): isolate systemd preflight in service tests`
- `b5ee187a0` — `fix(feishu): render known outbound mentions`
- `448b8b3fb` — `fix(feishu): handle mention-only messages`
- `211b9037c` — `feat(feishu): persist inbound mention refs for outbound at`
- `2b7472e3d` — `fix(feishu): suppress no-op reaction replies`

Merge commits：

- `5c6bed00c` — `Merge branch 'private-main-update'`
- `b81bd346a` — `Merge remote-tracking branch 'origin/main'`
- `3fa08f28b` — `Merge remote-tracking branch 'upstream/main'`
- `5ca69483e` — `Merge remote-tracking branch 'upstream/main'`
- `32ac6beff` — `Merge remote-tracking branch 'upstream/main'`

## 4. 高风险维护点

1. **Feishu adapter 差异很大**
   - `gateway/platforms/feishu.py` 是最大冲突点。
   - 每次合并上游 Feishu / gateway 改动都要重点复核：mention gate、reaction、approval、media、batching、session metadata。

2. **Gateway run/session 改动分散**
   - `gateway/run.py` 同时承载 restart、cron ACL、Feishu permissions、raw tracing、no-reply suppression 等多类私有逻辑。
   - 上游 gateway 生命周期改动容易与这些逻辑冲突。

3. **安全语义需要持续一致**
   - `curl`、browser SSRF、private URL allow、cron owner ACL、Feishu approval click auth 都属于安全相关差异。
   - 不应在同步上游时为了解冲突而简单删除测试。

4. **MOSS-TTS-Nano submodule 是私有依赖点**
   - `.gitmodules` 与 `moss_tts_nano_repo` 需要在 clone/update 时保持可用。
   - 相关测试应确保无模型权重环境下仍可 mock 通过。

5. **最近一次 upstream 同步（2026-05-18）**
   - 合并 413 个上游 commits。
   - 冲突解决位于 `.gitmodules`、`gateway/platforms/base.py`、`gateway/run.py`、`run_agent.py`、`tools/cronjob_tools.py`、`tools/tts_tool.py`。
   - `.gitmodules` 保留私有 `moss_tts_nano_repo` submodule，删除上游已移除/本地不存在的 `tinker-atropos` submodule。
   - `gateway/run.py` 合并私有审批 metadata（`user_id` / `user_id_alt` / thread metadata）与上游 `safe_schedule_threadsafe` 调度、platform reconnect/circuit-breaker、session/gateway lifecycle 改动。
   - `run_agent.py` 保留私有 `AIAgent` constructor/session-search/tool-observability 兼容行为，同时吸收上游模块拆分后的相关导入与辅助行为。
   - `tools/cronjob_tools.py` 合并上游 name-based `resolve_job_ref` 与私有 gateway cron owner/ACL 检查。
   - `tools/tts_tool.py` 合并上游 xAI OAuth credential 检测/说明与私有 MOSS-TTS-Nano provider 文档和行为。

6. **上一轮 upstream 同步（2026-05-14）**
   - 合并 172 个上游 commits。
   - 冲突解决位于 `gateway/run.py`、`hermes_cli/memory_setup.py`。
   - `gateway/run.py` 同时保留 Aiden per-user/platform command permission enforcement 与上游 free-form `clarify` reply interception；非 slash 文本仅在同 session 存在 pending clarify 时先解析为答案，slash command 与其它普通文本仍走权限检查。
   - `hermes_cli/memory_setup.py` 保留 Aiden `_safe_dependency_check_argv(...)` + `shell=False` 的安全依赖检查路径，吸收上游检查行为并去除重复 `shlex` import。

## 5. 建议的后续维护方式

1. **把私有补丁拆成主题分支或 patch stack**
   - `feishu-core`
   - `feishu-security-approval`
   - `feishu-doc-bitable-tools`
   - `gateway-restart-observability`
   - `gateway-cron-acl`
   - `safe-curl-url-safety`
   - `moss-tts`
   - `browser-cdp-stability`

2. **每次同步上游后跑最小回归集**

   ```bash
   scripts/run_tests.sh \
     tests/gateway/test_feishu.py \
     tests/gateway/test_feishu_approval_buttons.py \
     tests/gateway/test_feishu_command_permissions.py \
     tests/gateway/test_no_reply_sentinel.py \
     tests/tools/test_feishu_tools.py \
     tests/tools/test_curl_tool.py \
     tests/tools/test_url_safety.py \
     tests/tools/test_browser_cdp_tool.py \
     tests/tools/test_browser_connect_tool.py \
     tests/tools/test_tts_moss.py \
     tests/tools/test_cronjob_tools.py
   ```

3. **同步前先确认是否已有上游等价实现**
   - Feishu native tools、cron ACL、safe curl、browser CDP、reaction no-reply 这类功能未来可能被官方吸收或重写。
   - 如果上游已有等价实现，应优先收敛到上游实现，减少私有维护面。

4. **保留此文档作为差异索引**
   - 后续新增私有 commit 时，在本文追加主题、commit 和测试入口。

## 6. 历次上游同步冲突解决记录

### 6.1 本次上游同步冲突解决记录（2026-05-18）

合并 413 个 upstream commits，6 个文件存在冲突。以下为每处冲突的解决策略：

#### 6.1.1 `.gitmodules`

- 上游删除 `tinker-atropos` submodule；私有分支仍有 `moss_tts_nano_repo`。
- 解决策略：删除 `tinker-atropos` entry，保留私有 MOSS-TTS-Nano submodule entry。

#### 6.1.2 `gateway/platforms/base.py`

- 冲突点在 platform message metadata / channel context / TTS text preparation 附近。
- 解决策略：保留上游 `channel_context` 字段、clarify fallback text capture、rapid text merge 行为，同时保留私有 `prepare_tts_text(...)` hook 供平台过滤 TTS 文本。

#### 6.1.3 `gateway/run.py`

- 冲突点集中在 startup/reconnect lifecycle、Feishu approval metadata、thread/user identity、safe cross-thread scheduling。
- 解决策略：合并私有 approval metadata（`user_id`、`user_id_alt`、thread metadata）与上游 `safe_schedule_threadsafe`、platform reconnect/circuit-breaker、session/gateway lifecycle 改动。

#### 6.1.4 `run_agent.py`

- 上游将 `AIAgent` 大量初始化、conversation loop、tool execution 逻辑拆分到 `agent/*` helper modules；私有分支仍有 Feishu/session-search/tool-observability 兼容字段和行为。
- 解决策略：保留私有 `AIAgent` constructor/session-search/tool-observability 兼容行为，吸收上游模块拆分后的相关导入和辅助行为，避免 gateway 传入私有参数时失配。

#### 6.1.5 `tools/cronjob_tools.py`

- 冲突点在 cron job lookup/permission enforcement。
- 解决策略：合并上游 name-based `resolve_job_ref` 行为与私有 gateway cron owner/ACL 检查，继续阻止普通 gateway 用户管理不属于自己的 job。

#### 6.1.6 `tools/tts_tool.py`

- 冲突点在 TTS provider docs/credential detection：上游新增 xAI OAuth 检测，私有分支有 MOSS-TTS-Nano provider。
- 解决策略：保留 MOSS provider 文档和行为，合并上游 xAI OAuth credential 检测/说明。

#### 6.1.7 本次验证

- `python -m py_compile gateway/platforms/base.py gateway/run.py run_agent.py tools/cronjob_tools.py tools/tts_tool.py`
- import smoke check：`gateway.platforms.base`、`gateway.run`、`run_agent`、`tools.cronjob_tools`、`tools.tts_tool`

### 6.2 上游同步冲突解决记录（2026-05-14）

合并 172 个 upstream commits，2 个文件存在冲突。以下为每处冲突的解决策略：

#### 6.1.1 `gateway/run.py`

- 冲突点在私有/Aiden command permission enforcement 与上游 free-form `clarify` text reply interception 的相邻插入位置。
- 解决策略：保留两者，但 pending clarify 的非 slash 文本回复必须先于 free-text 权限拒绝处理，否则 `allow_plain_text=false` 会阻止用户回答 clarify；slash command 不被 clarify 消费，仍按 command permission enforcement 正常检查。
- clarify 拦截只消费非空且非 slash command 的文本回复，调用 `tools.clarify_gateway.resolve_gateway_clarify(...)` 后返回空字符串，避免将同一条回复再次作为新 agent turn 发送。
- 同文件还带入上游本次新增 gateway 行为（例如 security advisory startup logging、`/codex-runtime`、`/subgoal`、hook `chat_id`、queued follow-up history offset preservation 等），未删除既有 Aiden Feishu/gateway 私有逻辑。

#### 6.1.2 `hermes_cli/memory_setup.py`

- 冲突点在依赖检查命令解析：上游直接 `shlex.split(...)`，私有分支已有 `_safe_dependency_check_argv(...)` + `shell=False` 安全包装。
- 解决策略：保留 `_safe_dependency_check_argv(...)` 路径，吸收上游检查行为，并去除 merge 后重复的 `import shlex`。

#### 6.1.3 本次验证

- `python -m py_compile gateway/run.py hermes_cli/memory_setup.py`
- `scripts/run_tests.sh tests/gateway/test_feishu_command_permissions.py tests/tools/test_clarify_gateway.py tests/hermes_cli/test_setup.py -q`

### 6.2 上游同步冲突解决记录（2026-05-08）

合并 222 个 upstream commits，3 个文件存在冲突。以下为每处冲突的解决策略：

### 6.1 `gateway/run.py` — 5 处冲突

1. **方法区冲突（Aiden 进程 watcher vs 上游 goal 管理）**
   - 双方均为新增独立方法，无逻辑重叠。保留双方代码：`_track_background_task`、`_start_process_watcher_task`、`_drain_process_watcher_registrations`、`_process_watcher_dispatcher`（Aiden）+ `_is_goal_continuation_event`、`_clear_goal_pending_continuations`、`_goal_still_active_for_session`（upstream）。

2. **重启启动路径**
   - 上游新增 `_schedule_resume_pending_sessions()` 调度中断会话自动恢复，原始循环恢复 process watcher。采用上游框架，但使用 Aiden 的 `_drain_process_watcher_registrations(recovered=True)` 替代 raw 循环，保留去重和日志增强。

3. **turn 完成后清除 resume_pending**
   - 上游新增 `_should_clear_resume_pending_after_turn()` 辅助函数，逻辑更严谨（检查 interrupted、failed、partial、completed 状态）。采用上游版本。

4. **error/success 路径返回 dict**
   - 上游新增 `partial`、`completed`、`error` 字段，丰富结果元数据。采用上游版本以兼容调用方。

### 6.2 `tools/browser_tool.py` — 2 处冲突

1. **url_safety import fallback**
   - `_is_always_blocked_url` fallback lambda：私有用 `return False`，上游用 `return True`。采用上游 `return True`（即便安全模块不可用，也阻止 metadata endpoint）。

2. **SSRF 检查统一为 `not _is_local_backend()` 风格**
   - 双方在 pre-nav IMDS、general SSRF、post-redirect 三处使用不同的变量判断（私有用 `cloud_ssrf_applies = not _is_local_backend()`，上游直接写 `not _is_local_backend()`）。
   - 冲突解决时两套代码共存，导致 pre-nav IMDS 块重复（私有版本影子了上游版本，使上游版本成为死代码）。
   - 最终清理：删除私有 `cloud_ssrf_applies` 变量，所有三处检查统一使用上游 `not _is_local_backend()` 风格。pre-nav IMDS 保留上游详细注释 + #16234 说明 + 特定错误消息，语义更明确。

### 6.3 `tests/tools/test_browser_ssrf_local.py` — 1 处冲突 + 后续修复

- 上游新增 `test_cloud_blocks_redirect_to_imds_even_via_sidecar` 测试 + `TestAllowPrivateUrlsConfig` 类。采用上游完整新测试，验证 IMDS 阻断和 allow_private_urls 配置行为。
- 冲突解决后仍有 8 项测试因以下原因失败：

  a. **`_allow_private_urls` 模块级函数丢失**：私有分支的 `browser_tool._allow_private_urls()`（读取 `security.allow_private_urls` / 旧 `browser.allow_private_urls`）在冲突清理时被移除，但测试仍使用 monkeypatch 设置该属性。已于 2026-05-08 恢复，作为带独立缓存的模块级函数，使用 `utils.is_truthy_value` 处理配置字符串。

  b. **错误消息变更**：上游 IMDS 阻断使用精确消息 `"URL targets a cloud metadata endpoint"` / `"redirect landed on a cloud metadata endpoint"`。私有分支测试中两处的旧断言已更新：

  - `TestPreNavigationSsrf.test_cloud_auto_local_blocks_always_blocked_url`：`"private or internal address"` → `"cloud metadata endpoint"`
  - `TestPostRedirectSsrf.test_cloud_auto_local_blocks_redirect_to_always_blocked_url`：`"redirect landed on a private/internal address"` → `"cloud metadata endpoint"`
