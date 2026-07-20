# 薪酬一体化平台 — 建设蓝图

> 目标：为连锁餐饮企业（约 100 家门店、跨广深莞珠多区域）构建一套**全新独立**的一体化薪酬系统，覆盖**核算 + 管理 + 查询**三大支柱。数据库为权威来源，系统内录入/维护/审批，Excel 仅作历史迁移与批量导入辅助通道。承载全公司敏感薪资数据，需 RBAC 权限、审计、多组织。
>
> 技术栈：FastAPI (Python 3.12) + PostgreSQL 16 + React (TypeScript/Vite)。
>
> 参考来源：现有 `salary_search_app/server.py` 的 Excel 解析逻辑（表头归一化、门店别名、去重）与 138MB 历史缓存数据，**仅迁移逻辑与数据，不继承其架构**。

---

## 0. 执行模式与约定

- **Git 模式**：当前目录非 git 仓库、无 `gh`。新项目在 **S1 内 `git init`**，此后为**本地 git direct 模式**：每步一个分支、每步一次（或多次）commit，**无 PR / 无远程**。合并即 `git switch main && git merge --no-ff <step-branch>`。
- **项目根**：`C:\Users\Administrator\Downloads\salary_search_app\compensation-platform`（下称 `<ROOT>`），与旧 `salary_search_app` 平级隔离。
- **回滚**（direct 模式）：代码回滚 = `git revert` / 弃用分支；数据回滚 = Alembic `downgrade` + 迁移前 `pg_dump` 快照；线上 = 特性开关（feature flag）关闭未完成模块。
- **每步定义**：一个 PR 大小、可独立冷启动执行。每步含「上下文简报 / 任务清单 / 验证命令 / 退出标准 / 回滚」。
- **强模型步骤**（S3/S6/S11/S12）：安全或薪资正确性关键，须用最强模型执行 + 对抗性复核。

---

## 1. 全局不变量（Invariants — 每步完成后都必须成立）

这些是从旧系统的教训中固化出来的硬约束，任何步骤不得违反：

1. **金额一律 `Decimal` / PostgreSQL `NUMERIC(14,2)`，禁止 `float`。**（旧系统 float 累加对账差分）
2. **解析/导入失败不得静默归零或静默丢行**：无法解析的金额、缺失的必填项一律进入错误清单并阻断确认，不写脏数据。（旧系统 `number_value` 静默返回 0）
3. **员工身份唯一键含组织维度**：去重/匹配以 `(计薪周期, 员工工号)` 为准，工号全局唯一；姓名仅作展示，绝不作为身份键。（旧系统去重键 `(月份,姓名)` 误删同名不同店员工）
4. **认证 fail-closed**：未配置有效凭据 = 拒绝启动/拒绝访问，绝不放行。（旧系统空口令 fail-open）
5. **全量审计**：所有写操作、登录、导出、薪资查看均写 append-only `audit_log`（谁/何时/何源 IP/对象/前后值摘要）。
6. **RBAC 组织范围强制**：任何数据查询在仓储层按会话主体的组织范围注入过滤，越权不可达。
7. **敏感 PII（身份证、银行卡）列级加密存储**，日志/导出默认脱敏。
8. **无明文口令落盘**：口令 Argon2 哈希；配置密钥走环境变量 / `.env`（`.gitignore` 排除），不入库不入仓。
9. **导出防公式注入**：xlsx 文本单元格以 `= + - @` 开头时转义为纯文本。
10. **测试门槛**：后端行覆盖 ≥ 80%，核算/社保/个税等纯计算模块 ≥ 95%；关键流程有 E2E。
11. **计算可追溯**：每个员工每期的薪资结果可展开为逐项公式明细（component → 取值 → 公式 → 结果），核算规则版本化。

---

## 2. 目标架构

```
┌────────────────────────── 前端 (React + TS + Vite + Ant Design) ──────────────────────────┐
│  登录/RBAC菜单 │ 主数据管理 │ 薪酬结构/调薪审批 │ 考勤录入 │ 核算运行 │ 查询/工资条 │ 看板 │ 导出 │
└──────────────────────────────────────────┬──────────────────────────────────────────────┘
                                            │ HTTPS (JWT access + httpOnly refresh cookie)
┌──────────────────────────────────────────▼──────────────────────────────────────────────┐
│  FastAPI                                                                                    │
│  routers/  ── deps: 认证 + RBAC组织范围 + 审计中间件                                         │
│  services/ ── 业务编排（核算引擎/社保个税/审批状态机/导入管线/看板聚合）                       │
│  repositories/ ── SQLAlchemy 2.0，组织范围过滤在此强制                                       │
│  domain/   ── 纯计算（component 引擎、社保、个税），无 IO，可单测                             │
└──────────────────────────────────────────┬──────────────────────────────────────────────┘
                                            │ SQLAlchemy + Alembic
┌──────────────────────────────────────────▼──────────────────────────────────────────────┐
│  PostgreSQL 16  ── 组织树 / 员工 / 用户RBAC / 薪资结构 / 考勤 / 核算结果(NUMERIC) /          │
│                    社保个税政策 / 调薪审批 / 导入暂存 / audit_log(append-only)               │
└─────────────────────────────────────────────────────────────────────────────────────────┘

辅助：openpyxl（Excel 导入/导出）│ pytest/Vitest/Playwright（测试）│ Docker Compose（本地）│
       生产：TLS 反向代理(Caddy/nginx) + pg_dump 定时备份
```

**技术选型（具体）**

| 层 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI + Pydantic v2 | 类型安全、自带 OpenAPI、异步 |
| ORM/迁移 | SQLAlchemy 2.0 + Alembic | 成熟、可版本化 schema |
| 数据库 | PostgreSQL 16，金额 `NUMERIC` | 事务、并发、精确小数 |
| 认证 | Argon2 口令 + JWT(access) + httpOnly refresh cookie | 无明文、可过期、防 XSS 窃取 |
| 授权 | 自研 RBAC + 组织范围依赖注入 | 可审计、贴合门店层级 |
| 前端 | React + TypeScript + Vite + Ant Design + TanStack Query + React Router | 企业数据密集型后台首选 |
| 后端测试 | pytest + pytest-cov + Testcontainers(Postgres) | 真库集成测试 |
| 前端测试 | Vitest + React Testing Library | 组件/逻辑单测 |
| E2E | Playwright | 关键流程 |
| Excel | openpyxl（复用解析逻辑） | 与旧系统一致 |
| 批量核算 | 先同步带进度；量大再引入 RQ/Celery + Redis | 渐进 |
| 本地环境 | Docker Compose（postgres + backend + frontend） | 一键起 |
| 代码质量 | ruff + black + isort + mypy / eslint + prettier + pre-commit | 强制风格 |

---

## 3. 核心数据模型（S2/S3 落地，后续步骤扩展）

- **组织**：`org_unit(id, parent_id, type[集团/区域/门店], name, code, city, status)` — 自引用层级树；`city` 决定社保口径。
- **员工**：`employee(id, emp_no[唯一], name, org_unit_id, position_id, employment_type[全职/兼职小时工/劳务], probation_end, hire_date, leave_date, status, id_card_enc, bank_account_enc, social_city)`。餐饮行业兼职小时工占比高，`employment_type` 决定计薪模式（月薪制 vs 时薪制）与社保口径，是一级维度。
- **RBAC**：`user`、`role`、`permission`、`user_role`、`role_permission`、`user_org_scope`（用户可见组织范围）。
- **职级/薪档**：`job_grade(职级)`、`salary_band(带宽: grade → min/mid/max)`。
- **薪资结构**：`salary_component_def(组件目录: code,name,type[基本/绩效/岗位/补贴/加班/扣款],taxable,in_social_base,in_housing_base)`、`employee_salary_structure(employee,component,amount,effective_from,effective_to)` 生效日期化。
- **计薪周期**：`pay_period(year_month, status[open/calculating/closed/paid])`。
- **考勤/绩效**：`attendance_record(employee,period,应出勤,实出勤,加班时长,请假,...)`、`performance_record(employee,period,绩效系数/得分)`。
- **核算**：`payroll_run(period,org_scope,status,created_by)`、`payroll_result(run,employee,period,gross,net,各component明细JSON,social_breakdown,housing,tax)` 全 `NUMERIC`。
- **政策**：`social_insurance_policy(city,effective,养老/医疗/失业/工伤/生育/公积金 各费率+基数上下限)`、`tax_config(累计预扣税率表,起征点,专项附加扣除口径)`。
- **调薪审批**：`salary_adjustment(单据)` + 通用 `approval_flow/approval_step/approval_instance`。
- **预算**：`labor_budget(org,period,编制人数,人力成本预算)`。
- **导入**：`import_batch`、`import_staging_row`（暂存 + 校验状态）。
- **审计**：`audit_log(actor,action,target_type,target_id,ip,before,after,ts)` append-only。

**角色（初版）**：超级管理员 / 集团HR / 区域HR经理(区域范围) / 店长(门店范围,只读+考勤录入) / 财务(核算发放) / 审计(全局只读+审计日志) / 员工(仅本人工资条)。

---

## 4. 步骤总览与依赖图

```
Phase A 地基        Phase B 主数据/迁移     Phase C 薪酬管理      Phase D 核算            Phase E 查询/报表   Phase F 加固
────────────        ──────────────────     ──────────────      ───────────           ────────────────   ─────────
S1 脚手架 ─┬─ S2 数据模型 ─┬─ S5 主数据CRUD ─┬─ S7 薪资结构 ──┬─ S8 调薪审批流         S14 查询/自助
           │              │               │               └─ S9 成本预算            S15 看板分析
           ├─ S3 认证RBAC ─┤               ├─ S6 Excel导入/迁移                      S16 导出/报表
           └─ S4 审计/配置 ─┘               │
                                           └─ S10 考勤绩效 ─ S11 计算引擎 ─ S12 社保个税 ─ S13 核算发放
```

**依赖边**：S1→(全部)；S2,S3,S4→应用层各步；S5→S6,S7,S9,S10；S7→S8,S9,S11；S10→S11；S11→S12→S13；S13→S14,S15,S16；(全部)→S17。

**可并行**：
- S3 与 S4 可与 S2 完成后并行。
- S5 与 S7 的「组件目录」部分可并行（S7 目录不依赖员工）。
- S9、S10 在 S5/S7 后可并行推进。
- S14/S15/S16 在 S13 后可并行。

**模型分配**：S3/S6/S11/S12 用**最强模型**；其余默认模型。

---

## 5. 步骤明细

> 每步均假设执行者**冷启动**、只读本步简报即可动手。通用前置：`cd <ROOT>`，`git switch -c step/<n>-<slug>`，完成后跑该步验证命令，全绿再合并到 `main`。

### Phase A — 基础地基

#### S1 · 仓库脚手架与开发环境
- **上下文**：全新 monorepo，`<ROOT>` 下 `backend/`(FastAPI) + `frontend/`(React+Vite+TS) + `deploy/`(docker-compose) + `plans/` + `docs/`。此步只搭骨架不写业务。
- **任务**：
  1. `git init`；写 `.gitignore`（`.env`、`*.sqlite`、`node_modules`、`__pycache__`、`*.xlsx`、`.venv`、`dist`）。
  2. `backend/`：`pyproject.toml`（fastapi、uvicorn、sqlalchemy、alembic、pydantic-settings、argon2-cffi、python-jose、openpyxl、pytest、ruff、black、mypy）；`app/main.py` 带 `/health`；`app/core/config.py`（pydantic-settings 读 env）。
  3. `frontend/`：Vite React-TS 模板 + Ant Design + TanStack Query + React Router + axios；`.env` 指向 `/api`。
  4. `deploy/docker-compose.yml`：postgres:16 + backend + frontend；`.env.example`。
  5. `pre-commit` 配置（ruff/black/isort/mypy + eslint/prettier）；`Makefile`/`justfile` 常用命令。
  6. CI 占位（本地 `make ci` 跑后端+前端 lint/test）。
- **验证**：`docker compose up -d` 后 `curl localhost:8000/health` 返回 200；前端 `localhost:5173` 可开；`make ci` 通过。
- **退出标准**：三容器起、健康检查绿、lint 空、空测试套件跑通。
- **回滚**：删除分支。

#### S2 · 核心数据模型与迁移
- **上下文**：落地第 3 节的**基础** schema（组织/员工/职级薪档/计薪周期），为后续所有功能提供表结构。金额列 `NUMERIC(14,2)`。
- **任务**：
  1. SQLAlchemy 2.0 模型：`org_unit`(自引用树)、`employee`、`job_grade`、`salary_band`、`pay_period`。
  2. Alembic init + 首个迁移；`base` 通用列（id、created_at、updated_at、created_by）。
  3. `seed.py`：从旧 `config.json` 的 `store_aliases`/门店清单生成组织树种子（区域→门店），城市字段初值。
  4. 仓储层基类（分页、软删、组织范围过滤挂钩预留）。
- **验证**：`alembic upgrade head` 成功；`alembic downgrade -1` 可回滚；`pytest tests/models` 建表+CRUD 通过；seed 后组织树查询正确。
- **退出标准**：迁移可升可降，模型单测绿，种子组织树含区域/门店层级。
- **回滚**：`alembic downgrade base` + 弃分支。

#### S3 · 认证与 RBAC 【强模型 · 安全关键】
- **上下文**：全公司敏感薪资的第一道门。**fail-closed**：无有效口令拒绝启动。旧系统的教训（明文弱口令、无限速、token 永不过期、fail-open）全部反向修复。
- **任务**：
  1. `user/role/permission/user_role/role_permission/user_org_scope` 模型 + 迁移。
  2. Argon2 口令哈希；登录 `POST /api/auth/login`（限速：IP+账号失败计数、指数退避/锁定）。
  3. JWT access（短时效，如 15min）+ refresh（httpOnly、Secure、SameSite=Lax cookie，可服务端吊销）；`/refresh`、`/logout`（吊销）。
  4. 依赖注入：`current_user`、`require_permission(perm)`、`org_scope(user)` 返回可见组织 id 集合。
  5. 启动校验：无 `SECRET_KEY` / 无初始管理员 → 拒绝启动并给出明确指引。首个超级管理员经一次性 `bootstrap` 命令创建。
  6. 前端：登录页、鉴权路由守卫、按权限渲染菜单、401/403 处理、自动刷新。
- **验证**：`pytest tests/auth`（登录成功/失败/锁定/过期/刷新/吊销/越权 403/fail-closed 启动拒绝）；覆盖 ≥ 90%。手测：错误口令 5 次锁定；过期 token 拒绝。
- **退出标准**：不变量 4/6/8 成立；无明文口令；session 可过期可吊销；限速生效。
- **回滚**：`alembic downgrade` 至 S2 + 弃分支。

#### S4 · 审计日志与配置/密钥基础设施
- **上下文**：为不变量 5/8 提供地基，后续所有写操作/敏感读挂接审计。
- **任务**：
  1. `audit_log` 模型（append-only，DB 层禁 update/delete 权限）+ 迁移。
  2. 审计服务 + FastAPI 中间件/依赖：捕获 actor、IP、action、target、before/after 摘要；敏感字段脱敏。
  3. 结构化日志（`structlog`/标准 logging JSON），启动打印 warnings，替代旧系统的「日志全禁」。
  4. `pydantic-settings` 统一配置；`.env.example` 列全变量；密钥缺失 fail-fast。
  5. PII 列级加密工具（如 `cryptography` Fernet / pgcrypto），`id_card`/`bank_account` 加密读写 + 脱敏展示。
- **验证**：`pytest tests/audit`（写操作产生审计条目、审计不可改、脱敏正确、加密往返）；日志可见。
- **退出标准**：不变量 5/7/8 成立。
- **回滚**：`alembic downgrade` 至 S3 + 弃分支。

### Phase B — 主数据与数据迁移

#### S5 · 组织/员工/职级主数据管理
- **上下文**：DB 成为权威来源的落地。提供组织树、员工、职级/薪档的增删改查 API + 前端界面，全部 RBAC 组织范围过滤。
- **任务**：
  1. API：org_unit 树 CRUD（防环）、employee CRUD（工号唯一、PII 加密、social_city 必填）、job_grade/salary_band CRUD。
  2. 仓储层强制 `org_scope` 过滤；区域 HR 只见本区域，店长只见本店。
  3. 前端：组织树管理、员工列表/详情/编辑（PII 脱敏显示+权限解密）、职级薪档配置。
  4. 批量：员工 Excel 模板导入（走 S6 管线的简化版）。
- **验证**：`pytest tests/masterdata`（越权不可见、工号唯一冲突、防环、PII 脱敏）；前端 E2E 冒烟：建门店→建员工→受限账号看不到他店。
- **退出标准**：不变量 3/6/7 成立；主数据可维护。
- **回滚**：`alembic downgrade` 至 S4 + 弃分支。

#### S6 · Excel 导入管线与历史数据迁移 【强模型 · 正确性关键】
- **上下文**：迁移旧 `salary_search_app` 的解析价值（`normalize_salary_header` 表头归一化 60+ 规则、`store_aliases` 84 条门店别名、影子行/去重）到一个**带校验、暂存、人工核对、审计**的导入服务；把 138MB 历史缓存导入新库。**修复旧 bug**：去重键含门店、失败不静默、金额 Decimal。
- **任务**：
  1. 移植并重构解析逻辑到 `domain/excel_parser`（纯函数、可单测；表头映射规则下沉为 `header_rules.json` 数据文件；`郑建洪`/月份列特例等硬编码改为可配置且审计）。
  2. 导入管线：上传→解析→写 `import_staging_row`（每行带校验状态/错误原因）→前端核对页（展示错误清单，无法解析金额/缺工号阻断）→确认→事务写正式表 + `import_batch` 审计。
  3. 身份匹配：按 `(period, emp_no)`；无工号的历史数据用「姓名+门店+入职」辅助人工确认，绝不自动按姓名合并。
  4. 历史迁移脚本：读旧 `salary_data.sqlite` 的 JSON blob → 规整 → 灌入 `payroll_result`/历史表（标注来源=历史迁移，只读）。
  5. 导入防公式注入、大文件流式、进度反馈。
- **验证**：`pytest tests/import`（表头归一化各规则表驱动、去重键含门店不误删同名不同店、文本金额→进错误清单而非归零、影子行策略）；覆盖 ≥ 90%。用旧 138MB 数据实测迁移，行数/总额与旧系统对账（差异出报告）。
- **退出标准**：不变量 1/2/3/5/9 成立；历史数据可查且与旧系统对账通过。
- **回滚**：截断导入表 + `alembic downgrade` + 弃分支；迁移前先 `pg_dump`。

### Phase C — 薪酬管理

#### S7 · 薪资结构与薪档带宽
- **上下文**：定义薪资由哪些组件构成、职级对应的薪档带宽、每位员工的固定结构（生效日期化）。为核算提供「结构」输入。
- **任务**：
  1. `salary_component_def` 目录管理（类型、是否计税、是否计入社保/公积金基数）。
  2. `job_grade↔salary_band` 映射；compa-ratio（实发/中位）校验与超带宽预警。
  3. `employee_salary_structure` 生效日期化维护（调薪后新记录，不覆盖历史）。
  4. 前端：组件目录、带宽表、员工结构编辑（含历史时间线）。
- **验证**：`pytest tests/comp_structure`（生效日期取值、超带宽预警、计税/社保基数标记）；前端冒烟。
- **退出标准**：结构可配置、生效日期化、带宽校验生效。
- **回滚**：`alembic downgrade` 至 S6 + 弃分支。

#### S8 · 调薪审批流
- **上下文**：调薪需多级审批（如 店长→区域HR→集团HR）后才生效并写入员工结构。提供**通用审批引擎**供后续（如核算复核）复用。
- **任务**：
  1. 通用 `approval_flow/step/instance` 状态机（可配置层级、按组织/金额路由）。
  2. `salary_adjustment` 单据：发起→逐级审批/驳回→通过后生成 `employee_salary_structure` 新记录（生效日）。
  3. 待办、通知（站内；邮件/IM 预留）、全程审计。
  4. 前端：发起调薪、审批待办、单据流转轨迹。
  5. **职责分离（SoD/maker-checker）**：审批时强制 `approver != 发起人`。S3 安全复核指出当前角色里 GROUP_HR 同时有 adjustment:create+approve、FINANCE 同时有 payroll:run+approve，须在 approve 端强制不同主体，或拆出独立审批角色。
- **验证**：`pytest tests/approval`（各状态迁移、驳回、越级、通过后结构生效日正确、审计完整、同一人不能自审）。
- **退出标准**：审批流可配可用；通过即生效且可追溯；职责分离生效。
- **回滚**：`alembic downgrade` 至 S7 + 弃分支。

#### S9 · 人力成本预算与编制
- **上下文**：管理支柱的成本视角。按组织/周期维护编制人数与人力成本预算。**注意**：本步只做预算的录入与维护；「预算 vs 实际」差异对比依赖 S13 核算结果，放在 S15 看板实现，避免依赖倒置。
- **任务**：`labor_budget` CRUD；编制人数维护；前端预算表。
- **验证**：`pytest tests/budget`（预算录入、组织范围、周期归属）。
- **退出标准**：预算可维护；差异对比接口预留（S15 实现）。
- **回滚**：`alembic downgrade` 至 S8/S7 + 弃分支。

### Phase D — 薪资核算（正确性关键，全程 Decimal + 可追溯）

#### S10 · 考勤/绩效数据
- **上下文**：核算的变量输入。提供考勤/绩效的录入界面 + Excel 导入（复用 S6 管线）。
- **任务**：`attendance_record`（应出勤/实出勤/加班/请假/迟到早退）、`performance_record`（系数/得分）模型 + 录入 API + 导入 + 前端；店长可录本店。
- **验证**：`pytest tests/attendance`（校验区间、导入、组织范围）。
- **退出标准**：考勤绩效可录可导、按期归属。
- **回滚**：`alembic downgrade` 至 S9 + 弃分支。

#### S11 · 薪资计算规则引擎 【强模型 · 正确性关键】
- **上下文**：把「结构（S7）+ 考勤绩效（S10）」按可配置公式算出各组件金额与应发合计（gross）。**纯函数、确定性、版本化、逐项可追溯**。
- **任务**：
  1. `domain/payroll_engine`：组件公式 DSL 或注册式计算器（如 加班费=时薪×系数×加班时长、绩效=基数×绩效系数），全 `Decimal`，`ROUND_HALF_UP`。
  2. **计薪模式分派**：按 `employment_type` 走月薪制（结构组件+考勤折算）或时薪制（时薪×工时，兼职小时工主路径）；试用期按 `probation_end` 应用试用期系数/试用期薪资。
  3. **入离职当月折算**：按 `hire_date`/`leave_date` 与计薪日历做按天折算（21.75 或自然日口径可配，见 Q3）；离职结算含未休年假折算钩子（口径见 Q9）。
  4. 规则版本化（`rule_version`），每次核算记录所用版本。
  5. 计算结果产出逐项明细（component→输入→公式→值），供工资条与复核展开。
  6. 缺输入/异常值不静默：进入核算异常清单，阻断该员工出账。
- **验证**：`pytest tests/engine`（表驱动：各组件公式、边界、缺勤扣款、Decimal 精度、四舍五入口径）；覆盖 ≥ 95%。
- **退出标准**：不变量 1/2/11 成立；同输入同版本结果确定。
- **回滚**：纯代码，弃分支。

#### S12 · 社保公积金 + 个税累计预扣 【强模型 · 中国合规】
- **上下文**：按员工社保城市（广州/深圳/东莞/珠海口径不同）计算五险一金；按累计预扣法计算个税。**政策数据化、生效日期化、城市化**。⚠️ 见第 7 节开放问题——落地前须确认各市费率/基数与专项附加扣除来源。
- **任务**：
  1. `social_insurance_policy`（城市×生效期×险种：费率、基数上下限）+ `domain/social_insurance` 计算（个人/单位分摊、基数封顶封底）。
  2. `tax_config` 累计预扣税率表 + 起征点 + 专项附加扣除；`domain/tax` 按 YTD 累计计算当月应扣。
  3. 政策维护界面（HR 可维护费率、生效日）。
  4. 全 `Decimal`、逐项可追溯、城市缺政策则阻断出账。
- **验证**：`pytest tests/social_tax`（各市样例、基数封顶封底、累计预扣跨月、专项附加、税率跳档边界）；覆盖 ≥ 95%；与手工/官方样例对账。
- **退出标准**：不变量 1/2/11 成立；四市口径正确；个税累计法正确。
- **回滚**：`alembic downgrade` 至 S11 + 弃分支。

#### S13 · 薪资核算运行与发放
- **上下文**：把 S11+S12 串成一次可复核、可封存、不可篡改的**核算运行**，产出工资条与发放。
- **任务**：
  1. `payroll_run`：选周期+组织范围→锁定→批量计算（带进度）→异常清单→复核（复用 S8 审批）→封存（`payroll_result` 只读）→发放状态。
  2. 期间锁：`pay_period` 状态机 open→calculating→closed→paid，closed 后禁改考勤/结构。**并发互斥**：同一周期同一组织范围同时只允许一个进行中的 run（DB 唯一约束/advisory lock），重复发起报冲突。
  3. 工资条：员工自助查看本人（逐项明细），敏感、限本人。
  4. 重算保护：已发放不可静默覆盖，需红冲/补差流程。
- **验证**：`pytest tests/payroll_run`（全流程、期间锁、封存不可改、异常阻断、重算保护、审计）；E2E：一期从核算到发放。
- **退出标准**：不变量 5/11 成立；核算可复核可封存；工资条可见。
- **回滚**：`alembic downgrade` 至 S12 + 弃分支；作废未封存 run。

#### S13b · 钉钉薪酬推送（新需求，2026-07-19 加入）
- **上下文**：核算确认后，把每家门店的薪酬按部门推送给对应经理：**厅面员工薪酬→店长，厨房员工薪酬→厨房经理**，按门店隔离。用户确认的设计：
  1. **员工加部门字段**：`employee.department` 枚举（厅面/厨房/其他），S5 员工模型补列（迁移）+ 录入界面。
  2. **门店配负责人**：门店组织上配置 `店长`/`厨房经理`（指向 employee/user + 钉钉 userid/手机号），存加密。
  3. **钉钉工作通知**（企业内部应用）：配置 AppKey/AppSecret/AgentId（走密钥基础设施，不入库明文）；调用工作通知 API 按 userid 私发（非群机器人，避免薪酬广播）。
  4. **内容与触发**：内容=本店该部门薪酬明细（员工+关键金额）；核算 `payroll_run` 确认/发放后自动触发，支持手工重推；每次推送落审计（谁/何时/推给谁/覆盖哪些人，不含明文金额到日志）。
  5. **异议申诉按钮**（新增，2026-07-19）：推送的钉钉工作通知用**Action Card（消息卡片）带按钮**「有异议·发起申诉」；经理点击→打开申诉表单（H5/回系统页），填写异议对象（本次推送/某位员工）+ 原因 → 生成 `comp_appeal` 申诉单。申诉走**复用 S8 通用审批引擎**的流程：经理发起→HR 受理核实→结论（维持/更正）；若更正且该期已发放，触发 S13 的红冲/补差流程。申诉状态变化回推钉钉通知发起人。**授权**：只有收到该推送的经理能对其 (门店,部门) 范围发起申诉，绝不越店越部门。全程审计。
- **权限/隐私**：经理看本店本部门全员工资是明确数据共享决策——收件人解析必须严格按 (门店, 部门)，绝不跨店跨部门；推送前二次校验收件人身份。
- **可靠性**：推送失败重试 + 失败告警；先做**沙盒/演练模式**（不真发，仅生成待推清单）供核对，确认无误再开真推。真实推送需用户提供钉钉企业应用凭据。
- **任务**：`dingtalk` 客户端（token 缓存、工作通知/ActionCard API）、部门→收件人路由服务、推送记录表、`comp_appeal` 申诉单 + 申诉审批流（复用 S8）、`payroll_run` 确认钩子、前端门店负责人配置页 + 推送记录/重推 + HR 申诉处理页。
- **验证**：`pytest tests/dingtalk`（路由按门店+部门隔离、收件人解析、失败重试、审计、沙盒模式不真发）；`pytest tests/appeal`（只有收件经理能对其范围发起、申诉状态机、更正后触发重算钩子、越权申诉被拒）；用测试凭据在沙盒验证。
- **退出标准**：厅面→店长/厨房→厨房经理路由正确且隔离；申诉按钮可发起、按范围授权、结论可维持/更正并联动重算；沙盒清单可核对；审计完整。
- **回滚**：`alembic downgrade`（部门列/推送表/负责人配置/申诉表）+ 弃分支；关特性开关停推送。
- **开放问题**：Q11 一店多店长/无厨房经理时的兜底？Q12 部门=其他(如后勤/店长本人)的薪酬推给谁？Q13 推送频率与免打扰时段？Q14 申诉时限（发放后 N 天内可申诉）？Q15 申诉表单是钉钉内 H5 还是回系统 Web 登录？Q16 申诉粒度（整次推送 vs 逐个员工）？

### Phase E — 查询 / 报表 / 看板

#### S14 · 查询与员工自助
- **上下文**：重建旧系统的核心查询价值，但 RBAC 组织范围 + 真索引（不再全内存线性扫描）。
- **任务**：查询 API（姓名/工号/周期/组织，分页，索引优化）；员工自助查本人工资条；越权不可达。
- **验证**：`pytest tests/query`（组织范围、分页、索引命中）；性能：大数据量查询有界。
- **退出标准**：不变量 3/6 成立；查询快且受限。
- **回滚**：弃分支。

#### S15 · 管理看板与分析
- **上下文**：重建旧看板（人力成本/平均工资/门店排行/区域/趋势），改为真 SQL 聚合 + RBAC + 预算对比。
- **任务**：聚合 API（按组织/周期/区域，人力成本、平均工资、人数口径明确）；前端指标卡+趋势+排行；接 S9 预算 vs 实际。
- **验证**：`pytest tests/dashboard`（聚合口径、人数去重口径、组织范围）。
- **退出标准**：看板口径明确、受权限约束。
- **回滚**：弃分支。

#### S16 · 导出与报表
- **上下文**：xlsx 导出（防公式注入、RBAC、行数上限、审计），加社保/个税申报表与银行代发文件。
- **任务**：通用导出服务（转义、脱敏、限额、审计）；社保申报表、个税申报表、银行代发格式（⚠️ 格式见开放问题）。
- **验证**：`pytest tests/export`（公式注入转义、权限、审计、行数上限、代发格式）。
- **退出标准**：不变量 9 成立；报表可用。
- **回滚**：弃分支。

### Phase F — 加固与上线

#### S17 · 测试完善 / 安全评审 / 上线
- **上下文**：收口。E2E 关键流程、安全评审、部署与备份、可观测性、覆盖门槛。
- **任务**：
  1. Playwright E2E：登录→主数据→考勤→核算→发放→查询→导出全链路。
  2. 安全评审：TLS 终止（Caddy/nginx）、Secure cookie、限速、session 过期、依赖扫描（bandit/pip-audit/npm audit）、渗透自查。
  3. 部署：生产 docker-compose + 反向代理 + 防火墙规则；`pg_dump` 定时备份 + 恢复演练 runbook。
  4. 可观测性：健康检查、指标、错误告警。
  5. 覆盖门槛闸：后端 ≥80%、核算模块 ≥95%，CI 阻断。
- **验证**：E2E 全绿；`pip-audit`/`npm audit` 无高危；备份可恢复；覆盖达标。
- **退出标准**：全部不变量成立；可上线。
- **回滚**：部署层回滚到上一个 tag。

---

## 6. 里程碑（可交付节奏）

- **M1（S1–S4）地基**：可登录、有权限、有审计的空壳。→ 可演示鉴权与组织树。
- **M2（S5–S6）主数据 + 历史迁移**：DB 成权威来源，历史工资可查。→ **可替代旧查询系统**。
- **M3（S7–S9）薪酬管理**：结构/调薪审批/预算。→ HR 可管薪酬。
- **M4（S10–S13）核算发放**：一期完整算薪到发放。→ **核心业务闭环**。
- **M5（S14–S16）查询报表看板**：对外查询、看板、申报/代发导出。
- **M6（S17）上线加固**：安全、备份、E2E、上生产。

---

## 7. 开放问题（须在对应步骤前与业务确认，不阻塞总体规划）

| # | 问题 | 影响步骤 | 默认假设（待确认） |
|---|---|---|---|
| Q1 | 广州/深圳/东莞/珠海各市五险一金**费率、基数上下限、生效期**从哪取？ | S12 | 政策数据化，HR 维护；先用公开口径占位 |
| Q2 | 个税**专项附加扣除**数据来源（员工申报 / HR 录入 / 外部）？ | S12 | HR 录入 + 员工自助申报 |
| Q3 | 计薪周期与口径：自然月？跨月考勤截止日？**计时/计件/底薪+提成**混合？ | S10/S11 | 自然月；底薪+绩效+补贴，加班计时 |
| Q4 | 是否需要**银行代发文件**特定格式（哪家银行模板）？ | S16 | 通用 CSV + 可扩展模板 |
| Q5 | 员工自助入口是否要**微信 H5 / 小程序**，还是仅 Web？ | S13/S14 | 先 Web 响应式，H5 预留 |
| Q6 | 调薪审批**层级与路由**（按金额/组织）？ | S8 | 店长→区域HR→集团HR，可配 |
| Q7 | 历史数据迁移**对账口径**：允许多少差异？无工号历史如何认领？ | S6 | 逐行对账出差异报告，人工认领 |
| Q8 | 是否需要与现有**考勤机/钉钉/企业微信**对接取考勤？ | S10 | 先 Excel 导入 + 手工录入 |
| Q9 | **兼职小时工**计薪口径（时薪档、日结/月结、是否缴社保或商业险）与离职结算口径（未休年假折算）？ | S11/S12 | 时薪×工时月结；社保口径按用工性质由 HR 确认 |
| Q10 | 薪资数据**保留期限与销毁**合规要求（劳动法档案保存年限）？ | S4/S17 | 默认永久保留 + 离职员工 PII 最小化，待确认 |

---

## 8. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 社保/个税算错引发合规与纠纷 | 高 | S12 政策数据化 + 官方样例对账 + ≥95% 覆盖 + 逐项可追溯 |
| 历史迁移数据错乱/丢失 | 高 | S6 暂存核对 + 迁移前 pg_dump + 逐行对账 + 只读标注 |
| 敏感薪资泄露 | 高 | RBAC 组织范围 + PII 加密 + 审计 + TLS + 无明文口令 |
| 单文件/全内存旧病复发 | 中 | 分层架构 + 真 SQL 索引 + 不变量清单守门 |
| 范围蔓延、周期过长 | 中 | 里程碑交付，M2 即可替代旧系统先上线 |
| 中国薪酬政策变化 | 中 | 政策与规则版本化、生效日期化 |

## 9. 非目标（本阶段不做）

- 招聘/绩效评估全流程、培训、社保代缴对接经办机构 API。
- **个税年度汇算清缴申报**（员工在个税 App 自行办理；系统只保证累计预扣正确并提供年度收入明细导出）。
- 多币种/跨境薪酬。
- 移动原生 App（H5 预留但不在初版）。
- 与第三方 HR SaaS 双向同步。

---

## 10. 复用旧系统清单（迁移而非继承）

- ✅ **迁移**：`normalize_salary_header` 表头归一化规则、`store_aliases`（84 条门店别名）、门店名净化、`salary_data.sqlite` 历史数据。
- ⚠️ **修复后迁移**：去重逻辑（键加门店）、金额解析（Decimal + 失败不归零）、影子行判定（不删唯一记录）。
- ❌ **丢弃**：单文件架构、内嵌 HTML、blob 缓存、fail-open 认证、明文口令、日志全禁、CommandLine 模糊杀进程等运维脚本。
```
