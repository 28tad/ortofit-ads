"""Клиент Reports API Яндекс.Директа v5.

Только чтение статистики — методов записи здесь нет.
Ожидание отчёта (коды 201/202) спрятано внутри get_report: наружу
отдаются уже готовые строки.
"""

from __future__ import annotations

import os
import time
import uuid

import requests
from dotenv import load_dotenv

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"

# Единственный формат, который отдаёт Reports API. CSV делаем сами в fetch.py.
REPORT_FORMAT = "TSV"

# Потолок ожидания офлайн-отчёта: 60 попыток по <= 60 секунд.
MAX_ATTEMPTS = 60
MIN_RETRY_SECONDS = 3
MAX_RETRY_SECONDS = 60
REQUEST_TIMEOUT = 300


class DirectError(RuntimeError):
    """Ошибка Reports API или превышение числа попыток."""


def load_token() -> str:
    """Читает YANDEX_DIRECT_TOKEN из .env или переменных окружения."""
    load_dotenv()
    token = os.environ.get("YANDEX_DIRECT_TOKEN", "").strip()
    if not token:
        raise DirectError(
            "YANDEX_DIRECT_TOKEN не найден. Положи в корень проекта файл .env "
            "со строкой YANDEX_DIRECT_TOKEN=<токен> — без кавычек и без точки с запятой."
        )
    return token


class DirectClient:
    """Одна сессия к Reports API для одного аккаунта.

    Client-Login не передаём: аккаунт свой, а не клиентский под агентством.
    """

    def __init__(self, token: str | None = None, *, include_vat: bool = True) -> None:
        self.include_vat = include_vat
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token or load_token()}",
                "Accept-Language": "ru",
                "Content-Type": "application/json; charset=utf-8",
                # auto — Директ сам решает, отдать отчёт сразу или поставить в очередь.
                "processingMode": "auto",
                # false — деньги приходят в рублях, а не в миллионных долях.
                "returnMoneyInMicros": "false",
                # Шапку, названия колонок и строку итогов собираем сами, поэтому
                # в TSV остаются только данные и парсинг не зависит от вёрстки отчёта.
                "skipReportHeader": "true",
                "skipColumnHeader": "true",
                "skipReportSummary": "true",
            }
        )

    def get_report(
        self,
        *,
        report_type: str,
        field_names: list[str],
        date_range_type: str,
        date_from: str | None = None,
        date_to: str | None = None,
        goals: list[str] | None = None,
    ) -> list[list[str]]:
        """Запрашивает отчёт и ждёт его готовности. Возвращает строки без заголовка."""
        body = self._build_body(
            report_type=report_type,
            field_names=field_names,
            date_range_type=date_range_type,
            date_from=date_from,
            date_to=date_to,
            goals=goals,
        )

        # Тело собрано один раз до цикла намеренно: ReportName должен совпадать
        # во всех попытках, иначе каждый повтор заказывал бы новый отчёт.
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self.session.post(REPORTS_URL, json=body, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                return _parse_tsv(response)

            if response.status_code in (201, 202):
                delay = _retry_delay(response)
                queued = response.headers.get("reportsInQueue", "?")
                print(
                    f"  отчёт формируется (попытка {attempt}/{MAX_ATTEMPTS}, "
                    f"в очереди: {queued}), жду {delay} с",
                    flush=True,
                )
                time.sleep(delay)
                continue

            raise DirectError(_describe_error(response))

        raise DirectError(
            f"Отчёт {report_type} не сформировался за {MAX_ATTEMPTS} попыток. "
            "Попробуй сузить период."
        )

    def _build_body(
        self,
        *,
        report_type: str,
        field_names: list[str],
        date_range_type: str,
        date_from: str | None,
        date_to: str | None,
        goals: list[str] | None,
    ) -> dict:
        if date_range_type == "CUSTOM_DATE":
            if not (date_from and date_to):
                raise DirectError("Для CUSTOM_DATE нужны обе даты: --from и --to.")
            selection = {"DateFrom": date_from, "DateTo": date_to}
        else:
            # DateFrom/DateTo недопустимы при любом DateRangeType кроме CUSTOM_DATE,
            # но сам SelectionCriteria обязателен всегда — отсюда пустой объект.
            selection = {}

        params = {
            "SelectionCriteria": selection,
            "FieldNames": list(field_names),
            # Имя должно быть уникальным: Директ вернёт ошибку, если отчёт с тем же
            # именем, но другими параметрами уже сформирован или стоит в очереди.
            "ReportName": f"{report_type}-{uuid.uuid4().hex[:12]}",
            "ReportType": report_type,
            "DateRangeType": date_range_type,
            "Format": REPORT_FORMAT,
            "IncludeVAT": "YES" if self.include_vat else "NO",
        }
        if goals:
            params["Goals"] = [str(goal) for goal in goals]

        return {"params": params}


def _parse_tsv(response: requests.Response) -> list[list[str]]:
    # Директ отдаёт UTF-8, но не всегда объявляет кодировку в Content-Type,
    # и тогда requests молча угадывает latin-1 и портит кириллицу.
    response.encoding = "utf-8"
    return [line.split("\t") for line in response.text.splitlines() if line.strip()]


def _retry_delay(response: requests.Response) -> int:
    """Пауза из заголовка retryIn, зажатая в разумные границы."""
    try:
        seconds = int(response.headers.get("retryIn", ""))
    except (TypeError, ValueError):
        seconds = MIN_RETRY_SECONDS
    return max(MIN_RETRY_SECONDS, min(seconds, MAX_RETRY_SECONDS))


def _describe_error(response: requests.Response) -> str:
    """Собирает читаемое описание ошибки: код, текст, детали, RequestId."""
    request_id = response.headers.get("RequestId", "—")

    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        body = response.text.strip()[:500] or "<пустой ответ>"
        message = f"HTTP {response.status_code}, RequestId {request_id}: {body}"
    else:
        details = " ".join(
            part
            for part in (error.get("error_string"), error.get("error_detail"))
            if part
        )
        message = (
            f"HTTP {response.status_code}, код {error.get('error_code', '?')}, "
            f"RequestId {request_id}: {details}"
        )

    if response.status_code == 502:
        message += (
            "\n  Подсказка: 502 у Reports API означает, что отчёт не успел "
            "сформироваться. Запроси меньший период."
        )
    return message
