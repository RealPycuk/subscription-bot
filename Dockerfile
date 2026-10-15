FROM python:3.12-alpine
RUN apk add --no-cache postgresql16-client ca-certificates tzdata \
 && adduser -D -u 10001 -h /app bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot/
COPY config/tariffs.json .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER bot
CMD ["python", "bot/main.py"]
