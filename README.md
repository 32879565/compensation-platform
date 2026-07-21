# 薪酬一体化平台 (Compensation Platform)

连锁餐饮企业薪酬系统：核算 + 管理 + 查询。数据库为权威来源，Excel 仅作历史迁移与批量导入辅助。

- 建设蓝图：[plans/compensation-platform.md](plans/compensation-platform.md)（17 步 / 6 里程碑 / 11 条全局不变量）
- 旧系统真实数据迁移与目录复核：[docs/legacy-catalog-import.md](docs/legacy-catalog-import.md)
- 技术栈：FastAPI (Python 3.12) + PostgreSQL 16 + React (TypeScript / Vite / Ant Design)

## 目录结构

```
backend/    FastAPI 应用（routers / services / repositories / domain 分层）
frontend/   React + TS + Vite + AntD 单页应用
deploy/     docker-compose 与环境变量样例
plans/      建设蓝图
docs/       设计文档
```

## 首次配置（必须，fail-closed：不配置起不来）

```powershell
# compose 环境变量（Postgres 口令等）
copy deploy\.env.example deploy\.env
# 本地直跑后端的环境变量
copy deploy\.env.example backend\.env
# 然后分别编辑两个 .env，绝不能保留任何 change-me 示例值：
# - POSTGRES_PASSWORD：强且 URL-safe 的口令；还要同步替换 COMP_DATABASE_URL 中的同一口令。
# - COMP_SECRET_KEY 与 COMP_ENCRYPTION_KEY：两个彼此独立的随机值。
#   可分别执行两次下面命令生成：
python -c "import secrets; print(secrets.token_urlsafe(48))"
# - deploy/.env 的 COMP_COOKIE_SECURE 必须为 true；仅 backend/.env 本地 HTTP 直跑时可设为 false。
```

## 本地开发（Windows 可直接粘贴）

```powershell
# 1. 数据库
docker compose -f deploy/docker-compose.yml up -d postgres

# 2. 后端（http://127.0.0.1:8000/health）
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,windows]"
.venv\Scripts\alembic upgrade head
$bootstrapPassword = Read-Host -AsSecureString "管理员密码"
$env:COMP_BOOTSTRAP_PASSWORD = [System.Net.NetworkCredential]::new("", $bootstrapPassword).Password
.venv\Scripts\python -m app.auth.bootstrap --username admin
Remove-Item Env:COMP_BOOTSTRAP_PASSWORD
.venv\Scripts\python -m uvicorn app.main:app --reload

# 3. 前端（http://localhost:5173，dev server 将 /api 代理到 127.0.0.1:8000）
cd frontend
npm install
npm run dev
```

整套容器方式：`docker compose -f deploy/docker-compose.yml up -d --build`，
前端产物在 http://127.0.0.1:8080（容器端口只绑本机回环；对外发布由 S17 反代统一处理）。
若本机已有服务占用默认端口，可在 `deploy/.env` 设置
`COMP_POSTGRES_PORT`、`COMP_BACKEND_PORT`、`COMP_FRONTEND_PORT`；容器内部服务地址不受影响。

开发时如需 Swagger 文档：在 backend/.env 加 `COMP_DEBUG=true`（生产禁止），地址 /api/docs。

生产 HTTPS 使用同一 Compose 的 Caddy 网关 profile。先让 DNS 将域名解析到部署主机，再在 `deploy/.env` 设置 `COMP_PUBLIC_HOST` 和 `COMP_ACME_EMAIL`，并执行：

```powershell
docker compose -f deploy/docker-compose.yml --profile production up -d --build
```

网关是唯一暴露 80/443 的服务；它会清洗客户端转发头，前端只接受该固定网关的真实客户端 IP，后端也只信任前端。因此请勿把 backend 或 frontend 的回环端口重新映射到公网。

## 容器部署与首次管理员

运行 `docker compose -f deploy/docker-compose.yml up -d --build` 后，后端容器会在 API 启动前自动执行 `alembic upgrade head`；迁移失败时容器会退出。`/health/ready` 会验证数据库连通性。

首次部署还需要创建超级管理员（该命令也会写入 RBAC 角色和权限种子）：

```powershell
$bootstrapPassword = Read-Host -AsSecureString "管理员密码"
$env:COMP_BOOTSTRAP_PASSWORD = [System.Net.NetworkCredential]::new("", $bootstrapPassword).Password
docker compose -f deploy/docker-compose.yml exec -e COMP_BOOTSTRAP_PASSWORD backend python -m app.auth.bootstrap --username admin
Remove-Item Env:COMP_BOOTSTRAP_PASSWORD
```

## 质量门

```powershell
# 后端（覆盖率门槛 80% 已内置于 pytest 配置）
cd backend
.venv\Scripts\ruff check app tests
.venv\Scripts\black --check app tests
.venv\Scripts\mypy app
.venv\Scripts\python -m pytest

# 前端
cd frontend
npm run lint
npm run typecheck
npm test -- --run
npm run build
```

有 GNU make 时可用 `make ci` 一键执行以上全部。

## 供应链与依赖更新

容器和 CI 使用 `backend/requirements-build.lock`、`requirements.lock` 与
`requirements-dev.lock`：每个分发包都固定版本和 SHA256，基础镜像也固定到
digest。变更 `backend/pyproject.toml`、`backend/requirements-build.in` 或前端
依赖后，应在受控环境重新生成对应锁文件、审阅完整 diff，并提交锁文件；不要手工
修改锁文件中的版本或哈希。Windows 本地开发仍使用 `.[dev,windows]`，CI 与容器
锁文件以 Linux 为目标平台。

## 全局不变量（每次提交必须成立，详见蓝图第 1 节）

金额一律 Decimal/NUMERIC 禁 float；解析失败不静默；身份键含工号；认证 fail-closed；
全量审计；RBAC 组织范围仓储层强制；PII 列级加密；无明文口令；导出防公式注入；
测试门槛（后端 ≥80%，核算域 ≥95%）；计算逐项可追溯。
