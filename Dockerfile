# ایمیج تولیدی برای OpenShift.
# نکته‌ی مهم: OpenShift با UID تصادفی و GID=0 اجرا می‌کند،
# پس همه‌ی فایل‌ها باید group-writable باشند.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HEALTH_PORT=8080 \
    TZ=Asia/Tehran

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py ./
COPY src ./src
COPY config ./config

# OpenShift با UID تصادفی و GID=0 اجرا می‌کند،
# پس همه‌ی فایل‌ها باید group-writable باشند.
# عمداً useradd نمی‌زنیم: UID عددی برای این برنامه کافی است و
# وابستگی به بسته‌ی passwd را حذف می‌کند.
RUN mkdir -p /app/data \
 && chgrp -R 0 /app \
 && chmod -R g=u /app

USER 1001

EXPOSE 8080

CMD ["python", "run.py"]
