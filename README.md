# 股权穿透采集分析系统

一个仿工商公示网站的股权穿透尽调系统。从一家公司出发自下而上逐层抓取股东，
自动识别同一企业的不同写法、区分同名自然人、求解交叉/环形持股的最终权益、
按可调规则判定实际控制人，并支持股东变更的任意日期回放。

纯 Python `asyncio` + `aiohttp` 实现，前端零构建（原生 JS + 自绘 SVG），
除 `aiohttp` 外全部使用标准库（含手写高斯消元与 Tarjan 强连通分量）。

---

## 一、快速开始

```powershell
# 0. 安装唯一第三方依赖
py -3 -m pip install -r requirements.txt

# 1. 一键：生成数据 + 起仿真站 + 初始化仓库 + 起 Web
./run_all.ps1
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 仿真工商公示站 | http://127.0.0.1:8080 | 3000 家公司、虚拟时钟、账号配额 |
| 采集分析 Web | http://127.0.0.1:8000 | 任务/企业库/股权图/实控人/配额/核对/规则 |

手动分步：

```powershell
py -3 -m equity.sim.generate          # 生成 3000 家关系网 -> data/sim_disclosure.db
py -3 -m equity.sim.server            # 起仿真站（:8080）
py -3 -m equity.crawler.runner init   # 初始化采集仓库 + 同步账号
py -3 -m equity.crawler.runner crawl "公司名或ID" 3   # 命令行发起深度3的穿透任务
py -3 -m equity.web.app               # 起 Web（:8000）
```

全量采集 3000 家并逐家核对实控人：

```powershell
py -3 tests/crawl_all.py             # 全量采集（约 70 秒）
py -3 tests/run_verify.py            # 引擎实控人 vs 仿真站预设答案
```

---

## 二、目录结构

```
equity/
  config.py              全局配置（端口、配额、虚拟日时长、默认判定规则）
  db.py                  SQLite 线程局部连接 + asyncio.to_thread 异步封装
  identity.py            企业名归一化、自然人消歧（需求 3、4）
  sim/
    schema.py            仿真站与采集仓库的建表 DDL
    world.py             3000 家关系网随机构造（环/代持/表决权/同名/变更）
    generate.py          落库 + 全量股东表闭合校验
    server.py            仿真公示站（虚拟时钟、配额中间件、两种详情页）
    inspect.py           数据体检脚本
  crawler/
    accounts.py          账号池：轮用、429 冷却、虚拟日感知自动复活（需求 2）
    client.py            HTTP 客户端：自动取号、429 换号、退避重试
    engine.py            BFS 穿透引擎、实体归并、断点续爬、缓存复用（需求 1、3）
    seed.py              仓库初始化与账号种子
    runner.py            采集命令行
  analysis/
    linalg.py            带部分主元的高斯-约当消元
    history.py           变更事件回放（任意日期股东表，需求 7）
    graph.py             构图、Tarjan SCC 环检测、环内线性方程组求解（需求 5）
    controller.py        实控人判定与链条递归追溯（需求 6）
    service.py           网页/核对共用的只读分析服务
    verify.py            与预设答案逐家核对
  web/
    app.py               aiohttp Web API
    static/index.html    单页前端（七个 Tab，SVG 股权图）
tests/                   逻辑单测、冒烟、覆盖采集、配额恢复、核对脚本
```

---

## 三、七项需求如何落地

### 1. 逐层抓股东、深度可设
`crawler/engine.py` 用持久化队列做 BFS：根公司深度 0，每发现一个公司型股东
即以 `depth+1` 入队继续向上；`max_depth` 之外的节点标记 `skipped_depth`。
自然人是叶子。队列落库，进程中断后重跑同一任务自动续爬（已完成节点跳过）。

### 2. 多账号轮用配额、用完歇到第二天
- `sim/server.py` 配额中间件按 `账号 × 虚拟日` 计数，超限返回 `429 + Retry-After`。
- `crawler/accounts.py` 轮询取号（round-robin），收到 429 即把账号标记为
  当日耗尽并冷却；后台每 20 秒打一次不耗配额的 `/health` 感知虚拟时钟，
  一旦翻日自动复活全部账号、清零用量，任务无需重启即可续跑。
- 确定性验证见 `tests/test_quota_recovery.py`（压到 429 → 推进虚拟日 → 自动恢复）。

### 3. 同一企业的不同写法识别为一家
`identity.py` 做名称归一：全角/半角括号统一、NFKC、去空白、剥离
「有限公司/有限责任公司/股份有限公司」等后缀。但**实体合并只认统一社会
信用代码/企业 ID**，归一化名仅用于搜索召回——避免把仅后缀不同的两家独立
公司错误并表（该约束有真实踩坑修复记录）。

### 4. 同名自然人靠证件尾号区分
自然人主键 `person_key = 归一姓名 + 证件尾号`。仿真数据里制造了 2000+ 组
同名不同尾号的自然人；采集落库与穿透全程携带 `id_tail`，前端受益人表展示尾号。

### 5. 交叉/环形持股求最终比例且不死循环
`analysis/graph.py`：
- Tarjan 强连通分量识别环（迭代实现，无递归深度问题）；
- 缩点后的 DAG 按拓扑序直接传播；
- 环内 SCC 建立 `(I − W)x = b` 线性方程组，用手写高斯消元求解，
  规模通常只有 2~5 个节点；奇异时退化为不动点迭代兜底。
手算数值钉在 `tests/test_logic.py`（三公司环、两公司交叉、线性链共 43 项断言）。

### 6. 按穿透比例与表决权判实控人、规则可调
`analysis/controller.py` 分级判定：绝对控制（>50% 表决权）→ 共同控制
（多人达共同线且无决定性领先）→ 相对控制（最低表决权 + 领先幅度）→ 无控制；
公司型控制股东递归追溯到最终自然人；代持按规则决定是否穿透。
所有阈值存 `rule_configs` 表，网页「规则配置」Tab 可改并即时生效。
环内无过半数控股股东时判无实控人，避免沿环无限追溯。

### 7. 股东变更按时间回放、看任意一天结构
`analysis/history.py` 以半开区间 `event_date ≤ as_of < valid_to` 回放事件流。
`change` 模式的企业仿真站只给变更记录，采集端自行回放得到当前股东；
网页股权图页可输入任意日期回放当天股东表（`/api/companies/{id}/cap-table`）。

---

## 四、仿真数据的特殊结构

`sim/world.py` 在 3000 家公司里刻意埋入：

- 28 个多层集团（创始人 → 控股平台 → 产业公司，3~6 层）；
- 一个三家公司纯环（无实控人）+ 5 处集团内交叉持股（不破坏控制权）；
- 8 处股权代持（名义股东 + 实际权益人）；
- 5 处表决权委托（股权与表决权不一致，按表决权口径判控制）；
- 2000+ 组同名不同尾号自然人、200+ 条企业别名；
- 约一半企业只公示变更记录（`change` 模式），需回放；
- 350 家企业有真实股权转让历史。

每家公司生成时都断言股东表股权与表决权合计严格等于 100%，
并写入预设实控人答案 `preset_controllers` 供核对。

---

## 五、测试与验收

```powershell
py -3 tests/test_logic.py         # 43 项纯逻辑单测（环求解/回放/归一/代持/判定）
py -3 tests/smoke_sim.py          # 仿真站冒烟（登录/搜索/两种详情/429/虚拟时钟）
py -3 tests/test_quota_recovery.py# 配额耗尽与次日自动恢复
py -3 tests/crawl_all.py          # 3000 家全量采集
py -3 tests/run_verify.py         # 全量核对，输出准确率与不一致明细
```

当前全量核对结果：**3000 / 3000 一致，准确率 100%**（报告落库
`verify_reports` 表，网页「核对验收」Tab 可查）。

## 六、配置

全部集中在 `equity/config.py`，支持 `EQS_` 前缀环境变量覆盖，例如：

```powershell
$env:EQS_VIRTUAL_DAY_SECONDS = 60   # 加快虚拟时钟便于观察配额翻日
$env:EQS_DAILY_QUOTA = 100
```
