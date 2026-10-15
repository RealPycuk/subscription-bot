"""Метрики Prometheus: апдейты Telegram, API панели, бизнес-показатели, мониторинг и бэкапы.

Эндпоинт /metrics поднимается на 127.0.0.1:METRICS_PORT (по умолчанию 9101) и наружу не публикуется.
"""
from prometheus_client import Counter, Gauge, Histogram, start_http_server

UPDATES = Counter("bot_updates_total", "Апдейты Telegram, полученные ботом", ["type"])
HANDLER_ERRORS = Counter("bot_handler_errors_total", "Необработанные исключения в хендлерах")
HANDLER_SECONDS = Histogram("bot_handler_duration_seconds", "Время обработки одного апдейта",
                            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))

PANEL_SECONDS = Histogram("bot_panel_request_duration_seconds", "Время запроса к API панели", ["method"],
                          buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20))
PANEL_ERRORS = Counter("bot_panel_errors_total", "Ошибки запросов к API панели")

USERS = Gauge("bot_users", "Пользователи бота")
SUBSCRIBERS = Gauge("bot_users_with_subscription", "Пользователи с привязанной подпиской")
PAYMENTS = Gauge("bot_payments_paid", "Оплаченные платежи за всё время", ["currency"])
REVENUE = Gauge("bot_revenue", "Сумма оплат за всё время", ["currency"])

REGION_UP = Gauge("bot_region_up", "Регион доступен по данным встроенного мониторинга (1/0)", ["region"])
PROBLEMS = Gauge("bot_monitor_problems", "Активные проблемы встроенного мониторинга")

BACKUP_TS = Gauge("bot_last_backup_success_timestamp_seconds", "Время последнего успешного бэкапа (unix)")
BACKUP_SIZE = Gauge("bot_last_backup_size_bytes", "Размер последнего дампа базы панели")


def serve(port: int) -> None:
    start_http_server(port, addr="127.0.0.1")
