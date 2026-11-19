# Brief 0011：M2-4 主题插件 CSS 变量契约（#25）

## 角色与约定
你是实现者（Codex），在 UH! 仓库工作，先读 AGENTS.md。只改代码与测试，**不要 git commit**（DSH 审后提交）。复用 M2-1 PluginDriver（kernel/plugins.py）与 M2-2 沙箱。

## 目标
换肤是数据而非代码：主题插件只消费 CSS 变量，防 JS 注入。一键切换 200ms 内生效，Web/Desktop 同步（Desktop 未上线先验 Web）。

## 必读上下文
- AGENTS.md；kernel/plugins.py（PluginDriver/PluginManifest/六阶段生命周期/签名）；kernel/plugin_sdk.py。
- docs/contracts/manifest/（manifest schema，plugin_type 枚举；theme 类型 CSS 变量契约）；docs/contracts/manifest.schema.json。
- ui/index.html（:root CSS 变量 --uh-bg-primary/--uh-fg-primary/--uh-accent/--uh-radius 等）；ui/state-machine.js。
- v3 §4.7 / §5.4（主题插件 CSS 变量契约 + assets 限制）。

## 需求
1. **主题 manifest CSS 变量契约**：plugin_type=theme；manifest 声明 css_variables（map：变量名→值，仅允许 --uh-* 前缀 CSS 变量）；支持自定义字体（font-family）、间距（spacing）、圆角（radius）。校验变量名/值合法（禁 url()/expression()/javascript: 等注入向量）。
2. **一键切换 <200ms**：主题 activate 时将 css_variables 应用到 document :root（setProperty）；切换性能 <200ms（E2E-06）。提供主题切换 API/CLI（uh theme apply <name> 或 REST）。
3. **assets 限制**：主题 assets（字体/图片）限 MIME（woff2/png/svg 等白名单）、大小上限、来源（仅插件包内，禁外链）；非法 assets 拒绝。
4. **防 JS 注入**：主题包不可携带/执行 JS；manifest/资产校验拦截 script/事件处理器；CSS 值 sanitization。
5. **Web/Desktop 同步**：CSS 变量契约双端共用（Desktop M3-6 实装，先验 Web）；主题切换状态持久化（重启恢复）。
6. 复用 PluginDriver 签名/沙箱；密钥经 env，严禁硬编码。

## 测试（必须）
- 主题 install/activate：css_variables 应用到 :root（jsdom 或 node 模拟 / 纯函数校验）。
- CSS 变量契约校验：合法 --uh-* 通过；非法变量名/注入值（url/javascript/expression）拒绝。
- JS 注入拦截：主题包带 script/事件处理器被拒。
- assets 校验：非法 MIME/超大/外链拒绝。
- 切换性能：apply 纯函数 <200ms（大量变量）。
- 主题持久化：重启恢复 active 主题。
- 不得削弱现有 172 测试。

## 不动什么
- docs/contracts/ 冻结契约（theme schema 若需扩走只增 + 修订对照）；kernel/plugins.py 核心生命周期（可扩展 theme 分支）。

## 门禁（必须跑并报告）
- python -m pytest -q（期望全绿）
- node scripts/verify-all.mjs --full（期望 8/8）

## 报告契约
输出：新增/改动文件 + 设计要点（CSS 变量契约/防注入/assets 校验/切换性能）+ pytest + verify-all + 各验收如何验证。不要提交。
