# A股分析平台

围绕两件事构建的个人研究工具：**采集可检查的数据，生成能回看证据的分析**。

独立编写的 Vue 3 界面与 Python 服务，整合本项目维护者原有的 `market_diary` 收盘研究脚本。未复制 TradingAgents-CN 的前端、后端或品牌资源。不是其官方版本或改名发行版；Qlib 不是运行依赖。

## 已实现

- 数据中心：六指数、市场广度、行业/概念代理、指定股票/指数历史日线，股票财务摘要及新闻、独立黄金报价。
- 分析工作台：收盘研究、指定标的研究、历史资料研究，证据查看、报告下载、独立演示。
- 主分析 → 审阅 → 最多一次修订，调用次数和输出长度受限。修订后未再审阅的内容保持草稿，不自动入账。
- 正式行情必须 `ready + passed`；缺失数据保留缺失，不补零。历史日期不会补写正式预测。
- 新预测执行 4 个指数、3–5 个板块、5 只股票及六因素验证；同号非零区间、下一交易日校验、同日正式文件不可覆盖。
- 昨日预测与实际结果逐项评价，保留方向/区间命中率与 MAE。旧版预测可以读取，历史缺口明确展示。
- 持久任务队列、取消、失败重试、进程中断标记、可选每日收盘任务。重试生成新任务，复用已冻结证据与参数一致的成功模型步骤；未成功的请求可能重复计费。
- 单用户登录、本地凭据、密钥不回显。默认只监听本机，不提供公网部署方案。

## 启动

需要 Docker Desktop / Docker Compose：

```bash
docker compose up -d --build
cat runtime/admin-credentials.txt
```

打开 <http://127.0.0.1:3090>，使用生成的管理员账号。密码不是固定的 `123456`。在设置中填入兼容 Chat Completions JSON 模式的模型地址、模型名和 API 密钥。默认模型配置仅是可编辑占位，未附赠服务额度。

没有模型密钥也可采集、查看归档或体验离线演示。演示为合成资料，不进入正式行情、预测或评分。

```bash
docker compose logs --tail 100 web worker
docker compose down
```

所有私有数据与密钥保存在 `runtime/`，已排除出 Git 和镜像构建上下文。不要分享该目录；备份时可停止服务后备份整个目录。

## 本地开发

Python 3.12、Node 22+；macOS 或 Linux：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm ci --prefix frontend
npm run build --prefix frontend
bash scripts/run_local.sh
```

导入原有个人研究项目的台账（仅本地读取，源文件不变，冲突拒绝覆盖）：

```bash
.venv/bin/python scripts/import_legacy.py /absolute/path/to/legacy-project
```

验证：

```bash
.venv/bin/python -m unittest discover -s tests
npm run build --prefix frontend
```

旧个人历史台账不公开，依赖该台账的回归用例在未提供文件时明确跳过；其余合成资料测试独立运行。

## 数据边界

- 行情网络接口由腾讯、新浪、东方财富及 AkShare 提供，可能限流或变更。采集失败需重试或排查网络，不代表价格为零。
- 日线目前为未复权数据，除权除息会造成跳变。指标会标注此限制。
- 财务摘要与新闻缺少完整历史可见时间，不能当成严格的历史回测数据。历史研究不使用今天新抓取的财务/新闻冒充过去证据。
- XAU/USD 需自行配置 GoldAPI 密钥；Au99.99 经 AkShare 获取上金所历史日线，可能早于研究日期。两者单位、时间和状态分开标注，不用黄金股票代替黄金价格。黄金暂不进入正式评分。
- 请求证据保存原始响应或大响应哈希；只覆盖 requests 网络路径，其他客户端只保存标准化结果。导入历史不会补造原始响应。
- 模型审阅不等于事实绝对正确。结构、来源引用与预测契约由程序校验，自由文本的所有数字/因果尚无完全自动化核验；请查看证据和缺失提示。
- 默认调用预算最多 3 次，但不等于货币费用上限，实际费用由模型服务商决定。中断重试可能重复计费。
- 当前交易日历覆盖 2026 年；跨年使用前必须更新日历，不能猜测节假日。

计划与技术说明见 [docs/PLAN.md](docs/PLAN.md)。当前不提供货币费用硬上限、完整历史可见时间数据或自动跨年日历。

不构成投资建议。

## 使用本机 Codex CLI 分析

设置 → 分析引擎 → **本机 Codex CLI** → 保存。原来的模型 API 模式仍然可用。CLI 模式不需要在平台填写模型 API Key；使用 Mac 上 `codex login` 的登录状态和相应额度，仍需联网。

首次在 Mac 上安装本机执行服务（已验证 Codex CLI 0.135.0）：

```bash
codex login
.venv/bin/python scripts/codex_service.py install
```

这是当前用户的 LaunchAgent，登录 Mac 时自动启动。平台原有 Docker 启停方式和 **3090** 端口不变；`web`、`worker` 继续留在 Docker，本机执行服务负责调用 CLI。不添加网络端口，不把 `~/.codex` 或 Docker socket 挂载到容器。

设置里会显示“本机 Codex 已就绪”。“测试已保存的分析引擎”发起一次简短真实请求，成功或失败记录在任务列表；它也消耗所选服务的额度。先保存修改，再测试。Codex 模型留空使用 CLI 内置默认模型，也可填当前账号支持的模型名。

进程使用独立临时目录、只读沙箱及结构化输出；不加载个人 config.toml、插件、Hooks 和项目指令，禁用 shell、浏览器和多代理等工具。只把冻结的研究证据传给 Codex，结果仍通过原有校验及预测登记流程。CLI 不采用 API 模式的输出 Token 参数，以单次时间、任务总时间和调用次数限制执行；实际用量写入本地步骤记录。

任务文件通过已挂载的 `runtime/codex_bridge/` 交换。取消任务立即请求终止；容器停止导致租约失效，本机进程在约 15 秒内发现并终止（强制结束最多再需 3 秒）。本机服务崩溃后不会自动重复已开始的请求，平台可以手动重试；已发生的调用仍可能消耗额度。休眠期间无法持续执行。

维护命令：

```bash
.venv/bin/python scripts/codex_service.py status
.venv/bin/python scripts/codex_service.py uninstall
# 更换项目目录、Python 或 CLI 安装位置后，重新运行 install。
```

其他系统可在宿主机保持 `python -m backend.codex_host` 运行，并让服务与容器使用同一个 runtime 目录。Codex 登录与鉴权问题应在宿主机解决，不要把登录文件复制到镜像。新的安装默认仍为 API 模式，现有配置通过默认值兼容。
