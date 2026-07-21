from fastapi import FastAPI, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.audit.middleware import ClientIpMiddleware
from app.auth.router import router as auth_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import RequestMetrics, RequestMetricsMiddleware
from app.db.session import engine
from app.routers.approval import router as approval_router
from app.routers.attendance import router as attendance_router
from app.routers.attendance_schedule import router as attendance_schedule_router
from app.routers.audit import router as audit_router
from app.routers.batch import router as batch_router
from app.routers.budget import router as budget_router
from app.routers.comp import router as comp_router
from app.routers.comp import structure_router
from app.routers.dashboard import router as dashboard_router
from app.routers.dingtalk import router as dingtalk_router
from app.routers.dingtalk_sync import router as dingtalk_sync_router
from app.routers.employee import router as employee_router
from app.routers.employee_tax import router as employee_tax_router
from app.routers.export import router as export_router
from app.routers.grade import router as grade_router
from app.routers.holiday import router as holiday_router
from app.routers.imports import router as imports_router
from app.routers.imports import salary_router
from app.routers.org import router as org_router
from app.routers.payroll import router as payroll_router
from app.routers.payroll_adjustment import router as payroll_adjustment_router
from app.routers.payroll_policy import router as payroll_policy_router
from app.routers.payslip import router as payslip_router
from app.routers.users import router as users_router


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)
    # 安全不变量：绝不对携带凭据的接口配置宽松/反射式 CORS。
    # 登录 CSRF 目前靠「仅接受 application/json + 无 CORS」隐式阻断；若未来接入
    # 跨源前端，必须用固定 Origin 白名单，且不得与 allow_credentials 同用通配符。
    # OpenAPI/Swagger 仅在 debug 开启，生产不暴露接口清单
    app = FastAPI(
        title=settings.app_name,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )
    app.add_middleware(ClientIpMiddleware)
    request_metrics = RequestMetrics()
    app.state.request_metrics = request_metrics
    app.add_middleware(RequestMetricsMiddleware, metrics=request_metrics)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Expose aggregate operational metrics only on the backend listener."""
        return Response(
            content=request_metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        # The ordinary liveness endpoint is intentionally stable. It never
        # exposes an E2E identifier, including when an isolated E2E stack runs.
        return {"status": "ok"}

    @app.get("/api/health")
    def api_health() -> dict[str, str]:
        response = {"status": "ok"}
        # Marker values identify a disposable test stack; they are deliberately
        # not secrets. Production does not configure one and receives the
        # unchanged health response above.
        if settings.e2e_target_marker:
            response["e2e_target_marker"] = settings.e2e_target_marker
        return response

    @app.get("/health/ready")
    @app.get("/api/health/ready")
    def readiness() -> dict[str, str]:
        """Only report ready when the authoritative payroll database is reachable."""
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            # Do not leak database topology or credentials through an unauthenticated
            # operational endpoint.  The non-2xx response makes container orchestration
            # remove this instance from service until its database dependency recovers.
            raise HTTPException(status_code=503, detail="Database is unavailable.") from None
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(org_router)
    app.include_router(employee_router)
    app.include_router(employee_tax_router)
    app.include_router(grade_router)
    app.include_router(imports_router)
    app.include_router(salary_router)
    app.include_router(comp_router)
    app.include_router(structure_router)
    app.include_router(approval_router)
    app.include_router(attendance_router)
    app.include_router(attendance_schedule_router)
    app.include_router(holiday_router)
    app.include_router(payroll_policy_router)
    app.include_router(audit_router)
    app.include_router(budget_router)
    app.include_router(dashboard_router)
    app.include_router(export_router)
    app.include_router(dingtalk_router)
    app.include_router(dingtalk_sync_router)
    app.include_router(payroll_adjustment_router)
    app.include_router(payroll_router)
    app.include_router(payslip_router)
    app.include_router(batch_router)
    app.include_router(users_router)
    get_logger("app").info("应用已启动", extra={"context": {"app": settings.app_name}})
    return app


app = create_app()
