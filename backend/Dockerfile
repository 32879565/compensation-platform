FROM python:3.12-slim

WORKDIR /app

# 先装第三方依赖（此时 app/ 未拷入，setuptools 找不到包只装依赖），业务代码改动不触发依赖重装
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
RUN pip install --no-cache-dir --no-deps --force-reinstall .

# 非特权用户运行（承载薪资数据的服务默认加固）
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# --proxy-headers：经 nginx 反代时取真实客户端 IP（S4 审计依赖）。
# TODO(S4): forwarded-allow-ips 收敛为代理网段，防 XFF 伪造。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
