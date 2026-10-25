# HeyBuddy backend. Provider keys come from the platform's secret store at runtime, never from the image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN useradd --create-home --uid 10001 heybuddy
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY heybuddy_backend ./heybuddy_backend
RUN mkdir -p /data && chown heybuddy /data
USER heybuddy
ENV HEYBUDDY_ENV=production HEYBUDDY_DB_PATH=/data/heybuddy.db PORT=8080
EXPOSE 8080
# --proxy-headers: trust X-Forwarded-Proto from the platform's TLS-terminating proxy (HTTPS is enforced in-app).
CMD ["sh", "-c", "uvicorn heybuddy_backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
