from fastapi import FastAPI

from app.auth.router import router as auth_router
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
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

    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    return app


app = create_app()
