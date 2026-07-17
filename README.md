# 薪酬一体化平台 (Compensation Platform)

连锁餐饮企业薪酬系统：核算 + 管理 + 查询。数据库为权威来源，Excel 仅作历史迁移与批量导入辅助。

- 建设蓝图：[plans/compensation-platform.md](plans/compensation-platform.md)（17 步 / 6 里程碑 / 11 条全局不变量）
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
# 然后编辑两个 .env：把 change-me-strong-password 换成强口令（须 URL-safe，两处同步改）
```

## 本地开发（Windows 可直接粘贴）

```powershell
# 1. 数据库
docker compose -f deploy/docker-compose.yml up -d postgres

# 2. 后端（http://127.0.0.1:8000/health）
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m uvicorn app.main:app --reload

# 3. 前端（http://localhost:5173，dev server 将 /api 代理到 127.0.0.1:8000）
cd frontend
npm install
npm run dev
```

整套容器方式：`docker compose -f deploy/docker-compose.yml up -d --build`，
前端产物在 http://127.0.0.1:8080（容器端口只绑本机回环；对外发布由 S17 反代统一处理）。

开发时如需 Swagger 文档：在 backend/.env 加 `COMP_DEBUG=true`（生产禁止），地址 /api/docs。

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

## 全局不变量（每次提交必须成立，详见蓝图第 1 节）

金额一律 Decimal/NUMERIC 禁 float；解析失败不静默；身份键含工号；认证 fail-closed；
全量审计；RBAC 组织范围仓储层强制；PII 列级加密；无明文口令；导出防公式注入；
测试门槛（后端 ≥80%，核算域 ≥95%）；计算逐项可追溯。
