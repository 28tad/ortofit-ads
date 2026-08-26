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

from .api import ApiError, DirectApi, to_micros
from .direct import DirectError

# Лимиты Директа на тексты объявлений. У Text «узкие» символы !,.;:"
# в лимит не входят (до 15 штук сверх), поэтому проверка ниже строже реальной —
# прошедшее её объявление Директ примет гарантированно.
MAX_TITLE = 56
MAX_TEXT = 81
MAX_TITLE_WORD = 22

# У объявления ЕПК заголовков и текстов несколько — Директ комбинирует их сам.
# Потолки из интерфейса: до 7 заголовков и до 3 текстов.
MAX_TITLES = 7
MAX_TEXTS = 3

MAX_KEYWORD_WORDS = 7
MAX_KEYWORD_WORD = 35

# Требования Яндекса к рекламе медицинских изделий:
# https://yandex.ru/support/direct/ru/moderation/categories/medicine-medical-devices
# Кампания №713404098 уже была отклонена по этой тематике, поэтому тексты
# проверяются до отправки. Список literal: ищем ровно эти обороты, без попыток
# угадать словоформы — стем «лучш» ловил бы безобидное «улучшение».
MEDICAL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "создаёт впечатление, что обращение к врачу не требуется",
        (
            "не можете ехать в клинику",
            "без визита к врачу",
            "без врача",
            "не нужно в больницу",
            "замена клинике",
        ),
    ),
    (
        "сравнение с другими изделиями или заявление о превосходстве",
        (
            "золотой стандарт",
            "лучший",
            "эффективнее",
            "быстрее чем",
            "дешевле чем",
        ),
    ),
    (
        "гарантия эффективности, безопасности или отсутствия побочных действий",
        (
            "гарантируем",
            "без побочных",
            "безопасно для всех",
            "излечение",
            "вылечит",
        ),
    ),
    (
        "звучит как медицинская рекомендация",
        (
            "под ваш диагноз",
            "назначим",
            "подберём лечение",
        ),
    ),
)

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

    for key in ("name", "weekly_budget"):
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

        # В Единой перфоманс-кампании ставка и минус-фразы живут на группе,
        # общих настроек на кампанию для них нет.
        if (group or {}).get("max_cpc") in (None, ""):
            errors.append(f"группа «{name}»: не указана max_cpc")
        if not (group or {}).get("negative_keywords"):
            errors.append(f"группа «{name}»: не заданы negative_keywords")

        for keyword in (group or {}).get("keywords") or []:
            _check_keyword(errors, name, str(keyword))

        ads = (group or {}).get("ads") or []
        if not ads:
            errors.append(f"группа «{name}»: нет ни одного объявления")

        for ad in ads:
            # Одиночные title/title2/text — прежний формат. Объявление ЕПК несёт
            # массивы (API v501, структура ResponsiveAd), и старые ключи скрипт
            # молча проигнорировал бы — лучше остановиться и назвать замену.
            for legacy in ("title", "title2", "text"):
                if ad.get(legacy):
                    errors.append(
                        f"группа «{name}»: поле {legacy} больше не используется — "
                        f"объявление описывается массивами titles и texts"
                    )

            titles = [str(t) for t in ad.get("titles") or []]
            texts = [str(t) for t in ad.get("texts") or []]

            if not titles:
                errors.append(f"группа «{name}»: не заполнен ни один заголовок")
            elif len(titles) > MAX_TITLES:
                errors.append(
                    f"группа «{name}»: {len(titles)} заголовков при лимите {MAX_TITLES}"
                )
            if not texts:
                errors.append(f"группа «{name}»: не заполнен ни один текст")
            elif len(texts) > MAX_TEXTS:
                errors.append(
                    f"группа «{name}»: {len(texts)} текстов при лимите {MAX_TEXTS}"
                )

            for title in titles:
                _check_length(errors, name, "заголовок", title, MAX_TITLE)
                # Отдельный лимит Директа: длинное слово в заголовке роняет
                # объявление, даже когда сам заголовок укладывается в 56 символов.
                for word in title.split():
                    if len(word) > MAX_TITLE_WORD:
                        errors.append(
                            f"группа «{name}»: слово «{word}» в заголовке — "
                            f"{len(word)} символов при лимите {MAX_TITLE_WORD}"
                        )
            for text in texts:
                _check_length(errors, name, "текст", text, MAX_TEXT)

            if not ad.get("href"):
                errors.append(f"группа «{name}»: у объявления не указан href")

    return errors


def _normalize(text: str) -> str:
    """Регистр и ё к общему виду: «Подберём» и «подберем» — одно и то же."""
    return str(text or "").lower().replace("ё", "е")


def review_texts(spec: dict) -> list[str]:
    """Предупреждения по требованиям к рекламе медицинских изделий.

    Не ошибки: формулировку решает человек, скрипт лишь показывает, за что
    модерация уже отклоняла кампанию. Выполнение не прерывается.
    """
    warnings: list[str] = []

    def check(where: str, value) -> None:
        text = _normalize(value)
        if not text:
            return
        for reason, phrases in MEDICAL_RULES:
            for phrase in phrases:
                if _normalize(phrase) in text:
                    warnings.append(f'{where}: «{phrase}» — {reason}')

    campaign = spec.get("campaign") or {}
    for callout in campaign.get("callouts") or []:
        check("уточнение", callout)
    for sitelink in campaign.get("sitelinks") or []:
        check("быстрая ссылка", (sitelink or {}).get("title"))

    for index, group in enumerate(spec.get("groups") or [], start=1):
        name = (group or {}).get("name") or f"<без имени, №{index}>"
        for ad in (group or {}).get("ads") or []:
            for title in (ad or {}).get("titles") or []:
                check(f'группа «{name}», заголовок', title)
            for text in (ad or {}).get("texts") or []:
                check(f'группа «{name}», текст', text)

    return warnings


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


# Псевдофраза, которую Директ заводит в группе сам при включённом
# автотаргетинге. В файле кампании ей делать нечего, иначе она вечно висит
# в списке «есть в аккаунте, но нет в файле».
AUTOTARGETING_KEYWORD = "---autotargeting"

# Категории автотаргетинга: флаг YAML -> категория API. API v5 знает пять
# категорий; тумблеры narrow_queries, own_brand_queries и no_brand_queries
# существуют только в интерфейсе, через API они не читаются и не пишутся.
AUTOTARGETING_CATEGORIES = (
    ("target_queries", "EXACT"),
    ("alternative_queries", "ALTERNATIVE"),
    ("competitor_brand_queries", "COMPETITOR"),
    ("broad_queries", "BROADER"),
    ("accessory_queries", "ACCESSORY"),
)


def read_state(api: DirectApi, campaign: dict) -> AccountState:
    """Находит кампанию по id, а при его отсутствии — по имени.

    Директ называет кампании сам («Единая перфоманс-кампания №2 от 09-08-2026»),
    и поиск по имени промахивается, пока её не переименуют вручную. Id надёжнее.
    """
    wanted_id = campaign.get("id")
    wanted_name = str(campaign.get("name", "")).strip()

    found = None
    for item in api.get_campaigns():
        if wanted_id is not None:
            if item.get("Id") == wanted_id:
                found = item
                break
        elif item.get("Name", "").strip() == wanted_name:
            found = item
            break

    if found is None:
        return AccountState()

    campaign_id = found["Id"]
    groups = api.get_ad_groups([campaign_id])
    name_by_id = {g["Id"]: g["Name"] for g in groups}

    keywords = {
        (name_by_id.get(k["AdGroupId"], ""), k["Keyword"].strip().lower())
        for k in api.get_keywords([campaign_id])
        if k["Keyword"].strip().lower() != AUTOTARGETING_KEYWORD
    }
    # Объявление опознаётся по первому заголовку: он же показывается чаще всего,
    # и его изменение — это по сути новое объявление, а не правка старого.
    ads = {}
    for a in api.get_ads([campaign_id]):
        if "ResponsiveAd" not in a:
            continue
        titles = _ad_strings(a["ResponsiveAd"], "Titles", "Title")
        if titles:
            ads[(name_by_id.get(a["AdGroupId"], ""), titles[0])] = a

    return AccountState(
        campaign=found,
        groups={g["Name"]: g for g in groups},
        keywords=keywords,
        ads=ads,
    )


def _ad_strings(responsive_ad: dict, collection: str, key: str) -> list[str]:
    """Массив заголовков или текстов объявления как список строк.

    На чтении v501 отдаёт элементы объектами {"Title": ..., "Status": ...},
    а на записи принимает просто строки — здесь общий вид для сравнения.
    """
    return [item[key] for item in responsive_ad.get(collection) or [] if key in item]


# --- сличение --------------------------------------------------------------


def build_plan(spec: dict, state: AccountState) -> tuple[list[Change], list[str]]:
    """Возвращает список изменений и список лишнего, что есть только в аккаунте."""
    changes: list[Change] = []
    campaign = spec["campaign"]

    changes.append(_campaign_change(campaign, state.campaign))

    for group in spec["groups"]:
        name = group["name"]
        known = state.groups.get(name)
        changes.append(_group_change(campaign, group, known))

        for keyword in group.get("keywords") or []:
            exists = (name, str(keyword).strip().lower()) in state.keywords
            changes.append(Change(UNCHANGED if exists else CREATE, "keyword", str(keyword)))

        for ad in group.get("ads") or []:
            first_title = str((ad.get("titles") or [""])[0])
            changes.append(_ad_change(name, ad, state.ads.get((name, first_title))))

    return changes, _extras(spec, state)


def _campaign_change(campaign: dict, known: dict | None) -> Change:
    lines = [
        f"тип: {campaign.get('type', '—')}",
        f"недельный бюджет {campaign['weekly_budget']} ₽",
        f"стратегия: {campaign.get('strategy', '—')}",
        f"площадки: {_placements(campaign)}",
        f"регионы: {_regions(campaign)}",
        f"целей с ценностью: {len(campaign.get('goals') or [])}",
    ]
    if known is None:
        return Change(CREATE, "campaign", campaign["name"], lines)

    # Недельный бюджет, площадки, цели и атрибуцию campaigns.get в текущем наборе
    # полей не отдаёт — сверять нечего, они перечислены выше как желаемое
    # состояние и проверяются глазами в интерфейсе. Тип тоже не сличаем: API v5
    # отдаёт Единую перфоманс-кампанию как TEXT_CAMPAIGN, и сравнение с
    # UNIFIED_PERFORMANCE давало бы вечную ложную разницу.
    diffs = []
    if (actual := (known.get("Name") or "").strip()) != campaign["name"].strip():
        diffs.append(f"имя: «{actual}» -> «{campaign['name']}»")

    return Change(UPDATE if diffs else UNCHANGED, "campaign", campaign["name"], diffs)


def _negatives(group: dict) -> list[str]:
    """Минус-фразы группы одним плоским списком.

    Якорь YAML не умеет дополняться элементами — только подставляться целиком.
    Поэтому группа с дополнительными минусами записывает их вложенным списком
    (`- *negatives` плюс свои строки), а разворачивается он здесь.
    """
    flat: list[str] = []
    for item in group.get("negative_keywords") or []:
        if isinstance(item, list):
            flat.extend(str(inner) for inner in item)
        else:
            flat.append(str(item))
    return flat


def _group_change(campaign: dict, group: dict, known: dict | None) -> Change:
    name = group["name"]
    wanted_negatives = set(_negatives(group))

    if known is None:
        return Change(
            CREATE,
            "group",
            name,
            [
                f"регионы: {_regions(campaign)}",
                f"ставка до {group.get('max_cpc', '—')} ₽ за клик",
                f"минус-фраз: {len(wanted_negatives)}",
            ],
        )

    diffs = []
    current_negatives = set((known.get("NegativeKeywords") or {}).get("Items") or [])
    if added := wanted_negatives - current_negatives:
        diffs.append(f"минус-фразы: +{len(added)} ({', '.join(sorted(added)[:5])}...)")

    wanted_regions = set(campaign.get("regions") or [])
    current_regions = set(known.get("RegionIds") or [])
    if wanted_regions != current_regions:
        diffs.append(
            f"регионы: {_join(current_regions)} -> {_join(wanted_regions)}"
        )

    return Change(UPDATE if diffs else UNCHANGED, "group", name, diffs)


def _join(values) -> str:
    return ", ".join(str(v) for v in sorted(values)) or "—"


def _placements(campaign: dict) -> str:
    placements = campaign.get("placements") or {}
    enabled = [key for key, value in placements.items() if value]
    return ", ".join(enabled) if enabled else "—"


def _ad_change(group: str, ad: dict, known: dict | None) -> Change:
    titles = [str(t) for t in ad.get("titles") or [""]]
    texts = [str(t) for t in ad.get("texts") or []]

    if known is None:
        return Change(CREATE, "ad", titles[0], titles[1:] + texts + [ad.get("href", "")])

    responsive = known.get("ResponsiveAd", {})
    diffs: list[str] = []
    # Первый заголовок не сличается — по нему объявление опознаётся.
    _list_diff(diffs, "заголовки", _ad_strings(responsive, "Titles", "Title"), titles)
    _list_diff(diffs, "тексты", _ad_strings(responsive, "Texts", "Text"), texts)
    if (responsive.get("Href") or "") != (ad.get("href") or ""):
        diffs.append(f"ссылка: «{responsive.get('Href') or ''}» -> «{ad.get('href') or ''}»")

    return Change(UPDATE if diffs else UNCHANGED, "ad", titles[0], diffs)


def _list_diff(diffs: list[str], label: str, current: list[str], wanted: list[str]) -> None:
    if current == wanted:
        return
    # Частый случай — в конец дописали новые варианты: показываем только их.
    if current == wanted[: len(current)]:
        diffs.append(f"{label}: " + ", ".join(f"+«{item}»" for item in wanted[len(current):]))
    else:
        diffs.append(
            f"{label}: {'; '.join(f'«{i}»' for i in current) or '—'} -> "
            f"{'; '.join(f'«{i}»' for i in wanted)}"
        )


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
    # Сборка ниже написана под классическую текстово-графическую кампанию.
    # У Единой перфоманс-кампании другая структура — недельный бюджет, ставки на
    # группах, набор площадок, — и собрать её этим кодом нельзя. Падаем громко,
    # чтобы --apply не создал вместо неё что-то другое.
    if campaign.get("type") != "TEXT_CAMPAIGN":
        raise DirectError(
            f"campaign.type = {campaign.get('type')}: создание кампаний этого типа "
            "не реализовано, payload собирается только для TEXT_CAMPAIGN. "
            "Такую кампанию заводят вручную, а скрипт сверяет её содержимое."
        )

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
        # Из всех полей кампании через API правится только имя: бюджет,
        # стратегия, площадки и цели в ЕПК задаются в интерфейсе, и
        # campaigns.get их даже не отдаёт — сверять и слать нечего.
        if (state.campaign.get("Name") or "") != campaign["name"]:
            api.update_campaign({"Id": campaign_id, "Name": campaign["name"]})
            print(f"  кампания {campaign_id} переименована -> «{campaign['name']}»")
        else:
            print(f"  кампания {campaign_id} без изменений")

    group_ids = {name: group["Id"] for name, group in state.groups.items()}
    new_groups = [g for g in spec["groups"] if g["name"] not in group_ids]
    if new_groups:
        created = api.add_ad_groups(
            [
                {
                    "Name": g["name"],
                    "CampaignId": campaign_id,
                    "RegionIds": regions,
                    # Минус-фразы в ЕПК задаются на группе, а не на кампании.
                    "NegativeKeywords": {"Items": _negatives(g)},
                }
                for g in new_groups
            ]
        )
        group_ids.update(zip((g["name"] for g in new_groups), created))
        print(f"  создано групп: {len(created)}")

    group_edits = []
    for g in spec["groups"]:
        known = state.groups.get(g["name"])
        if known is None:
            continue
        patch: dict = {}
        wanted_negatives = set(_negatives(g))
        current_negatives = set((known.get("NegativeKeywords") or {}).get("Items") or [])
        if wanted_negatives - current_negatives:
            # Директ заменяет Items целиком, «дописать» нельзя. Отправляется
            # объединение с текущими, чтобы синхронизация не удаляла минусы,
            # заведённые руками, — удаление должно быть отдельным действием.
            patch["NegativeKeywords"] = {"Items": sorted(wanted_negatives | current_negatives)}
        if set(campaign.get("regions") or []) != set(known.get("RegionIds") or []):
            patch["RegionIds"] = regions
        if patch:
            group_edits.append({"Id": known["Id"], **patch})
    if group_edits:
        print(f"  обновлено групп: {len(api.update_ad_groups(group_edits))}")

    keywords, ads, edits = [], [], []
    for group in spec["groups"]:
        group_id = group_ids[group["name"]]
        for keyword in group.get("keywords") or []:
            if (group["name"], str(keyword).strip().lower()) not in state.keywords:
                # Bid обязателен здесь же: keywords.add без него создаёт фразу
                # со ставкой 0 ₽, и группа молча не участвует ни в одном
                # аукционе. Так четыре группы, созданные через API в августе,
                # простояли без показов до ручной сверки ставок 25-го.
                keywords.append(
                    {
                        "Keyword": str(keyword),
                        "AdGroupId": group_id,
                        "Bid": to_micros(group["max_cpc"]),
                    }
                )

        for ad in group.get("ads") or []:
            titles = [str(t) for t in ad.get("titles") or []]
            texts = [str(t) for t in ad.get("texts") or []]
            known = state.ads.get((group["name"], titles[0] if titles else ""))
            if known is None:
                ads.append(
                    {
                        "AdGroupId": group_id,
                        # На записи Titles и Texts — массивы строк, хотя get
                        # отдаёт их объектами со Status.
                        "ResponsiveAd": {
                            "Titles": titles,
                            "Texts": texts,
                            "Href": ad["href"],
                        },
                    }
                )
                continue

            # Правим только то, что действительно разошлось: любое обращение
            # к ads.update отправляет объявление на повторную модерацию.
            # Массив отправляется целиком — по одному элементу Директ его
            # не дополняет.
            responsive = known.get("ResponsiveAd", {})
            patch: dict = {}
            if _ad_strings(responsive, "Titles", "Title") != titles:
                patch["Titles"] = titles
            if _ad_strings(responsive, "Texts", "Text") != texts:
                patch["Texts"] = texts
            if (responsive.get("Href") or "") != (ad.get("href") or ""):
                patch["Href"] = ad["href"]
            if patch:
                edits.append({"Id": known["Id"], "ResponsiveAd": patch})

    if keywords:
        print(f"  создано фраз: {len(api.add_keywords(keywords))}")

    if new_groups:
        # Псевдофразу ---autotargeting Директ заводит в новой группе сам —
        # тоже со ставкой 0 ₽. Проставляем ей ставку группы, иначе
        # автотаргетинг существует, но не торгуется.
        max_cpc_by_id = {group_ids[g["name"]]: to_micros(g["max_cpc"]) for g in new_groups}
        autotargeting = [
            k
            for k in api.get_all(
                "keywords",
                {
                    "SelectionCriteria": {"AdGroupIds": list(max_cpc_by_id)},
                    "FieldNames": ["Id", "Keyword", "AdGroupId"],
                },
                "Keywords",
            )
            if k["Keyword"].strip().lower() == AUTOTARGETING_KEYWORD
        ]
        if autotargeting:
            api.set_bids(
                [
                    {"KeywordId": k["Id"], "Bid": max_cpc_by_id[k["AdGroupId"]]}
                    for k in autotargeting
                ]
            )
            print(f"  ставка автотаргетинга выставлена в {len(autotargeting)} группах")

            # Директ включает новой группе ВСЕ категории автотаргетинга —
            # в отличие от интерфейса, где выбор за человеком. Без правки
            # группа скупает широкие и сопутствующие запросы: 25 августа это
            # дало 199 показов за день при CTR 2,5%. На записи категории —
            # массив, хотя get отдаёт их обёрнутыми в Items.
            flags_by_id = {group_ids[g["name"]]: g.get("autotargeting") for g in new_groups}
            categories = [
                {
                    "Id": k["Id"],
                    "AutotargetingCategories": [
                        {"Category": category, "Value": "YES" if flags.get(flag) else "NO"}
                        for flag, category in AUTOTARGETING_CATEGORIES
                    ],
                }
                for k in autotargeting
                if (flags := flags_by_id.get(k["AdGroupId"])) is not None
            ]
            if categories:
                api.update_keywords(categories)
                print(f"  категории автотаргетинга приведены к YAML в {len(categories)} группах")
    if ads:
        created_ids = api.add_ads(ads)
        print(f"  создано объявлений: {len(created_ids)}")
        # Созданное через API объявление остаётся черновиком OFF/DRAFT и само
        # на модерацию не уходит. Без явного moderate группа молча не даёт
        # показов — эта ловушка уже стоила трёх «пустых» дней в августе.
        api.moderate_ads(created_ids)
        print("    отправлены на модерацию; после одобрения включатся сами")
    if edits:
        print(f"  обновлено объявлений: {len(api.update_ads(edits))}")
        print("    объявления ушли на повторную модерацию")

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

    # Требования к рекламе медизделий — предупреждения, а не ошибки:
    # формулировку выбирает человек, скрипт лишь называет спорные обороты.
    if warnings := review_texts(spec):
        print("ТРЕБОВАНИЯ К РЕКЛАМЕ МЕДИЦИНСКИХ ИЗДЕЛИЙ", file=sys.stderr)
        for warning in warnings:
            print(f"  {warning}", file=sys.stderr)
        print(
            f"\nПредупреждений: {len(warnings)}. Это не ошибки, работа продолжается.\n"
            "Требования: https://yandex.ru/support/direct/ru/moderation/categories"
            "/medicine-medical-devices\n",
            file=sys.stderr,
        )

    try:
        api = DirectApi()
        state = read_state(api, spec["campaign"])
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
            "\nЧасть изменений могла примениться до ошибки: Директ обрабатывает "
            "объекты поштучно. Прогони предпросмотр ещё раз и посмотри, что "
            "осталось, прежде чем повторять --apply.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
