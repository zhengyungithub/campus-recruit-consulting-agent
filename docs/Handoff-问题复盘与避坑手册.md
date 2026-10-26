# 校招咨询机器人 · Handoff 交接文档（问题复盘与避坑手册）

> 整理日期：2026-10-25
> 适用范围：接手本项目继续开发 / 排障 / 做下一期改造的人
> 配套文档：`docs/开发文档-校招咨询机器人.md`、`docs/功能规划-候选人端与HR后台.md`、`docs/Agent演进路线.md`、`docs/POC验证方案.md`
> 逐日工作日志：`.workbuddy/memory/2026-10-24.md`、`2026-10-25.md`

---

## 0. 一页速览

| 项 | 内容 |
|---|---|
| 项目 | 校招咨询机器人双端：候选人端（AI 问答 / 进度 / 转工单）+ HR 后台（配置 / 知识库 / 工单 / 账号 / 评测） |
| 技术栈 | 前端 Vue3 + Element Plus + Vite6；后端 FastAPI + SQLite；Agent 层 LangGraph + LangChain 工具 + Qdrant 混合检索 + SSE |
| 模型 | DashScope（阿里百炼）兼容端点，LLM `qwen-flash`，Embedding `text-embedding-v3` |
| 当前进度 | 基础双端全部完成；Agent 演进已完成 **第 0~3 期** + **S1 全部 / S3 / S4.1 / S4.2 / S5.1 / S5.2** |
| 当前基线 | intent 30 条 100%、fixed 30 条 100%、**agent 42 条 100%**（`eval_runs #17`） |
| 最大风险 | **DashScope 免费额度耗尽 `403 Free quota exhausted`**（2026-10-26 晚）——1M 免费 tokens 用完，且账号处于「仅免费额度」模式，不会自动转付费。见 §2.6 故障 C |
| 下一步 | 见 `docs/开发排期.md`：解除额度限制 → S2 数据闭环（沉淀去重 / 来源标记 / 检查点清理 / git 纳管）→ S4.3 回归门禁脚本化 → S5.3 事件触发主动推送 |
| 已完成 | 第 0~3 期 Agent 演进 + **S1 安全与治理基座（提示词版本化 / 注入检测 / PII 脱敏 / 限流）** + **S3 HITL 收口（方案 A）** |
| 提示词位置 | `app/agent/prompts/{system,plan_llm,judge}.txt`，版本记录在 `prompt_versions` 表，运行时指纹写进 `agent_runs.prompt_version` |

**五条铁律（血泪换来的，先读这里）**：

1. **改任何东西之前先跑一轮评测存基线**，改完再跑一轮对比。没有基线就没有回归门禁。
2. **前端部署必须清理式**：`rm -rf app/web_dist/assets` 后再 `cp -r frontend/dist/* app/web_dist/`。合并复制会让旧 JS 一直存在，用户浏览器永不刷新。
3. **跑命令行评测前必须先停 uvicorn**，否则 Qdrant 本地库 `.lock` 报 `Permission denied`。
4. **抽函数 / 重构后先跑单测再跑全量评测**（第 3 期在这个上面浪费了一整轮：全量 `KeyError('status')`）。
5. **不要用肉眼判断命令回显里的凭据**——回显可能被脱敏改写。验证账号密码要用 Python 从文件里提取原样请求。
6. **核查时搜全项目，不要只搜入口文件**（2026-10-25 的教训）：判断"某功能是否接线"时只搜了 `main.py`，
   漏掉 `app/agent/routes.py`，得出"无调用路由"的错误结论。路由分散在多个 router 文件里，
   判断前必须 `grep -rn` 整个 `app/` 目录。
7. **项目无 git，删除代码前先备份**：删到 `app/.backup_<日期>_<主题>/` 再动手。
   本轮 S3 删除 HITL 死代码即按此执行。
8. **`llm_ready: true` 不等于账号可用**：它只检查 Key 是否配置。判定模型是否真的能用，必须发一次真实请求。

---

## 1. 系统现状

### 1.1 目录结构

```
D:/校招咨询agent
├── app/                      # 后端（FastAPI）
│   ├── main.py               # 全部路由 + 鉴权 + 建表 migrate
│   ├── agent/                # Agent 层（13 个文件）
│   │   ├── graph.py          # LangGraph 主图：context→intent→plan→agent⇄act→critic→(degrade/clarify)
│   │   ├── intent.py         # 意图识别（规则优先 → KB 强命中 → LLM 兜底）
│   │   ├── planner.py        # 规则化任务规划 + 子任务探测
│   │   ├── verifier.py       # 双通道核验：L1 规则 + L2 LLM Judge
│   │   ├── coref.py          # 纯规则指代还原
│   │   ├── memory.py         # 三层记忆：短期窗口 / 长期画像 / 向量召回
│   │   ├── retrieval.py      # Qdrant 混合检索（dense 失败静默降级关键词）
│   │   ├── eval.py           # 评测引擎（intent / fixed / agent 三模式）
│   │   ├── tools.py          # 4 个 LangChain 工具（带 Pydantic schema + 审计）
│   │   ├── service.py / pipeline.py / routes.py / runtime.py
│   │   ├── config.py         # PEP 562 动态配置（DB settings > env > 默认）
│   │   ├── llm_service.py    # 模型实例缓存（reset_models() 热更新）
│   │   └── store.py
│   ├── evals/cases.jsonl     # 37 条标注用例
│   ├── web_dist/             # 前端构建产物（FastAPI 挂载 /ui/）
│   ├── campus_bot.db         # 业务库
│   ├── checkpoints.db        # LangGraph 检查点
│   ├── qdrant_data/          # 向量库（本地模式，进程独占）
│   └── .env                  # 密钥，不入库
├── frontend/                 # Vue3 工程
│   └── src/{views,components,api.js,router.js}
├── docs/                     # 本文档所在目录
└── .playwright-cli/          # 浏览器验证截图留档
```

### 1.2 启动与部署

```bash
# 后端（后台任务方式启动，便于日志回收）
cd D:/校招咨询agent/app
<venv>/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

# 前端构建 + 清理式部署
cd D:/校招咨询agent/frontend && npm run build
rm -rf ../app/web_dist/assets && cp -r dist/* ../app/web_dist/

# 入口
#   候选人端  http://127.0.0.1:8000/ui/
#   HR 后台   http://127.0.0.1:8000/ui/#/admin
```

- Python 解释器：`C:/Users/zheng-yun/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
- 已装额外依赖：`pypdf`、`python-docx`、`python-multipart`
- 线上已发布：`https://campus-consult-agent.app.workbuddy.host/?sharecode=DIF2H3UVSD`
- 管理员账号：`admin`，密码见部署记录（**不要写进文档/代码/提交信息**）；支持环境变量 `ADMIN_USER` / `ADMIN_PASSWORD`

---

## 2. 问题清单

> 每条按 **现象 → 根因 → 解法 → 防复发约定** 组织。标 ⭐ 的是反复踩或代价最高的问题。

---

### 2.1 前端：白屏与"空气泡"系列 ⭐⭐⭐

这是本项目最烧时间的一类问题，前后共 4 次不同根因，现象却完全一样（页面白 / 气泡空着不显示字）。

#### ① 白屏 · 浏览器缓存旧 index.html

| | |
|---|---|
| 现象 | 重建前端后，用户一打开就是白屏，控制台报旧 hash JS 的 404 |
| 根因 | 每次 `vite build` 产物 hash 变化，旧 JS 被删；但用户浏览器缓存了旧 `index.html`，它引用的正是已删除的旧 JS → 404 → 白屏 |
| 解法 | ① `main.py` 加 http middleware，对 `/ui` 与 `/` 返回 `Cache-Control: no-cache, no-store, must-revalidate`；② `/ui/assets/*.js` 404 时返回一段 `location.replace('/ui/?r='+Date.now()+location.hash)` 自愈脚本（200）；③ `index.html` 内置 SCRIPT onerror 自愈 |
| 防复发 | **部署策略必须是清理式**（见铁律 2）。曾经为了"兼容旧标签页"改成合并复制，结果旧 JS 永远不 404，自愈永不触发，用户看到的一直是旧版本，反而制造了更多"我改了没生效"的假故障 |

#### ② 白屏 · `loadMe()` 解包错误（真正根因）⭐

| | |
|---|---|
| 现象 | 候选人端登录后整页空白，控制台 `TypeError: Cannot read properties of undefined (reading 'slice')` |
| 根因 | `/api/me` 返回包装对象 `{candidate, history, timeline, next_step}`，`loadMe()` 直接整体赋给 `profile`，模板里 `profile.name.slice(0,1)` 取头像首字母时崩溃 → Vue 渲染中断白屏 |
| 解法 | `loadMe` 解包为 `{ ...d.candidate, next_step, timeline }`；头像改 `profile?.name?.slice(0,1) \|\| '客'` |
| 防复发 | **headless 冒烟测试必须带登录态**。第一次用无登录态的干净浏览器测，渲染的是登录页，根本走不到 `loadMe`，所以连续两轮都误判为"缓存问题"。凡是有"登录后才有数据"的模板取值，一律可选链兜底 |

#### ③ 空气泡 · ContextVar token 跨上下文重置

| | |
|---|---|
| 现象 | 回答流到一半中断，气泡空着；后端日志 `ValueError: Token was created in a different Context` |
| 根因 | 上一轮修复引入的：`_sink` 是 ContextVar，生成器在 ASGI 响应阶段可能在不同执行上下文里被恢复，`_sink.reset(token)` 跨 Context 直接抛异常 |
| 解法 | `finally` 里改用 `_sink.set(None)` 覆盖，不做 reset（每请求是独立上下文副本，不会泄漏） |
| 防复发 | SSE 生成器里凡是跨 `await` 的上下文变量清理，一律用 `.set(None)`，不要用 `.reset(token)` |

#### ④ 空气泡 · `push()` 忘记 return ⭐

| | |
|---|---|
| 现象 | 提问后助手气泡一直空转；但历史里的老回答显示正常 |
| 根因 | `CandidateView.push()` 没有 `return` 消息对象 → `send()` 里 `bot === undefined` → 第一个 `delta` 事件调用 `paint()` 时抛 TypeError → 没有任何文字进气泡。历史回答是登录时 `loadHistory` 渲染的，所以看起来"部分是好的"，极具迷惑性 |
| 解法 | `push()` 返回 `messages.value` 末位的响应式代理 |
| 防复发 | 前端 `push/append` 类函数统一约定返回刚创建的对象；气泡组件加兜底：流结束而 `buf` 为空时显示"回答没有生成成功，请重试"，而不是留一个空气泡 |

---

### 2.2 前端：交互与数据绑定

#### ⑤ 候选人状态下拉"改不动 / 弹回"

| | |
|---|---|
| 现象 | HR 后台改候选人状态，界面弹回原值 |
| 根因 | `el-select` 用单向 `:model-value` + 异步成功后 `load()` 回写，存在显示时序竞争 |
| 解法（二次修复） | 改 `v-model` **乐观更新**：选完立即显示新值，`PATCH` 失败才 `load()` 回滚；成功给 toast |
| 防复发 | 表格内联编辑一律乐观更新 + 失败回滚，不要"等接口回来再刷新整表" |

#### ⑥ 后台所有列表"全是空的"

| | |
|---|---|
| 现象 | 服务重启后，HR 后台看板/候选人/工单全部空白，无报错 |
| 根因 | `SESSIONS` 是内存字典，重启后浏览器 `sessionStorage` 里的旧 token 全部 401；而前端各 `load()` 没有错误处理，静默显示空列表 |
| 解法 | ① 新增 `sessions` 表 + `persist_session()`，token 落 SQLite，`auth()` 内存未命中时从库恢复（重启后会话不丢）；② `api.js` 的 `request()` 统一处理 401：admin 路径清 `A_TOKEN`、候选人路径清 `C_TOKEN` 并自动 reload 回登录页 |
| 防复发 | **任何 `load()` 都必须有 catch 并给用户可见提示**；静默失败是排查成本最高的故障形态 |

#### ⑦ 折叠菜单 / 字体 / 徽标等 UI 细节

侧栏折叠（220↔64px，`localStorage: campus_aside_collapsed`）、工单 pending 徽标（弃用 EP 默认亮粉 `#f56c6c` 与莫兰迪灰绿冲突，改手写胶囊 `#c4826f` 陶土玫瑰）、字体层级（顶栏标题 20/700/字距 2px，品牌 17/700/字距 1.5px）。均无功能风险，改动后走标准构建部署即可。

---

### 2.3 会话、鉴权与事件流

#### ⑧ Agent 过程面板一直空白 ⭐

| | |
|---|---|
| 现象 | 打开了"Agent 过程"开关，面板里始终没有任何事件 |
| 根因 | `emit()` 依赖 ContextVar 的 sink，而 **ContextVar 不会继承到 `threading.Thread` 新建的子线程** —— graph 在子线程里跑，内部 `emit()` 的事件全部丢失 |
| 解法 | 改用 `contextvars.copy_context().run(...)` 把上下文副本传进子线程 |
| 防复发 | 凡是在新线程里执行、又依赖请求级上下文变量的代码，都要显式 copy_context |

#### ⑨ 意图识别层"静默降级" ⭐

| | |
|---|---|
| 现象 | 加了意图识别后，四个场景全部 fallback 到固定管线，看上去"没生效" |
| 根因 | `SYSTEM_PROMPT` 新增了 `intent_label` / `intent_hint` 占位符，但 `run_agent` 没有传值 → `KeyError` → 整条 Agent 链路静默降级 |
| 解法 | 补齐提示词变量注入 |
| 防复发 | 提示词模板改了占位符，必须全局搜一遍注入点；`str.format` 的 KeyError 在 try 块里会被吞成"降级"，极难发现 |

#### ⑩ 登录 401（密码输错假象）

| | |
|---|---|
| 现象 | 用户反馈"登录不了"，uvicorn 日志确认 3 次 401；但干净浏览器用同样凭据 200 正常 |
| 根因 | 输入时带全角字符或首尾空格 |
| 解法 | 新增 `_norm_input()`（`strip` + `unicodedata.normalize('NFKC')` 全角转半角），统一应用于 `admin_login`、候选人 `login_password`、`account_password_save`、`admin_update_account`、`create_account` |
| 防复发 | 所有用户名/密码入参统一 normalize；再次强调：**验证凭据相关问题不要相信命令回显**（会被脱敏），要用 Python 从文件读原样值请求 |

#### ⑪ 鉴权头格式

管理端接口统一 `Authorization: Bearer <token>`（**不是** `X-Token`）。角色体系：`super` / `hr` 两级，`accounts` 表存角色，`auth_super` 装饰器对非超管抛 403。

---

### 2.4 后端接口与数据一致性

#### ⑫ fixed 管线工单分支 500

`_create_ticket` 与调用方重复写 `messages`，且 `reply=None` 触发 NOT NULL 约束。
**解法**：`fixed_chat_reply` 工单分支也返回文本，消息统一由调用方落库；SSE 的 fallback / shadow 分支补 `_bot_reply`。

#### ⑬ FAQ 改了不生效

| | |
|---|---|
| 现象 | 知识库增删改后检索结果不变，必须重启服务 |
| 根因 | 向量索引只在启动时同步 |
| 解法 | FAQ 增删改后自动调用 `retrieval.sync_faqs()` |
| 防复发 | **直接改 DB 不会触发 sync**，且 **Qdrant 本地模式被服务进程独占**，外部进程无法 sync。要清测试数据必须通过 API 增删，让服务自己重建索引 |

#### ⑭ FAQ 重复播种 & Embedding 批量限制

- 初始化逻辑被执行多次导致 FAQ 被播种 3 份（45 条）→ 已去重回 15 条。初始化脚本必须幂等。
- DashScope Embedding 单批上限 10 条，15 条整批提交被 400 拒 → 按 10 分批。

#### ⑮ shadow 模式误建工单

shadow 旁路本意是只读对比，却会真实创建工单。
**解法**：shadow 路径加 `allow_ticket=False`。任何"旁路/影子"执行路径，默认禁写。

#### ⑯ 跨请求数据串味（评测场景）

见 §2.5 第 1 期坑①，`run_agent(..., use_history=False)`。

---

### 2.5 Agent 演进：评测逼出来的问题（第 0~3 期）

> 这一节的问题**全都是评测跑出来的**。没有评测之前，这些缺陷只会在偶发场景里以"模型抽风"的名义被忽略。

#### 第 1 期（图重构）

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 1 | 评测同一账号连跑，前一条答案被灌进后一条上下文，出现"张冠李戴" | 跨轮记忆污染 | `run_agent(..., use_history=False)` 评测隔离历史，线上保留连贯记忆 |
| 2 | **单条用例卡了近 40 分钟**，整轮评测报废 | Embedding 调用无超时 | `OpenAIEmbeddings(timeout=20, max_retries=1)`；`ChatOpenAI(max_retries=1)`；eval 侧再加 120s 线程护栏 + try/except |
| 3 | 一轮里调 3 次检索，只有最后一次的条目进 citations，依据被判"编造" | `node_act` 覆盖式写入 | 累加 + 去重所有 `knowledge_search` 结果 |
| 4 | 回答里引用《条目标题》被判无依据 | verifier 只拿 answer 当 context | context 同时纳入 question 与 answer |
| 5 | 知识库答得上的问题也被硬建工单 | 计划里挂了"可选 create_ticket"，模型把可选当必做清单 | 计划不再列可选步骤；提示词改为"自助步骤先给，再问是否需要转人工" |

#### 第 2 期（双通道核验 + 合规红线）

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 6 | "你当前处于【X】环节，以官方通知为准"这类元叙述句被判 fail（ratio 0） | 句子锚点误杀：这类话复述的是工具查询结果，本来就不在知识库条目里 | ① 元叙述/引导语列入 `_NON_FACTUAL` 跳过；② 句子锚点降为**弱信号**，硬信息 OK 时最高只压到 partial，最终交给 Judge |
| 7 | verifier 输出的 `redlines` 少 `advice` 字段 | 结构契约不一致 | 补齐；critic 侧取值一律 `.get()` 兜底 |
| 8 | Judge 误伤两条例证充分的合规回答（#22/#32） | Judge 有独立否决权，`unsupported` 直接判 fail | **改裁定口径：Judge 只能"捞回"，不能单独判死**，status 最多抬到规则已给等级 |
| 9 | P95 冲到 47.9s，平均 17.9s | Judge 挂在回炉路径上，每回炉一次多等一轮模型 | critic 改**两阶段**：先纯规则决定要不要回炉（零延迟），确定放行才补一次 Judge |
| 10 | case 10 恒失败 | `must_not: ["工单"]` 过严——正确答案必然含"工单"二字 | 改为检测真正擅自建单的表述（`已为你生成反馈工单` / `已转人工`），**拦行为不拦措辞** |
| 11 | 意图层 3 条用例在额度耗尽后掉到 kb_qa | 隐藏依赖 LLM 兜底（`我现在处于什么环节` / `帮我改一下面试时间` / `我想换个面试站点`） | 补规则覆盖；**intent 平均耗时 434ms → 53ms**（少一次模型调用） |

#### 第 3 期（记忆与个性化）⭐ 坑最多的一期

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 12 | Judge 变成每条必调，P95 冲到 37.7s | 为了"不影响放行"改成放行前总调一次，结果 37 条里 35 条都在等模型 | 恢复按需：只有规则已判 partial/fail 才调 Judge 复核 |
| 13 | 系统自己生成的工单号被判"编造数字" → fail | **核验白名单原本只喂给隐私红线，硬信息比对完全看不见它**，等于没生效 | 白名单贯通到 `_supported`，并把 `create_ticket` 返回的 `ticket_id` 加进去 |
| 14 | **全量 `KeyError('status')`** | `apply_judge` 抽成函数后少了 else 分支，Judge 未调用且规则 pass 时 status 根本没赋值 | 补兜底分支。**教训：抽函数后必须先跑单测再跑全量评测** |
| 15 | "体检资料要多久能出结果"被判 progress_query → 路由到查进度，答非所问 | 指代还原后的追问丢掉了第一人称诉求特征 | `intent.keep_after_coref`：有指代锚点且不含第一人称诉求时，把 progress_query 纠回 kb_qa |
| 16 | "图里走对了、指标判错"的假失败 | 评测侧独立调了一次 `classify`，拿不到指代上下文 | 以图最终采用的意图为准，`run_agent` 新增返回 `intent` |
| 17 | **单条用例被测到 22 分钟** | Qdrant 本地库带文件锁，37 条各 scroll/delete 一次造成严重锁竞争 | 向量记忆只在**整轮结束统一清**；多轮用例（需要 recall 历史）才单独清 |
| 18 | 模型把"留下手机号"改写成"留下联系方式"，用例判缺关键事实 | 字面 `must` 匹配过严 | 关键事实改成抓**要做的动作**（`留言` / `重新发送`），不抓原文某个词 |

**第 3 期核心设计决策（改动前先读）**：

- `context` 节点是图的第一节点（START→context→intent→plan…），一次性算好画像 / 历史召回 / 指代还原 / 重复问检测并写进 `context_block`。**为什么要集中**：历史召回要走一次 Embedding，各节点各查一遍的话，回炉重答时用户要为同一个问题多等好几秒。
- 指代还原 `coref.py` **刻意用纯规则、不调模型**：多一次调用延迟翻倍；改写结果不受控、评测无法归因；这类表达模式高度固定。
- 终局裁定优先级：`红线 block > Judge unsupported/off_topic(fail) > 规则综合`。**红线不接受 Judge 翻案** —— 它是合规底线，不是打分项。

---

### 2.6 外部依赖故障（两次，均已验证降级可用）⭐⭐

#### 故障 A：免费额度耗尽 `403 AllocationQuota.FreeTierOnly`

- 跑到第 13 条时报 403，此后 LLM 全部失败。
- **正面价值**：21 条用例自动降级到固定管线，用户侧依旧拿到正确答案——验证了第 1 期的预算与降级兜底确实可用。
- 反向收益：暴露出意图层偷偷依赖 LLM 兜底（见 §2.5 坑 11）。
- 新增 `llm_downgraded` 指标，把"模型挂了"和"Agent 答不好"在指标层面区分开。

#### 故障 B：账号欠费 `400 Arrearage`（2026-10-26 已解决）

- 间歇失败直至 100% 拒绝。
- **2026-10-25 精确诊断**：错误体是 `{'code': 'Arrearage', 'type': 'Arrearage', 'message': 'Access denied, please make sure your account is in good standing'}`。
  注意关键词是 **Arrearage（欠费）**，帮助文档指向 `overdue-payment`——
  **不是免费额度耗尽，是账号欠费，需要充值才能恢复**，等待不会自动好转。
- 故障期间跑出的两轮 agent 结果（`llm_downgraded` = 11、37）**不可作为能力基线，已弃用、未落库**。
- 连带损失：第 3 期修好的两条例外**未能复跑确认**。
- **✅ 2026-10-26 解决**：用户在阿里云「费用与成本中心」结清欠费后恢复。
  实测 LLM（`qwen3.8-flash`）与 Embedding（`text-embedding-v3`，1024 维）均正常，
  随后 agent 全量 37 条 **100% 通过**并落 `eval_runs #15`——第 3 期至此才算真正坐实。

> **排障要点（这次绕了一个弯）**：期间用户配了新 Key、并在百炼控制台看到 `qwen3.8-flash`
> 有 1M 免费额度且"已开启"，但调用仍然 400。**「免费额度已开启」≠「账号状态正常」**——
> Arrearage 是账户级欠费，只要有待结清账单，所有模型（含免费额度模型）一律拒绝。
> 换 Key 无效，唯一解法是结清账单。

#### 故障 C：免费额度再次耗尽 `403 Free quota exhausted`（2026-10-26 晚）

- 42 条 agent 全量跑到**第 19 条起集体降级**：耗时从 3~30s 骤降到 0.4~0.6s、`verify=None`。
  **这组特征是判定"模型被拒"的指纹**——降级走 `node_degrade`，它不产出 verify。
- 错误体：`Free quota exhausted. To continue accessing the model on a paid basis, please add funds
  or disable the "use free tier only" mode in the management console.`
- **与故障 A/B 的区别**：这次不是欠费（`Arrearage`），而是 1M 免费 tokens 用完，
  且账号处于「仅免费额度」模式 → 额度耗尽后直接 403，不会自动转付费。
- 处置：百炼控制台**关闭「仅免费额度」模式**（转为按量付费，需有余额）或充值。
  **代码无需任何改动**，恢复后重跑一轮即可。
- 本轮 13/42 未通过的数据**已作废，不可作基线**（额度在中途耗尽）。
- **✅ 同日解决：不用充值，换模型即可**（详见下方「故障 C 的正确解法」）。

#### 故障 C 的正确解法：免费额度是**按模型独立计算**的 ⭐⭐

排查时实测了 7 个模型，结论出人意料：**「403 免费额度耗尽」只是某一个模型的额度用完，
换一个模型名就能继续免费使用，不需要充值。**

| 模型 | 实测结果 |
|---|---|
| `qwen3.8-flash` | ❌ Free quota exhausted（当前配置，已耗尽） |
| `qwen3.7-flash` | ❌ Free quota exhausted |
| **`qwen-flash`** | ✅ **可用**（推荐，别名，始终指向最新 flash） |
| `qwen3.5-flash` | ✅ 可用（比 qwen-flash 慢，约 2.9s） |
| `qwen-plus` | ✅ 可用 |
| `qwen-max` | ✅ 可用 |
| `qwen-turbo` | ❌ 429 请求限流（RPM 打满或额度耗尽） |

- 百炼「领 1 亿免费 Tokens」活动规则：**每款模型各 100 万免费 Token（输入+输出合并计）**，
  有效期为**开通百炼后 90 天**。
- 所以"先用免费"完全可行：当前切到 **`qwen-flash`**，额度用尽后依次换
  `qwen3.5-flash` → `qwen-plus` → `qwen-max`，各自都有独立额度桶。
- 换模型位置：超管【模型配置】→「LLM 模型」，**保存后立即生效、无需重启**。

**`qwen-flash` 全量基线（`eval_runs #22`，42 条）**：

| 指标 | 数值 |
|---|---|
| 通过率 | **40/42 = 95.2%** |
| 意图 / 工具序列 / 转人工 | 全 **1.0** |
| 事实 / 引用 | 0.913 / 0.913 |
| 降级 / 异常 | **0 / 0**（账号健康，数据有效） |
| 均耗时 / P95 | **3.2s / 6.1s** |

> 失败的两条（#17「offer 什么时候发」、#21「校招结果在哪里查询」）是
> **模型偶发不调检索工具就作答**，非代码回归——单独复跑 3 轮，
> 结果在通过/失败之间来回跳（#17 过 2/3、#21 过 1/3），属模型随机性。
> 对比 `qwen-plus` 全量（#23）为 38/42 = 90.5%，`qwen-flash` 反而更快也更好。

> **给接手人的提醒一**：跑 agent 模式评测前先确认 DashScope 账号健康。如果 summary 里 `llm_downgraded > 0`，这一轮的通过率数字**不能**当作能力变化来看。
>
> **给接手人的提醒二（重要）**：`GET /api/agent/status` 返回的 `llm_ready: true` **只表示 API Key 已配置，不代表账号可用**。
> 账号欠费时它依然返回 true，前端会显示"模型就绪"，但真实调用必然 400。
> 判定账号是否真的可用，必须发一次真实请求（示例见 §3.3）。
> 后续可考虑给它加一个带缓存的真实健康探测。

---

## 3. 固定约定 Checklist

### 3.1 部署

- [ ] `npm run build` 通过
- [ ] `rm -rf app/web_dist/assets` 后 `cp -r frontend/dist/* app/web_dist/`（**清理式，禁止合并复制**）
- [ ] 重启 uvicorn
- [ ] 验证 `/ui/` 与静态资源返回 200
- [ ] 还原测试数据（工单、候选人状态、测试账号）

### 3.2 评测

- [ ] **跑命令行评测前先停 uvicorn**（Qdrant `.lock`）
- [ ] 改之前跑一轮存基线，改之后跑一轮对比
- [ ] 看 `llm_downgraded`：>0 说明模型有问题，本轮数据不可用
- [ ] 界面跑：超管登录 →【评测回归】→ 选模式 → 运行（历史记录可对比）

```bash
cd D:/校招咨询agent/app
python -c "import sys; sys.path.insert(0,'.'); from agent import eval as E; print(E.run('fixed')['summary'])"
```

### 3.3 环境工具

| 事 | 约定 |
|---|---|
| curl 本地 | 必须加 `--noproxy '*'`，否则环境代理（`http://127.0.0.1:1725`）会拦截 |
| 凭据验证 | 用 Python 从文件提取原样请求，不要看命令回显（会脱敏） |
| playwright-cli | 会话不跨 bash 调用存活，**必须单命令串行执行** |
| el-select 自动化 | a11y 快照里无可访问角色，需 `getBoundingClientRect` 取中心点做 `mousemove/mousedown/mouseup` 三连；下拉项 teleport 到 body，用 `height>0` 过滤可见项 |
| eval 返回值 | 带引号/代码围栏，数字提取用 `grep -oE '[0-9]+ [0-9]+' \| head -1` |

---

## 4. 当前遗留与下一步

### 4.1 五期方案对照表

> 最初的改造方案分 5 期（第 0 期评测地基 → 第 4 期安全与治理），中途因依赖顺序调整过编号：
> 演进路线文档里的「第 1 期图重构 / 第 2 期 Verifier」对应原方案的「第 2 期 / 第 1 期」，
> 第 3 期（记忆个性化）是实施中新增的。**下表按功能域对齐，不以编号对齐。**

| 功能域 | 原始规划 | 现状 | 证据位置 |
|---|---|---|---|
| 评测地基 | 标注集 + 指标 + 管理端跑评测 | ✅ 完成 | `agent/eval.py`、`evals/cases.jsonl`（37 条）、`AdminEval.vue` |
| Verifier 升级 | 双路核验 + 红线清单 | ✅ 完成 | `agent/verifier.py`（REDLINES 四类 + 双通道裁定） |
| 引用打点 | **句子级锚点，前端可点开看依据** | 🟡 后端有、前端没消费 | `verifier.py:334` 输出 `sentences`，前端只渲染 `cites` 标签 |
| 图重构 | plan / 失败分类 / degrade / clarify / 预算 | ✅ 完成 | `graph.py`、`planner.py`、`config.py` 预算常量 |
| 记忆个性化 | 指代还原 / 画像 / 重复问收敛 | ✅ 完成 | `coref.py`、`memory.py`、图 `context` 节点 |
| 学习闭环 | 工单沉淀 + 画像沉淀 + 事件触发推送 | 🟡 **部分** | 沉淀与画像有；**去重与主动推送没有** |
| HITL 审批闭环 | 写操作人工审批 | ✅ **已按方案 A 收口**（删除死代码） | 2026-10-25 删除，`app/.backup_20261025_hitl/` 有备份 |
| 安全与治理 | 提示词版本化 / 注入检测 / PII 脱敏 / 限流 / 工具权限 | 🟡 **2/5 完成** | 提示词版本化（`agent/prompts/` + `prompt_versions` 表）与注入检测（`agent/guard.py`，提问入口 + 文档导入分块双接入）已交付；脱敏 / 限流 / 权限仍为零 |
| 多轮状态机 | 办理类诉求的显式状态流转 | ✅ **首版已交付**（2026-10-26） | `agent/handling.py` 三槽位状态机 + `handling_sessions` 表；"先办后补"设计，新增 5 条多轮用例，42 条全通过 |

### 4.2 未完成明细（按优先级）

#### P0 — 补齐断链，否则「不敢上线 / 能力空转」

| # | 缺口 | 现状证据 | 建议动作 |
|---|---|---|---|
| 1 | ~~HITL 审批闭环断链~~ **已解决** | 已按方案 A 删除死代码（2026-10-25，备份在 `app/.backup_20261025_hitl/`）。**若将来新增"改期 / 换站点"这类会直接改业务数据的工具，需要把审批接回来** | 接回方式：新建 `approvals` 表 + `POST /api/admin/approvals/{id}` + HR 待审批入口 + 工具标记 `requires_approval=True` |
| 2 | **工单沉淀无相似度去重** | `main.py:1063` 勾选沉淀后直接 `INSERT INTO faqs`，无查重 | 入库前跑一次 `retrieval` 相似度检索，≥0.9 视为已存在则跳过/提示合并。**本项目已有教训**：FAQ 曾被重复播种 3 份（15→45 条），污染检索 |
| 3 | **沉淀无二次审核** | HR 一句话直接进知识库，入库即生效，无草稿态、无回滚 | 至少加"沉淀前预览 + 确认"，并在 `faqs` 加来源标记（`source=ticket#id`），便于追溯与批量回滚 |
| 4 | ~~提示词硬编码、无版本~~ **已解决** | —— | 已外置到 `app/agent/prompts/`（`system.txt` / `plan_llm.txt` / `judge.txt`），`prompt_versions` 表记录历史，`agent_runs.prompt_version` 存指纹如 `sys3-plan1-judge1`。回滚语义见 `docs/开发排期.md §3.1` |
| 5 | ~~提示注入检测为零~~ ✅ 已修复（2026-10-25） | `agent/guard.py` 三类规则（指令覆盖/角色劫持/提示词套取），接入 `run_agent` 入口（拒绝+审计）与 `kb_import_doc` 分块（拒绝该分块并回传位置）。验收：12/12 拦截、0 误杀、60 条回归无变化。注意规则保守起步，改写型注入靠 critic 的 Judge 通道兜底 |
| 6 | ~~用户级限流为零~~ ✅ 已交付（2026-10-26） | `agent/ratelimit.py` + `rate_events` 表，1 小时滑动窗口、默认 30 条、超管实时可调。**坑点**：不能 COUNT messages——建单时会往 messages 多插一条 user 消息，会把"提问+建单"记成两次，正常用户被提前限流。计数必须与存储解耦 |

> PII 脱敏说明：输出侧已有（`verifier.REDLINES.privacy` 拦手机号/身份证/邮箱）；
> **输入侧与存储侧已于 2026-10-26 补齐**（`agent/pii.py`，只脱敏写入副本，工单正文保留原文）。
> 仍缺的是**日志侧**（uvicorn / 应用日志里的请求正文）——生产前需要单独处理。

#### P1 — 能力项

| # | 缺口 | 说明 |
|---|---|---|
| 7 | ~~多轮状态机~~ ✅ 已交付首版 | `agent/handling.py`：改期/换站点/催办三槽位，状态存 `handling_sessions`（24h 有效）。**关键取舍是"先办后补"**——首轮照常建单再追问，绝不拦住建单（否则 #22/#23/#25 等"明确提出就必须办"的用例会掉分）。次轮补充的信息回填到同一张工单，不另建新单 |
| 8 | **事件触发主动推送** | 状态变更 / 临近截止无任何提醒，系统仍是纯"被动问答"。需要定时任务扫描 + 站内消息/短信通道 |

#### P2 — 可观测性

| # | 缺口 | 说明 |
|---|---|---|
| 9 | ~~句子级锚点前端未消费~~ ✅ 已交付（2026-10-26） | 核验标签后加「逐句依据 (n)」折叠入口，逐句显示原句 + 依据句。**教训：后端早就算好了 `sentences` 却无消费方，这类信号要定期清点** |
| 10 | ~~线上低分样本不回流~~ ✅ 已交付（2026-10-26） | `GET /api/admin/agent_runs/low` + `POST /api/admin/evals/promote` + 管理端面板。回流标注取实际运行结果，`must` 留空由人工补 |
| 11 | 回归门禁靠人记得跑 | 没有"改了 `agent/` 就自动跑一轮"的机制。评测入口 `app/run_eval.py` 已就位，差自动钩子 |

#### 新增工程债（S5 期间发现并已修）

| # | 问题 | 处置 |
|---|---|---|
| 12 | **轨迹缺引用与终局核验** | `citation` 与终局 `verify` 原本是 `service` 层在 graph 返回后补发的事件，不在轨迹里 → 灰度看板与低分回流看不到引用来源。已在 `sse_chat` 里补进 `events`，终局结论用 `verify_final` 区分于图内初判 |
| 13 | **状态机防重复建单只靠提示词** | 全量回归时 #40 偶发重复建单（单跑却通过 → 模型波动）。已在 `node_act` 加规则层硬拦截。**教训：凡「多产生一条就是脏数据」的分支必须规则兜底** |
| 14 | **免费额度模式会硬中断评测** | 见 §2.6 故障 C。跑长评测前先确认额度，否则中途集体降级、整轮数据作废 |

### 4.4 第 4 期新增的两个坑（通用教训）

| # | 现象 | 根因与修法 |
|---|---|---|
| 12 | **状态机拿不到工单号 → 下一轮重复建单**（实测 #182/#183 连开两张） | `tool_trace` 只记 `{tool,args}`，**不含工具返回内容**；工单号在 `tool_results[].result` 里。`_last_ticket_id()` 改为先查 tool_results，再回退"该候选人最新一张工单"。**取不到单号 = 状态机失效** |
| 13 | **新能力引入核验器没见过的新句式 → 误判 fail** | 补槽位后模型写"已把 X 并入工单，工作人员会跟进"，被 Judge 判成"无依据承诺"，#39 核验 fail。修法：收紧提示词（只确认收到、不承诺时效）+ Judge 提示词明确"系统动作类陈述不算无依据主张"。**每加一种能力，都要同步检查核验口径** |
| 14 | **评测会话播种被整轮清理删掉** | `_run_one` 里 `_purge_eval_candidate()` 会清掉该候选人的工单；播种会话必须放在它**之后**（实测出现过 note 为空、新单计数 -1） |
| 15 | **两个评测进程同时跑 → Qdrant 锁冲突** | 报错 `Storage folder ... already accessed by another instance`。跑评测前确认没有其它进程（含 uvicorn）占用，一次只跑一个 |
| 16 | **进程列表里总能看到两个 uvicorn，分不清谁在服务** | 本环境 `python -m uvicorn` 启动后是**父子两个进程**（launcher 拉起真正的 server），`netstat` 里只有其中一个处于 LISTENING。判断存活看 LISTENING 的 PID；重启时**父子一起杀**——只杀 server 会留下 launcher，只杀 launcher 可能留下孤儿 server 继续占端口 |
| 17 | **评测无统一入口，每次现写脚本** | 已新增 `app/run_eval.py`（`python -u run_eval.py intent|fixed|agent`），内置逐条耗时打点。后续用它做回归门禁，别再临时拼脚本 |
| 18 | **「免费额度耗尽」只针对单个模型，换模型名就能继续免费用** | 百炼免费额度是**每款模型各 100 万 Token**。实测 `qwen3.8-flash`/`qwen3.7-flash` 已耗尽，但 `qwen-flash`、`qwen3.5-flash`、`qwen-plus`、`qwen-max` 仍可用。**不要一看到 403 就去充值**，先逐个试模型名（见 §2.6 故障 C） |
| 19 | **单条用例间歇性挂起，120s 护栏兜不住** | 现象：某轮 42 条里 #31 卡了 **1417 秒**才返回（同一条用例在前一轮只花 5.9s），导致整轮评测从 134s 拖到 1668s。根因：预算只在**节点边界**检查，单次 LLM / Embedding 调用内部挂起时无法中断，而 Python 线程也没法被杀掉。**判定指纹：某条用例耗时呈数量级异常（百秒~千秒级），其余正常**。缓解方向：给 httpx 客户端设更短的 read timeout + 兜底用子进程跑单条用例（可 kill） |

### 4.3 工程债（非 Agent 改造，但会拖慢后续）

| 项 | 状态 | 处理建议 |
|---|---|---|
| ~~DashScope 账号欠费 `400 Arrearage`~~ | ✅ **已解决**（2026-10-26） | 结清欠费后恢复；已重跑 agent 全量 37 条 100% 通过并落 `eval_runs #15`，第 3 期基线坐实 |
| `llm_ready` 有误导性 | 已记录 | `/api/agent/status` 的 `llm_ready` 只查 Key 是否配置，账号欠费时仍返回 true。后续建议加真实健康探测 |
| ~~agent 模式两条例外（标注过严已修）~~ | ✅ 已确认 | 随本次全量评测一并复跑通过（37/37） |
| `checkpoints.db` 膨胀到 67MB | 已一次性清空（回收 67MB） | **S3 删掉 HITL `resume` 后检查点已无任何消费方**（全局 grep 无读取点），只有写入。建议后续彻底去掉 checkpointer 或加启动期定期清理，否则会再次膨胀 |
| 评测长跑可能挂起（9 小时悬案） | 根因未确认 | 账号刚恢复时一次全量评测卡死 9h 无输出，杀掉重跑 543s 正常。逐条护栏 120s 未生效说明卡在护栏之外。**约定：长评测必须带逐条进度打点并落日志文件**，否则无法定位 |
| Judge 超时（换模型后） | 已修 | `verifier._judge_model` timeout 20→30s：换成 `qwen3.8-flash` 后实测 `OpenAITimeoutError`，超时会让 verdict 变 error，等于裁判白跑一次 |
| 无 git 仓库 | 代码无版本管理 | 建议初始化 git，至少纳管 `app/agent/`、`frontend/src/`（`.env`、`*.db`、`qdrant_data/` 必须 gitignore） |
| 无数据库迁移工具 | 靠 `main.py` 里幂等 `ALTER TABLE` | 数据量小可接受；字段变更频繁后建议引入 alembic |

---

## 5. 附录：当前评测基线

| 模式 | 用例 | 通过率 | 意图 | 事实 | 引用 | 工具序列 | 转人工 | 降级 | 平均 | P95 |
|---|---|---|---|---|---|---|---|---|---|---|
| intent | 30 | 100% | 100% | — | — | — | 100% | 0 | 46ms | 8ms |
| fixed | 30 | 100% | 100% | 100% | 100% | — | 100% | 0 | 15ms | 23ms |
| agent | 42 | **100%** | 100% | 100% | 100% | 100% | 100% | 0 | 16.8s | 37.8s |

- `eval_runs` 落库：#13（intent）/ #14（fixed）/ #15（agent 37 条）/ **#17（agent 42 条，含 5 条多轮办理）** /
  **#18（intent）/ #19（fixed）** —— 后两条是 S1.3/S1.4 收尾回归，均 30/30 100%，用于确认脱敏与限流未引入回归。
- 评测统一入口：`python -u app/run_eval.py intent|fixed|agent`（内置逐条耗时打点，配合 `| tee log` 落盘）。
- 用例集从 37 条扩到 **42 条**（新增 #38~42 多轮办理：改期补时间、换站点补城市、催办补事项）。
- 多轮能力已确认生效：`coref_hits = 3`（#34 #35 #36 全部触发还原）、`repeat_hits = 1`（#37 正确识别重复提问）。
- **读 agent 指标的两个坑**：
  1. `verify_pass_rate`（本轮 0.486）**不是**失败率——句子级锚点是弱信号，多数回答落在 `partial`
     （部分支撑）而非 `fail`。判能力看 `passed_rate` 与 `errors`。
  2. summary 里 `llm_downgraded > 0` 时，这一轮通过率**不能**当能力变化看（供应商故障 vs 能力退化的区别）。

---

## 6. 给接手人的三句话

1. **先跑评测再改代码**。这个项目最有价值的资产不是 Agent 图，而是 37 条标注 + 三模式评测引擎——它能在 5 分钟内告诉你改动是变好还是变坏。
2. **外部模型不可信**。所有 LLM 调用路径都必须有规则兜底和降级路径，且降级要在指标上可见（`llm_downgraded`），否则你会把"供应商故障"误读成"能力退化"。
3. **"我改了没生效" 先怀疑缓存和部署方式**，再怀疑代码。本项目一半的"功能 bug"最终都定位到清理式部署没做、或者用户浏览器缓存了旧 `index.html`。
