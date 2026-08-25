"""Клиент JSON API v5 Яндекс.Директа: кампании, группы, объявления, фразы.

Reports API живёт отдельно, в direct.py: там другой эндпоинт и TSV вместо JSON.

Право на запись у аккаунта есть с 13 августа 2026, и методы add/update/moderate
здесь рабочие: вызов действительно меняет живой кабинет. Из plan.py они
вызываются только из-под флага --apply с подтверждением с клавиатуры.
"""

from __future__ import annotations

import requests

from .direct import DirectError, load_token

# Версия v501, а не v5: только в ней объявления ЕПК отдаются как RESPONSIVE_AD
# с массивами заголовков и текстов. В v5 те же объявления приходят как TEXT_AD
# с одним Title — даже у объявления с тремя заголовками, — а Title2 всегда пуст.
# Остальные сервисы (campaigns, adgroups, keywords) в v501 работают как в v5.
API_BASE = "https://api.direct.yandex.com/json/v501"

# Все денежные параметры Директ принимает целыми числами, умноженными на миллион.
MONEY_MULTIPLIER = 1_000_000

REQUEST_TIMEOUT = 120

# Директ отдаёт объекты страницами; лимит на страницу зависит от сервиса,
# 10 000 — потолок для большинства.
PAGE_LIMIT = 10_000


class ApiError(DirectError):
    """Ошибка JSON API v5."""


def to_micros(amount: float | int) -> int:
    """Рубли -> целое, умноженное на миллион."""
    return int(round(float(amount) * MONEY_MULTIPLIER))


def from_micros(value: int | str) -> float:
    """Целое из API -> рубли."""
    return int(value) / MONEY_MULTIPLIER


class DirectApi:
    """Одна сессия к JSON API v5 для одного аккаунта."""

    def __init__(self, token: str | None = None) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token or load_token()}",
                "Accept-Language": "ru",
                "Content-Type": "application/json; charset=utf-8",
            }
        )
        # Директ возвращает расход баллов в заголовке Units; складываем,
        # чтобы можно было показать цену прогона.
        self.units_spent = 0

    # --- транспорт -------------------------------------------------------

    def call(self, service: str, method: str, params: dict) -> dict:
        response = self.session.post(
            f"{API_BASE}/{service}",
            json={"method": method, "params": params},
            timeout=REQUEST_TIMEOUT,
        )
        # Директ отдаёт UTF-8, но не всегда объявляет кодировку.
        response.encoding = "utf-8"
        self._count_units(response)

        if response.status_code != 200:
            raise ApiError(
                f"{service}.{method}: HTTP {response.status_code}, "
                f"RequestId {response.headers.get('RequestId', '—')}: "
                f"{response.text.strip()[:400]}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise ApiError(f"{service}.{method}: ответ не разобрался как JSON") from None

        # Ошибки уровня запроса приезжают с HTTP 200 и ключом error —
        # именно сюда попадёт отказ из-за отсутствия права на запись.
        if "error" in payload:
            error = payload["error"]
            raise ApiError(
                f"{service}.{method}: код {error.get('error_code', '?')}, "
                f"RequestId {error.get('request_id', '—')}: "
                f"{error.get('error_string', '')} {error.get('error_detail', '')}".strip()
            )

        return payload.get("result", {})

    def _count_units(self, response: requests.Response) -> None:
        # Формат заголовка: "списано/осталось/суточный лимит".
        units = response.headers.get("Units", "")
        try:
            self.units_spent += int(units.split("/")[0])
        except (ValueError, IndexError):
            pass

    def get_all(self, service: str, params: dict, collection: str) -> list[dict]:
        """Читает все страницы выборки и возвращает объекты одним списком."""
        items: list[dict] = []
        offset = 0
        while True:
            page_params = dict(params)
            page_params["Page"] = {"Limit": PAGE_LIMIT, "Offset": offset}
            result = self.call(service, "get", page_params)
            items.extend(result.get(collection, []))

            limited_by = result.get("LimitedBy")
            if limited_by is None:
                return items
            offset = int(limited_by)

    # --- чтение ----------------------------------------------------------

    def get_campaigns(self) -> list[dict]:
        return self.get_all(
            "campaigns",
            {
                "SelectionCriteria": {},
                "FieldNames": [
                    "Id",
                    "Name",
                    "Type",
                    "State",
                    "Status",
                    "DailyBudget",
                    "NegativeKeywords",
                ],
                "TextCampaignFieldNames": ["BiddingStrategy"],
            },
            "Campaigns",
        )

    def get_ad_groups(self, campaign_ids: list[int]) -> list[dict]:
        if not campaign_ids:
            return []
        return self.get_all(
            "adgroups",
            {
                "SelectionCriteria": {"CampaignIds": campaign_ids},
                "FieldNames": ["Id", "Name", "CampaignId", "RegionIds", "NegativeKeywords"],
            },
            "AdGroups",
        )

    def get_ads(self, campaign_ids: list[int]) -> list[dict]:
        if not campaign_ids:
            return []
        return self.get_all(
            "ads",
            {
                "SelectionCriteria": {"CampaignIds": campaign_ids},
                "FieldNames": ["Id", "CampaignId", "AdGroupId", "State", "Status"],
                # На чтении элементы массивов — объекты {"Title": ..., "Status": ...},
                # на записи те же массивы принимают просто строки.
                "ResponsiveAdFieldNames": ["Titles", "Texts", "Href"],
            },
            "Ads",
        )

    def get_keywords(self, campaign_ids: list[int]) -> list[dict]:
        if not campaign_ids:
            return []
        return self.get_all(
            "keywords",
            {
                "SelectionCriteria": {"CampaignIds": campaign_ids},
                "FieldNames": ["Id", "Keyword", "AdGroupId", "State", "Status"],
            },
            "Keywords",
        )

    # --- запись ----------------------------------------------------------
    # Методы ниже реально меняют аккаунт. Отката нет: перед вызовом должен
    # пройти предпросмотр в plan.py и подтверждение человеком.

    def add_campaign(self, campaign: dict) -> int:
        result = self.call("campaigns", "add", {"Campaigns": [campaign]})
        return _single_id(result, "AddResults", "кампании")

    def add_ad_groups(self, ad_groups: list[dict]) -> list[int]:
        result = self.call("adgroups", "add", {"AdGroups": ad_groups})
        return _all_ids(result, "AddResults", "групп")

    def add_ads(self, ads: list[dict]) -> list[int]:
        result = self.call("ads", "add", {"Ads": ads})
        return _all_ids(result, "AddResults", "объявлений")

    def add_keywords(self, keywords: list[dict]) -> list[int]:
        result = self.call("keywords", "add", {"Keywords": keywords})
        return _all_ids(result, "AddResults", "фраз")

    def update_ad_groups(self, ad_groups: list[dict]) -> list[int]:
        result = self.call("adgroups", "update", {"AdGroups": ad_groups})
        return _all_ids(result, "UpdateResults", "групп")

    def update_campaign(self, campaign: dict) -> int:
        result = self.call("campaigns", "update", {"Campaigns": [campaign]})
        return _single_id(result, "UpdateResults", "кампании")

    def update_ads(self, ads: list[dict]) -> list[int]:
        result = self.call("ads", "update", {"Ads": ads})
        return _all_ids(result, "UpdateResults", "объявлений")

    def set_bids(self, bids: list[dict]) -> int:
        """Ставки фраз: [{"KeywordId": ..., "Bid": micros}]. Возвращает число удач."""
        result = self.call("bids", "set", {"Bids": bids})
        problems = []
        for index, item in enumerate(result.get("SetResults", [])):
            for error in item.get("Errors", []):
                problems.append(
                    f"  #{index + 1}: код {error.get('Code', '?')} "
                    f"{error.get('Message', '')} {error.get('Details', '')}".rstrip()
                )
        if problems:
            raise ApiError("Директ отклонил часть ставок:\n" + "\n".join(problems))
        return len(bids)

    def moderate_ads(self, ad_ids: list[int]) -> list[int]:
        """Отправляет черновики на модерацию. Принимает только статус DRAFT.

        Созданные через API объявления остаются черновиками и сами на модерацию
        не уходят — без этого вызова группа не даёт показов.
        """
        result = self.call("ads", "moderate", {"SelectionCriteria": {"Ids": ad_ids}})
        return _all_ids(result, "ModerateResults", "объявлений")


def _all_ids(result: dict, key: str, what: str) -> list[int]:
    """Достаёт Id из ответа, собирая ошибки по отдельным объектам."""
    items = result.get(key, [])
    ids, problems = [], []

    for index, item in enumerate(items):
        # Директ отвечает поштучно: часть объектов может создаться,
        # часть — упасть, и молча терять вторые нельзя.
        for error in item.get("Errors", []):
            problems.append(
                f"  #{index + 1}: код {error.get('Code', '?')} "
                f"{error.get('Message', '')} {error.get('Details', '')}".rstrip()
            )
        for warning in item.get("Warnings", []):
            print(f"  предупреждение #{index + 1}: {warning.get('Message', '')}")
        if "Id" in item:
            ids.append(item["Id"])

    if problems:
        raise ApiError(f"Директ отклонил часть {what}:\n" + "\n".join(problems))
    return ids


def _single_id(result: dict, key: str, what: str) -> int:
    ids = _all_ids(result, key, what)
    if not ids:
        raise ApiError(f"Директ не вернул Id для {what}")
    return ids[0]
