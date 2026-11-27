"""Construction et envoi du message Google Chat (carte cardsV2).

Chaque sujet affiche obligatoirement : la source, sa date de publication et un
lien cliquable vers l'article d'origine. La source officielle (l'éditeur de
l'outil) est affichée en premier, les autres ensuite.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import requests

from analyzer import DOMAINS, Topic
from collector import USER_AGENT, retry_call

log = logging.getLogger(__name__)

#: Emoji par domaine, dans l'ordre d'affichage défini par analyzer.DOMAINS.
DOMAIN_EMOJI: dict[str, str] = {
    "DevOps": "☸️",
    "Cloud": "☁️",
    "DevSecOps": "🔐",
    "LLMOps": "🤖",
}

_WEEKDAYS = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)

CARD_TITLE = "📡 Utrains Pulse"


@dataclass(slots=True)
class RunStats:
    """Chiffres affichés en bas de message."""

    articles_analyzed: int
    sources_ok: int
    failed_sources: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Formatage
# ---------------------------------------------------------------------------


def get_timezone(name: str) -> timezone | ZoneInfo:
    """Fuseau demandé, avec repli sur UTC si la base tz est absente."""
    try:
        return ZoneInfo(name)
    except Exception as exc:  # tzdata manquant, nom invalide...
        log.warning("Fuseau « %s » indisponible (%s) — repli sur UTC", name, exc)
        return timezone.utc


def french_long_date(moment: datetime) -> str:
    """« jeudi 27 novembre 2026 »."""
    return (
        f"{_WEEKDAYS[moment.weekday()]} {moment.day} "
        f"{_MONTHS[moment.month - 1]} {moment.year}"
    )


def short_date(moment: datetime, tz: Any) -> str:
    """« 26/11/2026 » dans le fuseau configuré."""
    return moment.astimezone(tz).strftime("%d/%m/%Y")


def _esc(text: str) -> str:
    """Échappe le texte avant insertion dans un textParagraph (qui accepte du HTML)."""
    return html.escape(text, quote=False)


def _topic_widget(topic: Topic, tz: Any) -> dict[str, Any]:
    """Un sujet = un paragraphe : catégorie + titre, résumé, puis ses sources."""
    lines = [
        f"<b>{topic.categorie_emoji} {_esc(topic.titre)}</b>",
        _esc(topic.resume),
    ]
    if topic.remplacement:
        lines.append(f"<b>Remplacement :</b> {_esc(topic.remplacement)}")

    for article in topic.articles:
        lines.append(
            f'🔗 Source : {_esc(article.source)} — {short_date(article.published, tz)} '
            f'<a href="{html.escape(article.url, quote=True)}">Lire l\'article</a>'
        )
    return {"textParagraph": {"text": "<br>".join(lines)}}


def _footer_text(stats: RunStats) -> str:
    """Pied de message : volumétrie et sources en panne."""
    lines = [
        f"<i>{stats.articles_analyzed} articles analysés depuis "
        f"{stats.sources_ok} sources</i>"
    ]
    if stats.failed_sources:
        sources = ", ".join(_esc(name) for name in stats.failed_sources)
        lines.append(f"<i>⚠️ Sources indisponibles : {sources}</i>")
    return "<br>".join(lines)


def _card(subtitle: str, sections: list[dict[str, Any]], index: int) -> dict[str, Any]:
    return {
        "cardsV2": [
            {
                "cardId": f"utrains-pulse-{index}",
                "card": {
                    "header": {"title": CARD_TITLE, "subtitle": subtitle},
                    "sections": sections,
                },
            }
        ]
    }


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def build_payloads(
    topics: Sequence[Topic],
    stats: RunStats,
    config: dict[str, Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Construit un ou plusieurs payloads Google Chat (découpage si trop gros)."""
    general = config["general"]
    tz = get_timezone(str(general.get("timezone", "UTC")))
    moment = (now or datetime.now(timezone.utc)).astimezone(tz)
    subtitle = f"Veille du {french_long_date(moment)}"
    max_bytes = int(config["chat"].get("max_payload_bytes", 28000))

    footer_section = {"widgets": [{"textParagraph": {"text": _footer_text(stats)}}]}

    if not topics:
        sections = [
            {"widgets": [{"textParagraph": {"text": "Rien de majeur aujourd'hui ✅"}}]},
            footer_section,
        ]
        return [_card(subtitle, sections, 1)]

    # Sujets regroupés par domaine, dans l'ordre d'affichage.
    grouped: list[tuple[str, list[Topic]]] = []
    for domain in DOMAINS:
        in_domain = [topic for topic in topics if topic.domaine == domain]
        if in_domain:
            grouped.append((domain, in_domain))

    # Découpage : on ajoute les sujets un par un et on ouvre un nouveau message
    # dès que la limite de taille de Google Chat serait dépassée.
    payloads: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal sections, current
        if sections:
            index = len(payloads) + 1
            suffix = "" if index == 1 else f" (suite {index})"
            payloads.append(_card(subtitle + suffix, sections, index))
        sections = []
        current = None

    def widget_count() -> int:
        return sum(len(section["widgets"]) for section in sections)

    for domain, domain_topics in grouped:
        header = f"{DOMAIN_EMOJI.get(domain, '•')} {domain}"
        current = {"header": header, "widgets": []}
        sections.append(current)
        for topic in domain_topics:
            widget = _topic_widget(topic, tz)
            current["widgets"].append(widget)
            candidate = _card(subtitle, sections, len(payloads) + 1)
            if _payload_size(candidate) > max_bytes and widget_count() > 1:
                # Le sujet ne rentre pas : on le reporte au message suivant.
                current["widgets"].pop()
                if not current["widgets"]:
                    sections.pop()
                flush()
                current = {"header": header, "widgets": [widget]}
                sections.append(current)

    flush()

    # Pied de message : dans le dernier message s'il y a la place, sinon seul.
    last = payloads[-1]
    last["cardsV2"][0]["card"]["sections"].append(footer_section)
    if _payload_size(last) > max_bytes:
        last["cardsV2"][0]["card"]["sections"].pop()
        payloads.append(_card(subtitle + " (suite)", [footer_section], len(payloads) + 1))

    log.info("Message construit : %d partie(s)", len(payloads))
    return payloads


# ---------------------------------------------------------------------------
# Rendu terminal (--dry-run)
# ---------------------------------------------------------------------------


def payloads_to_text(payloads: Sequence[dict[str, Any]]) -> str:
    """Rendu lisible des payloads, pour vérifier sans rien envoyer."""
    out: list[str] = []
    for position, payload in enumerate(payloads, start=1):
        if "text" in payload:
            out.append(payload["text"])
            continue
        card = payload["cardsV2"][0]["card"]
        header = card.get("header", {})
        out.append(
            f"===== message {position}/{len(payloads)} "
            f"({_payload_size(payload)} octets) =====\n"
            f"{header.get('title', '')} — {header.get('subtitle', '')}"
        )
        for section in card.get("sections", []):
            if section.get("header"):
                out.append(f"\n{section['header']}")
            for widget in section.get("widgets", []):
                text = widget.get("textParagraph", {}).get("text", "")
                text = text.replace("<br>", "\n")
                # <a href="URL">Libellé</a>  ->  Libellé (URL)
                text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", text)
                text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
                out.append(html.unescape(text))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------


class PermanentChatError(RuntimeError):
    """Erreur non récupérable côté Google Chat (payload ou URL en cause)."""


def _post(webhook_url: str, payload: dict[str, Any], timeout: int, attempts: int) -> None:
    """POST du payload avec retry/backoff. Lève si l'envoi échoue définitivement."""

    def _do() -> None:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        # 4xx hors 429 : inutile de réessayer, le payload ou l'URL est en cause.
        if response.status_code >= 400 and not (
            response.status_code == 429 or response.status_code >= 500
        ):
            raise PermanentChatError(
                f"Google Chat a refusé le message ({response.status_code}) : "
                f"{response.text[:300]}"
            )
        response.raise_for_status()

    retry_call(
        _do,
        attempts=attempts,
        base_delay=2.0,
        max_delay=30.0,
        exceptions=(requests.RequestException,),
        label="webhook Google Chat",
    )


def send(
    webhook_url: str,
    payloads: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    """Envoie les messages dans l'ordre, avec une courte pause entre chacun."""
    timeout = int(config["general"].get("http_timeout", 20))
    attempts = int(config["chat"].get("max_attempts", 4))
    for position, payload in enumerate(payloads, start=1):
        _post(webhook_url, payload, timeout, attempts)
        log.info("Message %d/%d envoyé", position, len(payloads))
        if position < len(payloads):
            time.sleep(1.0)


def send_error(webhook_url: str, message: str, timeout: int = 15) -> None:
    """Envoie un message d'erreur court (texte simple, un seul essai + 1 retry)."""
    payload = {"text": f"🔴 *Utrains Pulse* — échec de la veille\n{message}"[:3500]}
    try:
        retry_call(
            lambda: requests.post(
                webhook_url,
                json=payload,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            ).raise_for_status(),
            attempts=2,
            base_delay=2.0,
            exceptions=(requests.RequestException,),
            label="webhook (message d'erreur)",
        )
    except Exception as exc:  # on ne masque jamais l'erreur d'origine
        log.error("Impossible d'envoyer le message d'erreur : %s", exc)
