"""Сверяет YAML-описание кампании с аккаунтом Директа и печатает план изменений.

    python -m src.plan campaigns/arenda-poisk.yaml            # предпросмотр
    python -m src.plan campaigns/arenda-poisk.yaml --apply    # применение

По умолчанию — предварительный просмотр: скрипт читает аккаунт методами get
и печатает, что изменилось бы. Ничего не отправляется, пока нет --apply.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .api import ApiError, DirectApi, from_micros, to_micros
from .direct import DirectError

# Лимиты Директа на тексты объявлений. У Title2 и Text «узкие» символы !,.;:"
# в лимит не входят (до 15 штук сверх), поэтому проверка ниже строже реальной —
# прошедшее её объявление Директ примет гарантированно.
MAX_TITLE = 56
MAX_TITLE2 = 30
MAX_TEXT = 81
MAX_TITLE_WORD = 22

MAX_KEYWORD_WORDS = 7
MAX_KEYWORD_WORD = 35

BANNER = "РЕЖИМ ПРЕДВАРИТЕЛЬНОГО ПРОСМОТРА — изменения не применяются"

CREATE, UPDATE, UNCHANGED = "create", "update", "unchanged"

KIND_NAMES = {
    "campaign": ("кампания", "кампании", "кампаний"),
    "group": ("группа", "группы", "групп"),
    "keyword": ("фраза", "фразы", "фраз"),
    "ad": ("объявление", "объявления", "объявлений"),
}


@dataclass
class Change:
    action: str
    kind: str
    name: str
    lines: list[str] = field(default_factory=list)


@dataclass
class AccountState:
    """Текущее состояние аккаунта по одной кампании."""

    campaign: dict | None = None
    groups: dict[str, dict] = field(default_factory=dict)
    keywords: set[tuple[str, str]] = field(default_factory=set)
    ads: dict[tuple[str, str], dict] = field(default_factory=dict)


def plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


# --- чтение и валидация файла ---------------------------------------------


def load_spec(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DirectError(f"Не читается {path}: {error}") from None

    try:
        spec = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise DirectError(f"{path}: не разбирается как YAML.\n{error}") from None

    if not isinstance(spec, dict):
        raise DirectError(f"{path}: ожидался словарь с ключами campaign и groups")
    return spec


def validate(spec: dict) -> list[str]:
    """Возвращает список ошибок. Пустой список — файл пригоден к применению."""
    errors: list[str] = []

    campaign = spec.get("campaign")
    if not isinstance(campaign, dict):
        return ["Нет секции campaign"]

    for key in ("name", "daily_budget", "avg_cpc_limit"):
        if campaign.get(key) in (None, ""):
            errors.append(f"campaign: не заполнено поле {key}")
    if not campaign.get("regions"):
        errors.append("campaign: не указаны regions — Директ требует хотя бы один регион")

    groups = spec.get("groups")
    if not isinstance(groups, list) or not groups:
        errors.append("Нет ни одной группы в секции groups")
        return errors

    for index, group in enumerate(groups, start=1):
        name = (group or {}).get("name") or f"<без имени, №{index}>"

        for keyword in (group or {}).get("keywords") or []:
            _check_keyword(errors, name, str(keyword))

        ads = (group or {}).get("ads") or []
        if not ads:
            errors.append(f"группа «{name}»: нет ни одного объявления")

        for ad in ads:
            _check_length(errors, name, "заголовок", ad.get("title", ""), MAX_TITLE)
            _check_length(errors, name, "второй заголовок", ad.get("title2", ""), MAX_TITLE2)
            _check_length(errors, name, "текст", ad.get("text", ""), MAX_TEXT)

            # Отдельный лимит Директа: длинное слово в заголовке роняет
            # объявление, даже когда сам заголовок укладывается в 56 символов.
            for word in str(ad.get("title", "")).split():
                if len(word) > MAX_TITLE_WORD:
                    errors.append(
                        f"группа «{name}»: слово «{word}» в заголовке — "
                        f"{len(word)} символов при лимите {MAX_TITLE_WORD}"
                    )
            if not ad.get("href"):
                errors.append(f"группа «{name}»: у объявления не указан href")

    return errors


def _check_length(errors: list[str], group: str, label: str, value, limit: int) -> None:
    text = str(value or "")
    if not text:
        errors.append(f"группа «{group}»: не заполнен {label}")
    elif len(text) > limit:
        errors.append(
            f"группа «{group}»: {label} — {len(text)} "
            f"{plural(len(text), 'символ', 'символа', 'символов')} "
            f"при лимите {limit}: «{text}»"
        )


def _check_keyword(errors: list[str], group: str, keyword: str) -> None:
    words = keyword.split()
    if len(words) > MAX_KEYWORD_WORDS:
        errors.append(
            f"группа «{group}»: фраза «{keyword}» — {len(words)} слов "
            f"при лимите {MAX_KEYWORD_WORDS}"
        )
    for word in words:
        if len(word) > MAX_KEYWORD_WORD:
            errors.append(
                f"группа «{group}»: слово «{word}» во фразе — "
                f"{len(word)} символов при лимите {MAX_KEYWORD_WORD}"
            )


# --- чтение состояния аккаунта --------------------------------------------


def read_state(api: DirectApi, campaign_name: str) -> AccountState:
    wanted = campaign_name.strip()
    campaign = next(
        (c for c in api.get_campaigns() if c.get("Name", "").strip() == wanted), None
    )
    if campaign is None:
        return AccountState()

    campaign_id = campaign["Id"]
    groups = api.get_ad_groups([campaign_id])
    name_by_id = {g["Id"]: g["Name"] for g in groups}

    keywords = {
        (name_by_id.get(k["AdGroupId"], ""), k["Keyword"].strip().lower())
        for k in api.get_keywords([campaign_id])
    }
    ads = {
        (name_by_id.get(a["AdGroupId"], ""), a.get("TextAd", {}).get("Title", "")): a
        for a in api.get_ads([campaign_id])
        if "TextAd" in a
    }

    return AccountState(
        campaign=campaign,
        groups={g["Name"]: g for g in groups},
        keywords=keywords,
        ads=ads,
    )


# --- сличение --------------------------------------------------------------


def build_plan(spec: dict, state: AccountState) -> tuple[list[Change], list[str]]:
    """Возвращает список изменений и список лишнего, что есть только в аккаунте."""
    changes: list[Change] = []
    campaign = spec["campaign"]

    changes.append(_campaign_change(campaign, state.campaign))

    for group in spec["groups"]:
        name = group["name"]
        known = state.groups.get(name)
        changes.append(
            Change(
                UNCHANGED if known else CREATE,
                "group",
                name,
                [] if known else [f"регионы: {_regions(campaign)}"],
            )
        )

        for keyword in group.get("keywords") or []:
            exists = (name, str(keyword).strip().lower()) in state.keywords
            changes.append(Change(UNCHANGED if exists else CREATE, "keyword", str(keyword)))

        for ad in group.get("ads") or []:
            changes.append(_ad_change(name, ad, state.ads.get((name, ad.get("title", "")))))

    return changes, _extras(spec, state)


def _campaign_change(campaign: dict, known: dict | None) -> Change:
    lines = [
        f"дневной бюджет {campaign['daily_budget']} ₽",
        f"стратегия: {campaign.get('strategy', '—')}, до {campaign['avg_cpc_limit']} ₽ за клик",
        "показы в сетях отключены (кампания поисковая)",
        f"регионы: {_regions(campaign)}",
        f"минус-слов: {len(campaign.get('negative_keywords') or [])}",
    ]
    if known is None:
        return Change(CREATE, "campaign", campaign["name"], lines)

    diffs = []
    budget = known.get("DailyBudget") or {}
    if "Amount" in budget:
        current = from_micros(budget["Amount"])
        if to_micros(current) != to_micros(campaign["daily_budget"]):
            diffs.append(f"дневной бюджет: {current:g} ₽ -> {campaign['daily_budget']} ₽")

    current_negatives = set((known.get("NegativeKeywords") or {}).get("Items") or [])
    wanted_negatives = set(campaign.get("negative_keywords") or [])
    if added := wanted_negatives - current_negatives:
        diffs.append(f"минус-слова: +{len(added)} ({', '.join(sorted(added)[:5])}...)")

    return Change(UPDATE if diffs else UNCHANGED, "campaign", campaign["name"], diffs)


def _ad_change(group: str, ad: dict, known: dict | None) -> Change:
    title = ad.get("title", "")
    if known is None:
        return Change(CREATE, "ad", title, [ad.get("title2", ""), ad.get("text", ""), ad.get("href", "")])

    text_ad = known.get("TextAd", {})
    diffs = [
        f"{label}: «{text_ad.get(api_field, '')}» -> «{ad.get(yaml_field, '')}»"
        for label, api_field, yaml_field in (
            ("второй заголовок", "Title2", "title2"),
            ("текст", "Text", "text"),
            ("ссылка", "Href", "href"),
        )
        if text_ad.get(api_field, "") != ad.get(yaml_field, "")
    ]
    return Change(UPDATE if diffs else UNCHANGED, "ad", title, diffs)


def _extras(spec: dict, state: AccountState) -> list[str]:
    """Что есть в аккаунте, но отсутствует в файле. Показываем, но не удаляем."""
    if state.campaign is None:
        return []

    extras = []
    wanted_groups = {g["name"] for g in spec["groups"]}
    for name in sorted(set(state.groups) - wanted_groups):
        extras.append(f"группа «{name}»")

    wanted_keywords = {
        (g["name"], str(k).strip().lower())
        for g in spec["groups"]
        for k in (g.get("keywords") or [])
    }
    for group, keyword in sorted(state.keywords - wanted_keywords):
        extras.append(f"фраза «{keyword}» в группе «{group}»")

    return extras


def _regions(campaign: dict) -> str:
    return ", ".join(str(r) for r in campaign.get("regions") or [])


# --- вывод -----------------------------------------------------------------


def print_plan(path: Path, spec: dict, changes: list[Change], extras: list[str], preview: bool) -> None:
    if preview:
        print(BANNER)
        print("=" * len(BANNER))
    print(f"\nФайл: {path}")
    print(f"Кампания: {spec['campaign']['name']}\n")

    for action, header in ((CREATE, "СОЗДАТЬ"), (UPDATE, "ОБНОВИТЬ")):
        selected = [c for c in changes if c.action == action]
        if not selected:
            continue
        print(header)
        for change in selected:
            label = KIND_NAMES[change.kind][0]
            indent = "    " if change.kind in ("keyword", "ad") else "  "
            print(f"{indent}{label:<11} {change.name}")
            for line in change.lines:
                if line:
                    print(f"{indent}{'':<11} {line}")
        print()

    unchanged = [c for c in changes if c.action == UNCHANGED]
    if unchanged:
        counts = Counter(c.kind for c in unchanged)
        print("БЕЗ ИЗМЕНЕНИЙ")
        print(f"  {_describe(counts)}\n")

    if extras:
        print("ЕСТЬ В АККАУНТЕ, НО НЕТ В ФАЙЛЕ (не трогаем)")
        for extra in extras:
            print(f"  {extra}")
        print()

    print("ИТОГО")
    for action, label in ((CREATE, "будет создано"), (UPDATE, "будет обновлено")):
        counts = Counter(c.kind for c in changes if c.action == action)
        print(f"  {label}: {_describe(counts) if counts else '—'}")


def _describe(counts: Counter) -> str:
    order = ("campaign", "group", "keyword", "ad")
    parts = [
        f"{counts[kind]} {plural(counts[kind], *KIND_NAMES[kind])}"
        for kind in order
        if counts.get(kind)
    ]
    return ", ".join(parts) if parts else "—"


# --- применение ------------------------------------------------------------


def campaign_payload(campaign: dict) -> dict:
    return {
        "Name": campaign["name"],
        "NegativeKeywords": {"Items": list(campaign.get("negative_keywords") or [])},
        "DailyBudget": {
            "Amount": to_micros(campaign["daily_budget"]),
            "Mode": "STANDARD",
        },
        "TextCampaign": {
            # WB_MAXIMUM_CLICKS — это и есть «максимум кликов, ограничивать по
            # средней цене клика». Network SERVING_OFF выключает показы в РСЯ.
            # Единственный блок, который нельзя проверить на живом API, пока не
            # выдано право на запись: структуру стратегий Директ меняет чаще
            # остального, так что при включении доступа сверить в первую очередь.
            "BiddingStrategy": {
                "Search": {
                    "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                    "WbMaximumClicks": {
                        "AverageCpcLimit": to_micros(campaign["avg_cpc_limit"])
                    },
                },
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            }
        },
    }


def apply_plan(api: DirectApi, spec: dict, state: AccountState, changes: list[Change]) -> int:
    campaign = spec["campaign"]

    if not any(c.action in (CREATE, UPDATE) for c in changes):
        print("\nНечего применять: аккаунт уже соответствует файлу.")
        return 0

    print("\n" + "!" * 60)
    print("ПРИМЕНЕНИЕ ИЗМЕНЕНИЙ. Кампания будет создана в реальном аккаунте.")
    print("!" * 60)

    if not sys.stdin.isatty():
        print("--apply требует интерактивного терминала для подтверждения.", file=sys.stderr)
        return 1

    if input('Введите "да" для применения: ').strip().lower() != "да":
        print("Отменено. Ничего не отправлено.")
        return 1

    # Регионы в YAML заданы на кампанию, а Директ требует их на каждой группе —
    # разворачиваем здесь.
    regions = list(campaign.get("regions") or [])

    if state.campaign is None:
        campaign_id = api.add_campaign(campaign_payload(campaign))
        print(f"  создана кампания {campaign_id}")
    else:
        campaign_id = state.campaign["Id"]
        print(f"  кампания уже есть: {campaign_id}")

    group_ids = {name: group["Id"] for name, group in state.groups.items()}
    new_groups = [g for g in spec["groups"] if g["name"] not in group_ids]
    if new_groups:
        created = api.add_ad_groups(
            [
                {"Name": g["name"], "CampaignId": campaign_id, "RegionIds": regions}
                for g in new_groups
            ]
        )
        group_ids.update(zip((g["name"] for g in new_groups), created))
        print(f"  создано групп: {len(created)}")

    keywords, ads = [], []
    for group in spec["groups"]:
        group_id = group_ids[group["name"]]
        for keyword in group.get("keywords") or []:
            if (group["name"], str(keyword).strip().lower()) not in state.keywords:
                keywords.append({"Keyword": str(keyword), "AdGroupId": group_id})
        for ad in group.get("ads") or []:
            if (group["name"], ad.get("title", "")) not in state.ads:
                ads.append(
                    {
                        "AdGroupId": group_id,
                        "TextAd": {
                            "Title": ad["title"],
                            "Title2": ad.get("title2", ""),
                            "Text": ad["text"],
                            "Href": ad["href"],
                            # Поле объявлено устаревшим, но остаётся обязательным.
                            "Mobile": "NO",
                        },
                    }
                )

    if keywords:
        print(f"  создано фраз: {len(api.add_keywords(keywords))}")
    if ads:
        print(f"  создано объявлений: {len(api.add_ads(ads))}")

    print("\nГотово.")
    return 0


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.plan",
        description="Сверяет YAML-описание кампании с аккаунтом Яндекс.Директа",
    )
    parser.add_argument("spec", type=Path, help="путь к YAML-файлу кампании")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="применить изменения (по умолчанию — только предварительный просмотр)",
    )
    args = parser.parse_args(argv)

    try:
        spec = load_spec(args.spec)
    except DirectError as error:
        print(error, file=sys.stderr)
        return 1

    if errors := validate(spec):
        print("ОШИБКИ В ОПИСАНИИ КАМПАНИИ", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        print(f"\nВсего ошибок: {len(errors)}. План не строился.", file=sys.stderr)
        return 1

    try:
        api = DirectApi()
        state = read_state(api, spec["campaign"]["name"])
    except (ApiError, DirectError) as error:
        print(f"Не удалось прочитать аккаунт: {error}", file=sys.stderr)
        return 1

    changes, extras = build_plan(spec, state)
    print_plan(args.spec, spec, changes, extras, preview=not args.apply)

    if not args.apply:
        print(
            f"\nНичего не отправлено. Для применения:\n"
            f"  python -m src.plan {args.spec} --apply"
        )
        return 0

    try:
        return apply_plan(api, spec, state, changes)
    except ApiError as error:
        print(f"\nДирект отклонил запрос: {error}", file=sys.stderr)
        print(
            "\nЕсли в ошибке речь про права доступа — это ожидаемо: у токена "
            "сейчас доступ только на чтение, методы записи Директ не пропускает.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
