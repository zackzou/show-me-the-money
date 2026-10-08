FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 中文字体：早报长图在服务端用 Pillow 渲染（/api/brief/image），
# 没有 CJK 字体时中文会变成方框。fonts-noto-cjk 约 60MB，值得。
# 彩色 emoji 字体（fonts-noto-color-emoji）：天气卡的 ☀️/👕 没有它
# 会渲染成豆腐块。
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
