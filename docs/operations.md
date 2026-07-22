# 运行与恢复手册

**最后更新：2026-07-22**

本手册适用于 `deploy/docker-compose.yml` 的 PostgreSQL 容器部署。薪资批次迁移中存在 forward-only 数据保护迁移，因此**数据库恢复优先于尝试 schema downgrade**。

## 发布前检查

1. 确认 `deploy/.env` 中的 PostgreSQL 口令、JWT 密钥和 PII 加密密钥均已替换示例值；生产环境的 `COMP_COOKIE_SECURE=true`。
2. 在首次升级到 D20 组织同步迁移 `i4r7l0n2q568` **之前**创建并校验一份 pre-D20 数据库备份，再运行 `docker compose -f deploy/docker-compose.yml up -d --build`。后端入口会在启动 API 前运行 `alembic upgrade head`；`/health/ready` 变为 200 前不应切流。
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
复核地址和业务方抽样核对，然后在受控发布窗口显式切换为 `live`；Client Secret 只进入后端
secret manager/未跟踪环境文件，不得写入前端变量、仓库、日志或审计明细。

经理端复核采用钉钉 H5 免登，不使用人事后台账号密码。生产环境还必须配置
`COMP_DINGTALK_CORP_ID`，并在钉钉企业内部应用中把 HTTPS 域名加入安全域名。工作通知只显示
月份、门店、部门和人数；员工姓名及金额仅在 `/manager-review/<随机标识>` 页面通过一次性
免登码换取 15 分钟专用会话后返回。该会话不写入 local/session storage，不能调用人事 API，
且每次请求都会重新校验通知收件人及“门店 + 部门”复核范围。

在人事后台“用户复核范围”页为店长/厨房经理完成三项配置：钉钉 userid、精确复核范围、
“仅钉钉复核”。最后一项会禁止密码和 refresh token 登录，但不会停用工作通知。上线前用
厅面、厨房各一个账号验证：只能看到本部门员工；错误钉钉身份、转发链接、旧批次链接均不能
继续操作。`COMP_DINGTALK_REVIEW_SESSION_TTL_MINUTES` 只允许 5–30 分钟，默认 15 分钟。
正式发送的复核链接还受 `COMP_DINGTALK_REVIEW_LINK_TTL_HOURS` 绝对期限限制（默认 168 小时）；
每次查看、确认或提交异议都会实时向钉钉复核在职状态、负责人规则和完整部门父级路径。负责人调店、
离职、兼职跨店或钉钉读取失败时，旧链接立即拒绝，不等待下一次全量同步。

## 直接同步钉钉门店与负责人

门店及厅面/厨房负责人的生产权威源是钉钉组织通讯录，数据流为“钉钉 API → 同步预览 → 人事确认
→ 本系统”，**不需要也不允许用钉钉 Excel、旧预览或手工接口绕过新预览**。组织通讯录同步始终
只读：只读取 `COMP_DINGTALK_ORG_ROOT_MAPPINGS` 指定根的部门树，以及判定负责人所需的成员、工号、
职位和部门归属；不会向钉钉写组织或人员数据，也不会读取配置根以外的通讯录（摘要工作通知是独立
投递流程）。该操作只供集团 HR 使用：账号必须具备全局
`dingtalk_org:sync` 和通知管理权限；默认仅 `GROUP_HR` 与应急管理用的 `SUPER_ADMIN` 满足，
区域经理、店长、厨房经理和普通员工均无权发起同步。

### 上线配置

1. 在受控的后端环境中配置完整的钉钉企业内部应用凭证，并设置
   `COMP_DINGTALK_READ_SYNC_ENABLED=true`。凭证只保存在 secret manager 或未跟踪的
   `deploy/.env`，不得粘贴到工单、文档、前端变量或日志。
2. 生产边界只用 `COMP_DINGTALK_ORG_ROOT_MAPPINGS=<正整数远端根ID>:<已有本地锚点代码>,...`。
   本地锚点必须已存在、启用且不是门店；远端根 ID、本地锚点代码均须唯一，远端根子树不得交叠，
   本地锚点不得相同或互为祖先/后代。部署前逐个核对锚点代码和完整路径。旧的
   `COMP_DINGTALK_STORE_ROOT_NAMES` 只兼容 sandbox/旧测试，不能作为生产权威边界。
3. 用 `COMP_DINGTALK_DINING_MANAGER_TITLES` 与 `COMP_DINGTALK_KITCHEN_MANAGER_TITLES`
   分别配置厅面、厨房负责人的钉钉职位精确值，多个值用英文逗号分隔，两组不得重叠。不得使用
   “包含经理”等模糊匹配；实际通讯录未维护职位时，应先在钉钉补齐或采用经评审的部门负责人规则。
4. 保持 `COMP_DINGTALK_ORG_SYNC_FRESHNESS_MINUTES=5`，除非经安全评审后在允许的 1–15 分钟
   范围内调整。这个值用于正式薪资推送前的组织映射新鲜度门禁，不是后台定时同步周期。
5. `COMP_DINGTALK_ORG_SYNC_TIMEZONE` 必须是有效 IANA 时区，生产固定为 `Asia/Shanghai`。应用只
   校验该值并生成一次性预览，不自行调度；不得增加“调度小时”环境变量或在容器中运行 cron/loop。

### 人事操作流程

首次上线必须先备份 pre-D20 数据库，在完全隔离且只含合成数据/测试身份的 UAT 环境跑完整流程。
UAT 通过后，首次生产运行仍须由 HR 手动“刷新预览 → 逐项核对 → 确认应用”；核对生产树和负责人
权限无误后，基础设施运维才可启用外部每日调度。定时任务永远只生成/复用预览，不自动应用。

1. 在“组织架构”页点击“刷新预览”。系统直接读取钉钉部门、成员、职位和部门归属，
   只生成预览，不立即修改正式组织或负责人权限。
2. 核对钉钉门店数、本地门店数、完整部门路径以及下表中的每个显式动作。尤其检查负责人撤销项、
   新建门店和钉钉中缺失的本地门店；不得只看汇总数量后直接确认。
3. 负责人存在任何 `CONFLICT` 时，确认按钮和后端应用都会拒绝执行。先在钉钉修正职位/部门归属，
   或在人事主数据中修正工号、账号、员工绑定，再重新生成预览。仅姓名相同不会自动绑定负责人。
4. 预览自生成起 **15 分钟**有效。点击“确认应用变更”时，系统会重新读取一次钉钉并核对快照及
   本地基线；负责人调动、钉钉组织变化、本地并发修改或预览过期都会使该批次失效，必须重新预览，
   不会套用旧负责人。
5. 应用后立即为所有 `CREATE` 门店补齐城市并抽查厅面、厨房复核范围。新建门店虽然会处于
   `ACTIVE`，但城市默认留空，城市未补齐前不得进入正式薪酬推送范围。

### 外部每日调度

由生产主机的唯一外部调度器按 **每日 09:00 `Asia/Shanghai`** 执行下面的精确命令；调度器必须
显式设置/校验时区，不能把主机本地时区当作隐含约定：

```powershell
docker compose -f deploy/docker-compose.yml --profile org-sync-job run --rm dingtalk-org-sync-job
```

`dingtalk-org-sync-job` 保留后端入口迁移，随后只执行一次 `python -m app.dingtalk.org_sync_job` 并
退出。数据库 advisory lock 覆盖供应商读取、预览、通知和审计全过程；重叠调用拿不到锁时安全
退出，因此不要在命令外再套应用层循环。退出码 0 表示成功、无变化或另一个实例持锁；退出码 1
表示本次检查失败，必须告警并查看审计中的稳定错误码。

区域/门店动作含义如下；`change_fields` 只可能是 `name`、`parent_id`、
`dingtalk_dept_id`，一次 `UPDATE` 可同时改名和移动：

| 动作 | 运维含义 |
| --- | --- |
| `LINK` | 将唯一匹配的已有区域/门店关联到稳定的钉钉部门 ID。 |
| `CREATE` | 在已解析的本地上级组织下新建区域/在营门店；新门店应用后必须人工补齐城市。 |
| `ACTIVATE` | 将匹配的历史区域/门店恢复为在营并关联钉钉部门 ID。 |
| `UPDATE` | 按 `change_fields` 更新已关联区域/门店的名称、上级和/或钉钉部门 ID。 |
| `DEACTIVATE` | 权威远端范围内缺失的本地在营节点；预览项的 `match_method=MISSING_IN_DINGTALK`，应用后状态变为 `HISTORICAL`，不物理删除。应用前必须检查根映射、钉钉归属和停业决定。 |
| `NO_CHANGE` | 无正式组织写入；用于表达已应用/保留项，不得误当成待应用动作。 |

负责人动作含义如下：默认钉钉职位“店长”对应厅面、“厨房经理”对应厨房；正式值以上述配置为准。

| 动作 | 运维含义 |
| --- | --- |
| `ASSIGN` | HTTP 预览值；持久化动作为 `ASSIGN_SCOPE`。以稳定钉钉身份或唯一工号匹配员工，替换该“门店 + 部门”的复核人；旧范围随之撤销。 |
| `REMOVE` | HTTP 预览值；持久化动作为 `REMOVE_SCOPE`。钉钉中已没有有效唯一负责人时明确撤销现有复核范围，必须逐项人工确认。 |
| `CONFLICT` | HTTP 预览值；持久化动作保留为 `NO_CHANGE` 且 item 状态为 `CONFLICT`。任何冲突都会阻止整个批次应用。 |

排障时使用下列完整状态协议，不能从 latest 响应臆测未返回的批次状态：模式为 `sandbox/live`；
触发来源为 `MANUAL/SCHEDULED`；批次状态为 `PREVIEWED/APPLIED/STALE`；差异类型为
`REGION/STORE/REVIEWER`；持久化动作共 `LINK/CREATE/UPDATE/ACTIVATE/DEACTIVATE/ASSIGN_SCOPE/REMOVE_SCOPE/NO_CHANGE`；
差异项状态为 `READY/CONFLICT/APPLIED/IGNORED`（`IGNORED` 为保留值）；通知投递状态为
`PENDING/SANDBOXED/SENT/FAILED`。

### 稳定冲突码与故障处置

预览项的稳定 `conflict_code` 必须原样进入告警/工单：`ORG_NODE_CLASSIFICATION_CONFLICT`（节点分类
不唯一）、`ORG_PATH_AMBIGUOUS`（完整路径歧义）、`ORG_MANAGER_AMBIGUOUS`（负责人不唯一）、
`ORG_EMPLOYEE_MATCH_FAILED`（工号无法唯一匹配本地在职员工）、`ORG_IDENTITY_CONFLICT`（稳定身份
与账号/员工冲突），以及 `STORE_UNRESOLVED`、`MULTIPLE_LOCAL_ACCOUNTS`、
`MANAGER_ACCOUNT_INACTIVE`、`MANAGER_ACCOUNT_PRIVILEGED`、`MANAGER_IDENTITY_CONFLICT`、
`MANAGER_ASSIGNED_MULTIPLE_STORES`。它们都必须先修正钉钉或本地主数据，再生成**新预览**；
不得跳过冲突项。

批次和任务还会使用这些稳定码：`ORG_ROOT_CONFIG_INVALID`、`ORG_SNAPSHOT_INVALID`、
`ORG_NODE_CLASSIFICATION_CONFLICT`、`ORG_LOCAL_BASELINE_CHANGED`、`ORG_CONCURRENT_CHANGE`、
`ORG_PREVIEW_HAS_CONFLICTS`、`ORG_ROOT_CONFIG_CHANGED`、`PREVIEW_EXPIRED`、
`PROVIDER_SNAPSHOT_CHANGED`、`CONCURRENT_CHANGE`、`BATCH_NOT_FOUND`、`BATCH_STALE`、
`TENANT_NOT_CONFIGURED`、`ROLE_NOT_CONFIGURED`、`INVALID_PREVIEW` 和
`ORG_PROVIDER_UNAVAILABLE`。任何码都默认拒绝正式写入；配置/快照/并发/过期类错误要修复根因并
重新预览，不能重复应用旧批次。

- 以最近一次成功的定时检查为基准设置 **26 小时（93600 秒）** stale 告警；即使没有变更也必须
  更新成功检查时间。发薪新鲜度门禁独立 fail-closed，不能因为任务告警正常而放宽。
- 钉钉超时、限流、5xx 或读取失败时，任务退出 1，审计记录 `ORG_PROVIDER_UNAVAILABLE`；保留
  最近已应用组织但禁止使用失败读取生成/确认变更，供应商恢复后重新预览。
- 组织摘要通知失败不回滚已成功生成的预览。普通失败按 delivery 状态由运维核对处置，不能假定
  系统会自动重试；出现
  `PROVIDER_SEND_OUTCOME_UNKNOWN` 时不得盲目重发，先由运维在钉钉侧人工核对是否已送达，再按
  幂等键和审计结果决定补发。通知稳定失败码还包括 `PUBLIC_BASE_URL_MISSING`、
  `PUBLIC_BASE_URL_INVALID`、`RECIPIENT_NOT_AUTHORIZED`、`MISSING_DINGTALK_USER_ID`、
  `BATCH_NOT_FOUND` 和 `PROVIDER_SEND_FAILED`。

### 正式推薪门禁

手动选择门店推送工资的链路必须在**生成/暂存任何发送记录之前**调用组织新鲜度门禁，并同时满足：

- 最近一次已确认的钉钉组织同步不早于 `COMP_DINGTALK_ORG_SYNC_FRESHNESS_MINUTES`（默认 5 分钟）；
- 本次选择的每个“门店 + 厅面/厨房”范围，都在同一个最近同步批次中同时具有已应用的门店覆盖项
  和负责人项，且当前复核账号仍绑定该同步批次确认的同一员工及钉钉身份；任一范围缺失或负责人
  已调动都应整次拒绝，不能跳过后继续发送。

该门禁已集中接入正式模式的发送记录暂存入口，并在真正调用钉钉前再次校验；第二次校验失败会
在未调用供应商 API 的前提下取消发送标记，等待人事重新同步和确认。沙盒模式保留给隔离测试月份，
不要求近期正式组织同步；正式模式仍须先完成部署迁移、72 家门店预览核对和全流程验收后再启用。

### 误应用、灾备与 D20 回退禁令

组织同步没有“反向修改钉钉”或直接撤销按钮。若误应用，先暂停外部调度和相关薪资写入，在钉钉
权威源或本地主数据中修正错误，再生成新的补偿性预览，由 HR 逐项核对并应用；不得直接改同步表、
删除审计记录或用 Excel 覆盖。若数据库已损坏，则按本手册“恢复演练与事故恢复”从带 SHA256 的
受控备份恢复，并使用该备份对应的 `COMP_ENCRYPTION_KEY` 版本；同时轮换暴露的钉钉 Client Secret、
`COMP_SECRET_KEY` 和其他受影响凭据，吊销会话并记录密钥版本，不记录密钥原文。

一旦生产在 D20 head `i4r7l0n2q568` 上产生任何组织同步批次、差异项、稳定身份或通知投递数据，
**禁止跨 `i4r7l0n2q568` 执行 Alembic downgrade**。唯一例外是事故窗口内先停止 backend、frontend、
一次性 job 和所有写入，再完整恢复已校验的 pre-D20 数据库备份以及与之匹配的加密密钥版本；不得
在保留 D20 数据的数据库上手工删表、禁用外键或尝试部分降级。恢复后须重新执行隔离验收和首次
生产手动预览流程，确认无误后才能恢复外部调度。

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
