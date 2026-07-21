# 运行与恢复手册

本手册适用于 `deploy/docker-compose.yml` 的 PostgreSQL 容器部署。薪资批次迁移中存在 forward-only 数据保护迁移，因此**数据库恢复优先于尝试 schema downgrade**。

## 发布前检查

1. 确认 `deploy/.env` 中的 PostgreSQL 口令、JWT 密钥和 PII 加密密钥均已替换示例值；生产环境的 `COMP_COOKIE_SECURE=true`。
2. 先创建并校验一次备份，再运行 `docker compose -f deploy/docker-compose.yml up -d --build`。后端入口会在启动 API 前运行 `alembic upgrade head`；`/health/ready` 变为 200 前不应切流。
3. 生产发布使用 `--profile production` 的 Caddy TLS 网关，且 DNS 已解析到该主机。不得将 backend 或 frontend 的回环端口暴露到公网。
4. 保存本次镜像 digest、Alembic head、备份 SHA256、操作人和开始/完成时间到变更记录。同步记录 `COMP_ENCRYPTION_KEY` 在受控 secret manager 中的**版本标识**（不记录密钥原文）。

## 创建备份

备份命令不会覆盖已有文件，输出 PostgreSQL custom-format archive，并创建同名 `.sha256` 校验文件：

```powershell
# 使用仓库外、受访问控制的目录；不要把薪资 archive 放进代码仓库。
$backupDirectory = "D:\controlled-backups\compensation"
New-Item -ItemType Directory -Force $backupDirectory | Out-Null
.\deploy\backup.ps1 -OutputPath (Join-Path $backupDirectory "compensation-2026-07-20.dump")
```

将 archive **和同名 `.sha256` 文件**保存到受访问控制的异地存储；恢复脚本会拒绝缺失或不匹配校验值的 archive。建议至少保留：最近 7 个日备份、最近 4 个周备份和最近 12 个按月备份；保留期限应由公司数据保留政策最终确认。

数据库 archive 不包含 `COMP_ENCRYPTION_KEY`。该密钥及其历史版本必须独立保存在具备异地恢复、访问审计和最小权限控制的 secret manager 中；**绝不可**写入 archive、仓库、日志或 checksum 文件。

## 恢复演练与事故恢复

恢复会替换当前数据库，脚本要求显式 `-Force`，并在 backend 仍运行时拒绝执行。它会先重建 `public` schema，再恢复 archive，避免旧备份恢复后残留较新迁移的对象；恢复前必须保留通过校验的同名 `.sha256` 文件。

```powershell
# 1. 在隔离环境先演练；生产恢复前停止所有 API 写入。
docker compose -f deploy/docker-compose.yml stop backend frontend

# 2. PostgreSQL 必须保持运行；恢复指定 archive。
$backupDirectory = "D:\controlled-backups\compensation"
$emergencyArchive = Join-Path $backupDirectory "before-restore-2026-07-20.dump"
.\deploy\restore.ps1 `
    -BackupPath (Join-Path $backupDirectory "compensation-2026-07-20.dump") `
    -EmergencyBackupPath $emergencyArchive `
    -Force

# 3. 重新启动。入口迁移完成后，再检查就绪和抽样结果。
docker compose -f deploy/docker-compose.yml up -d backend frontend
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

恢复前，从 secret manager 取回与 archive 记录对应的 `COMP_ENCRYPTION_KEY` 版本并配置到 `deploy/.env`；恢复后先用具备 PII 权限的受控账号抽样验证身份证/银行卡可解密，再恢复业务访问。恢复脚本会在预检 archive 后、删除当前 schema 前创建 `$emergencyArchive` 及其 `.sha256`；不可用或不兼容 archive 会在删除前停止。演练至少每季度执行一次：记录恢复耗时、`alembic current`、就绪探针、最新锁定薪资批次行数/金额抽样和审计日志可读性，以及 PII 解密抽样。任何失败都应保留 backend/postgres 日志，并在重新开放写入前完成根因分析。

## 历史门店组织回填

数据迁移 `q6f9a2c8d753` 将没有组织关联的历史薪资门店放到编码为
`HIST-REGION-PENDING`、名称为“历史门店（待归属）”的独立区域。迁移不会根据工资表中的
历史“区域”字段猜测当前组织归属；生成的区域和门店状态均为 `HISTORICAL`，门店编码使用
`HIST-ST-` 前缀和门店名称的稳定摘要。迁移完成后应核对：

```sql
SELECT version_num FROM alembic_version;
SELECT count(*) FROM org_unit
WHERE parent_id = (SELECT id FROM org_unit WHERE code = 'HIST-REGION-PENDING')
  AND type = 'STORE';
SELECT count(*) FROM salary_record
WHERE source = 'HISTORICAL' AND org_unit_id IS NULL;
```

生产环境仍以向前修复为首选。若刚部署后必须回退，先停止后端并创建带 SHA256 的新备份，
确认历史区域没有人工添加的组织、没有员工或核算数据引用其门店，再执行
`alembic downgrade p5e8f3a1b742`。降级会把本迁移关联的历史工资恢复为未关联并删除生成的
72 个门店及历史区域；存在非迁移子节点或其他外键引用时会拒绝删除，此时应恢复迁移前备份，
不得手工绕过约束。

## 按姓名生成临时员工

数据迁移 `r7a0b3d9e864` 按业务方明确决定，从最新历史工资月份生成当前员工候选。它要求该
月份姓名唯一、门店已关联，并使用 `LEGACY-NAME-` 加姓名稳定摘要生成可识别的临时工号。
入职日期、职位和门店取最新月份记录；部门暂设为 `OTHER`。兼职/小时工/寒暑假工映射为
`PART_TIME_HOURLY`，模型中明确列出的储备、洗碗和寒暑假岗位标记为特殊岗位。所有同名的
历史工资会关联到该临时员工，因此正式花名册到位后必须用受审计迁移替换临时工号并复核
同名、跨店人员，不能把摘要工号当作永久身份。

当前数据基线为：2026-06 共生成 3,702 名临时员工，关联 48,622 条历史工资；仅存在于更早
月份的 3,713 个姓名不进入当前员工主档，其 19,623 条历史工资保持未关联。部署后核对：

```sql
SELECT version_num FROM alembic_version;
SELECT count(*) FROM employee WHERE emp_no LIKE 'LEGACY-NAME-%';
SELECT count(*) FROM salary_record
WHERE source = 'HISTORICAL' AND employee_id IS NOT NULL;
```

若导入后尚未创建考勤、核算、调薪、用户绑定等下游数据，可先停止后端、创建带 SHA256 的
新备份，再执行 `alembic downgrade q6f9a2c8d753`。降级会解除本迁移建立的工资关联并删除
临时员工；任何下游外键引用都会使删除事务失败。出现这种情况时应采用向前校正迁移，或在
维护窗口恢复导入前备份，不得禁用外键强行删除。

## 密钥轮换

`COMP_SECRET_KEY` 轮换会使旧 JWT/refresh 会话失效；安排维护窗口并通知用户重新登录。`COMP_ENCRYPTION_KEY` 轮换涉及现有 PII 重加密，当前版本未提供自动重加密命令，必须在有经过验证的迁移工具和完整备份后进行，不能直接替换生产值。

钉钉企业应用始终以 `COMP_DINGTALK_MODE=sandbox` 起步。凭证完整只允许管理员执行连通性检测，
不会发送工作通知。正式发送前必须同时完成加密的用户 userid 路由、可从钉钉访问的 HTTPS
申诉地址和业务方抽样核对，然后在受控发布窗口显式切换为 `live`；Client Secret 只进入后端
secret manager/未跟踪环境文件，不得写入前端变量、仓库、日志或审计明细。

## 获批薪酬申诉的受控更正

钉钉沙盒申诉获批并不直接修改工资金额。系统会在同一事务中创建一条
`comp_appeal_correction_work_item`，由具备 `payroll:correct` 权限且组织范围匹配的
HR 通过 `GET /api/comp-appeal-corrections` 核对。该队列只包含批次、原始复核版本、
门店/部门和状态；不复制工资、员工标识或申诉自由文本。

- `PENDING_TRIAGE`：原始复核轮次仍可处理。先核实具体员工和源数据，再使用既有
  `reopen/unlock → 受审计源数据更正 → run → 重新确认/锁定` 流程；禁止直接修改最终工资。
- `PENDING_REOPEN`：该批次已锁定。必须先按既有顺序约束解锁，保留旧版本，再进行源数据
  更正和重算；若已有后续月份开始，系统会拒绝解锁。
- `HISTORICAL_SETTLEMENT_REQUIRED`：申诉对应的复核版本已经不是当前版本。不得把该申诉
  重定向到新版本或覆盖历史结果；需按经业务批准的红冲/补差/银行结算政策处理并保留外部
  凭证。当前系统不会伪造或自动执行该资金动作。

处理时应在审计中记录工作项 ID、对应源数据修改记录、重算版本和结算凭证编号。没有这些
证据时，不应将申诉标注为已完成。

## Observability and alerts

- `GET /metrics` is a Prometheus-text endpoint outside `/api`. It exposes only aggregate method, route-template, status, request-count, duration, total-5xx, and request-failure data; it never includes raw paths, query strings, client addresses, users, PII, or salary values. HTTP count, duration, status, and 5xx series are emitted only after ASGI `http.response.start` was successfully sent; a client disconnect before that point does not create a fabricated `500` series.
- Scrape `/metrics` only through the backend loopback listener or a trusted internal network. Do not expose it through the frontend proxy or public gateway.
- Alert on sustained growth of both `compensation_http_requests_5xx_total` and `compensation_http_request_failures_total`, plus failed or `503` `/health/ready` checks. The failure counter catches server-side application, streaming, and background-task failures after a response may already have sent a non-5xx status, including a server failure before any response start (which has no HTTP-status series). It excludes only confirmed normal client disconnects: an observed ASGI `http.disconnect`, a closed-client error seen by the response send wrapper, or normal task cancellation (`asyncio.CancelledError`). A bare `ClientDisconnect` is not sufficient, because Starlette can wrap unrelated send `OSError` failures. Investigate its structured logs even when the HTTP-status counter remains 2xx.

## Disposable browser E2E safety

Browser E2E scenarios create payroll-policy and salary-component records and
advance one seeded payroll through attendance entry, calculation, scoped store
review, HR approval, lock, export, and employee payslip query. Run them only
against a deliberately disposable stack, and provide all of the following
values explicitly:

- `E2E_ALLOW_WRITES=true` — an explicit acknowledgement that the suite writes data.
- `COMP_E2E_TARGET_MARKER=<identifier>` on the backend container and the exact
  same non-secret `E2E_TARGET_MARKER=<identifier>` to Playwright.
- `E2E_USERNAME` / `E2E_PASSWORD` for the disposable administrator and
  `E2E_REVIEWER_USERNAME` / `E2E_REVIEWER_PASSWORD` for the scoped store
  reviewer. The suite fails before opening a browser when any value is absent.
  CI generates fresh passwords and a fresh marker for every run; there is no
  repository fallback.

After the administrator bootstrap, seed the lifecycle prerequisites from inside
the backend container. The command is idempotent but is intended only for a new
disposable database:

```powershell
docker compose -f deploy/docker-compose.yml exec -T `
  -e E2E_ALLOW_WRITES -e E2E_USERNAME -e E2E_REVIEWER_USERNAME `
  -e E2E_REVIEWER_PASSWORD backend python -m app.e2e.bootstrap
```

The seed CLI opens no HTTP endpoint and refuses to open a database session
unless the backend marker is non-empty and `E2E_ALLOW_WRITES` is exactly
`true`. It never prints either password.

Playwright calls `/api/health` before any test runs and refuses the target
unless the marker is present and exactly matches. The ordinary `/health`
liveness response never contains this marker; `/api/health` includes it only
when `COMP_E2E_TARGET_MARKER` is explicitly configured. Do not configure that
variable in production. A marker is an environment identifier, not a secret.

All browser specs import the shared `frontend/e2e/guardedTest.ts` fixture. It
installs a context-wide guard plus a Chromium request-stage guard before a
test page is exposed, aborting every HTTP(S) request outside the verified
base-URL origin, including response-created 30x hops. It blocks every WebSocket
and service-worker registration, ignores per-spec `baseURL` overrides, and
allows no other schemes except the initial `about:blank` and same-origin `blob:`
URLs needed for verified workbook downloads. The shared sign-in flow and every
data-writing action also assert that the page is still on that origin. Do not
bypass this fixture with Playwright's unguarded `test` export.

The validated E2E stack is destroyed after CI, so E2E data intentionally has
no product-API cleanup path. Browser traces, screenshots, and video are
disabled, and `preserveOutput: 'never'` removes per-test output directories,
including Playwright's failure DOM context. CI deliberately uploads no
Playwright browser artifact.
