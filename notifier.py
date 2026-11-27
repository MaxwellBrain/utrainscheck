"""Construction et envoi du message Discord (webhook entrant).

Chaque sujet affiche obligatoirement : la source, sa date de publication et un
lien cliquable vers l'article d'origine. La source officielle (l'éditeur de
l'outil) est affichée en premier, les autres ensuite.

Le message est une liste d'« embeds » : un embed par domaine, un champ par
sujet. Les limites Discord (10 embeds, 25 champs, 6000 caractères par message)
sont appliquées ici, avec découpage automatique en plusieurs messages.
"""

from __future__ import annotations

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

#: Emoji et couleur par domaine (barre verticale de l'embed Discord).
DOMAIN_EMOJI: dict[str, str] = {
    "DevOps": "☸️",
    "Cloud": "☁️",
    "DevSecOps": "🔐",
    "LLMOps": "🤖",
}
DOMAIN_COLOR: dict[str, int] = {
    "DevOps": 0x326CE5,  # bleu Kubernetes
    "Cloud": 0xFF9900,  # orange AWS
    "DevSecOps": 0xE53935,  # rouge
    "LLMOps": 0x7C4DFF,  # violet
}

# Limites imposées par l'API Discord.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_FIELDS_PER_EMBED = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FOOTER = 2048
MAX_MESSAGE_CHARS = 6000
MAX_CONTENT = 2000

_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
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

BOT_NAME = "Utrains Pulse"

#: Caractères qui déclencheraient du formatage Markdown involontaire.
_MD_SPECIALS = re.compile(r"([*_~`|\\>])")


@dataclass(slots=True)
class RunStats:
    """Chiffres affichés en pied de message."""

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
    """Neutralise le Markdown Discord dans un texte venant d'une source externe."""
    return _MD_SPECIALS.sub(r"\\\1", text)


def _safe_link(url: str) -> str:
    """Encode les parenthèses pour ne pas casser la syntaxe [texte](url)."""
    return url.replace("(", "%28").replace(")", "%29")


def _trim(text: str, limit: int) -> str:
    """Tronque proprement à la limite Discord."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _topic_field(topic: Topic, tz: Any) -> dict[str, str]:
    """Un sujet = un champ d'embed : titre, résumé, puis ses sources."""
    name = _trim(f"{topic.categorie_emoji} {_esc(topic.titre)}", MAX_FIELD_NAME)

    lines = [_esc(topic.resume)]
    if topic.remplacement:
        lines.append(f"**Remplacement :** {_esc(topic.remplacement)}")
    for article in topic.articles:
        lines.append(
            f"🔗 {_esc(article.source)} — {short_date(article.published, tz)} · "
            f"[Lire l'article]({_safe_link(article.url)})"
        )
    return {"name": name, "value": _trim("\n".join(lines), MAX_FIELD_VALUE)}


def _footer_text(stats: RunStats) -> str:
    """Pied de message : volumétrie et sources en panne."""
    text = f"{stats.articles_analyzed} articles analysés depuis {stats.sources_ok} sources"
    if stats.failed_sources:
        text += "\n⚠️ Sources indisponibles : " + ", ".join(stats.failed_sources)
    return _trim(text, MAX_EMBED_FOOTER)


def _embed_chars(embed: dict[str, Any]) -> int:
    """Nombre de caractères comptés par Discord dans le quota de 6000."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        total += len(field["name"]) + len(field["value"])
    return total


def _new_embed(domain: str, suffix: str = "") -> dict[str, Any]:
    return {
        "title": f"{DOMAIN_EMOJI.get(domain, '•')} {domain}{suffix}",
        "color": DOMAIN_COLOR.get(domain, 0x5865F2),
        "fields": [],
    }


def _message(content: str, embeds: list[dict[str, Any]], username: str) -> dict[str, Any]:
    return {
        "username": username,
        "content": _trim(content, MAX_CONTENT),
        "embeds": embeds,
        # Aucune mention automatique : un titre contenant @here ne ping personne.
        "allowed_mentions": {"parse": []},
    }


def _topic_message(
    topic: Topic,
    tz: Any,
    username: str,
    image_style: str,
) -> dict[str, Any]:
    """Un sujet = un message à part entière, façon article illustré."""
    main = topic.articles[0]

    lines = [_esc(topic.resume)]
    if topic.remplacement:
        lines.append(f"**Remplacement :** {_esc(topic.remplacement)}")
    lines.append("")
    for article in topic.articles:
        lines.append(
            f"🔗 **{_esc(article.source)}** — {short_date(article.published, tz)} · "
            f"[Lire l'article]({_safe_link(article.url)})"
        )

    embed: dict[str, Any] = {
        "author": {"name": f"{DOMAIN_EMOJI.get(topic.domaine, '•')} {topic.domaine}"},
        "title": _trim(f"{topic.categorie_emoji} {_esc(topic.titre)}", MAX_EMBED_TITLE),
        "url": _safe_link(main.url),  # le titre devient cliquable
        "description": _trim("\n".join(lines), MAX_EMBED_DESCRIPTION),
        "color": DOMAIN_COLOR.get(topic.domaine, 0x5865F2),
        "footer": {"text": f"{main.source} · importance {topic.importance}/5"},
        "timestamp": main.published.astimezone(timezone.utc).isoformat(),
    }

    # Illustration : la première disponible parmi les articles du sujet.
    image = next((article.image for article in topic.articles if article.image), None)
    if image and image_style in ("large", "thumbnail"):
        key = "image" if image_style == "large" else "thumbnail"
        embed[key] = {"url": image}

    return _message("", [embed], username)


def build_payloads(
    topics: Sequence[Topic],
    stats: RunStats,
    config: dict[str, Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Construit les messages Discord du jour.

    Par défaut, un message par sujet (façon fil d'actualité illustré). Avec
    `discord.message_per_topic: false`, on retombe sur un message unique
    regroupé par domaine.
    """
    general = config["general"]
    discord_config = config.get("discord", {})
    username = str(discord_config.get("username", BOT_NAME))
    max_fields = min(int(discord_config.get("max_fields_per_embed", 15)), MAX_FIELDS_PER_EMBED)
    max_chars = min(int(discord_config.get("max_message_chars", 5500)), MAX_MESSAGE_CHARS)

    tz = get_timezone(str(general.get("timezone", "UTC")))
    moment = (now or datetime.now(timezone.utc)).astimezone(tz)
    header = f"📡 **{username}** — Veille du {french_long_date(moment)}"
    footer = {"text": _footer_text(stats)}
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    if not topics:
        embed = {
            "title": "Rien de majeur aujourd'hui ✅",
            "color": 0x9E9E9E,
            "footer": footer,
            "timestamp": timestamp,
        }
        return [_message(header, [embed], username)]

    if discord_config.get("message_per_topic", True):
        # Les sujets sont regroupés par domaine pour garder un ordre de lecture.
        ordered = sorted(
            topics,
            key=lambda topic: (
                DOMAINS.index(topic.domaine) if topic.domaine in DOMAINS else len(DOMAINS),
                -topic.importance,
            ),
        )
        image_style = str(discord_config.get("image_style", "large"))
        payloads = []
        if discord_config.get("send_header", True):
            sujets = f"{len(ordered)} sujet" + ("s" if len(ordered) > 1 else "")
            content = (
                f"{header}\n*{sujets} · {stats.articles_analyzed} articles analysés "
                f"depuis {stats.sources_ok} sources*"
            )
            if stats.failed_sources:
                content += "\n⚠️ Sources indisponibles : " + ", ".join(stats.failed_sources)
            payloads.append(_message(content, [], username))
        payloads += [_topic_message(topic, tz, username, image_style) for topic in ordered]
        if not discord_config.get("send_header", True):
            payloads[-1]["embeds"][-1]["footer"] = footer
        log.info("Message construit : %d message(s), un par sujet", len(payloads))
        return payloads

    # Mode groupé : un embed par domaine, découpé si trop de sujets ou trop long.
    embeds: list[dict[str, Any]] = []
    for domain in DOMAINS:
        domain_topics = [topic for topic in topics if topic.domaine == domain]
        if not domain_topics:
            continue
        current = _new_embed(domain)
        for topic in domain_topics:
            field = _topic_field(topic, tz)
            too_many = len(current["fields"]) >= max_fields
            too_long = (
                _embed_chars(current) + len(field["name"]) + len(field["value"]) > max_chars
            )
            if current["fields"] and (too_many or too_long):
                embeds.append(current)
                current = _new_embed(domain, suffix=" (suite)")
            current["fields"].append(field)
        embeds.append(current)

    # 2) Regroupement des embeds en messages (10 embeds et 6000 caractères max).
    payloads: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    batch_chars = 0
    for embed in embeds:
        size = _embed_chars(embed)
        if batch and (len(batch) >= MAX_EMBEDS_PER_MESSAGE or batch_chars + size > max_chars):
            suffix = "" if not payloads else f" (suite {len(payloads) + 1})"
            payloads.append(_message(header + suffix, batch, username))
            batch, batch_chars = [], 0
        batch.append(embed)
        batch_chars += size
    suffix = "" if not payloads else f" (suite {len(payloads) + 1})"
    payloads.append(_message(header + suffix, batch, username))

    # 3) Pied de message sur le dernier embed, ou seul s'il ne rentre pas.
    last = payloads[-1]
    total = sum(_embed_chars(embed) for embed in last["embeds"])
    if total + len(footer["text"]) <= max_chars:
        last["embeds"][-1]["footer"] = footer
        last["embeds"][-1]["timestamp"] = timestamp
    else:
        payloads.append(
            _message(
                header + f" (suite {len(payloads) + 1})",
                [{"color": 0x9E9E9E, "footer": footer, "timestamp": timestamp}],
                username,
            )
        )

    log.info("Message construit : %d partie(s), %d embed(s)", len(payloads), len(embeds))
    return payloads


# ---------------------------------------------------------------------------
# Rendu terminal (--dry-run)
# ---------------------------------------------------------------------------


def _unescape_md(text: str) -> str:
    """Retire les échappements Markdown pour l'affichage terminal."""
    return re.sub(r"\\([*_~`|\\>])", r"\1", text)


def payloads_to_text(payloads: Sequence[dict[str, Any]]) -> str:
    """Rendu lisible des messages, pour vérifier sans rien envoyer."""
    out: list[str] = []
    for position, payload in enumerate(payloads, start=1):
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        chars = sum(_embed_chars(embed) for embed in payload.get("embeds", []))
        out.append(
            f"===== message {position}/{len(payloads)} "
            f"({chars} caractères comptés par Discord, {size} octets) ====="
        )
        if payload["content"]:
            out.append(_unescape_md(payload["content"].replace("**", "")))
        for embed in payload.get("embeds", []):
            if embed.get("author"):
                out.append(f"\n{embed['author']['name']}")
            if embed.get("title"):
                out.append(_unescape_md(embed["title"]))
            if embed.get("description"):
                description = embed["description"].replace("**", "")
                description = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", description)
                out.append(_unescape_md(description))
            for key in ("image", "thumbnail"):
                if embed.get(key):
                    out.append(f"🖼️  {key} : {embed[key]['url']}")
            for field in embed.get("fields", []):
                out.append(_unescape_md(field["name"]))
                value = field["value"].replace("**", "")
                value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", value)
                out.append(_unescape_md(value))
            if embed.get("footer"):
                out.append(f"\n{embed['footer']['text']}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------


class PermanentWebhookError(RuntimeError):
    """Erreur non récupérable côté Discord (payload ou URL en cause)."""


def _post(webhook_url: str, payload: dict[str, Any], timeout: int, attempts: int) -> None:
    """POST du message avec retry/backoff et respect du rate limit Discord."""

    def _do() -> None:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code == 429:
            # Discord indique le délai à respecter (en secondes).
            try:
                wait = float(response.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2.0
            log.warning("Discord : rate limit, pause de %.1fs", wait)
            time.sleep(min(wait, 30.0))
            raise requests.HTTPError("429 rate limit", response=response)
        if 400 <= response.status_code < 500:
            raise PermanentWebhookError(
                f"Discord a refusé le message ({response.status_code}) : "
                f"{response.text[:300]}"
            )
        response.raise_for_status()

    retry_call(
        _do,
        attempts=attempts,
        base_delay=2.0,
        max_delay=30.0,
        exceptions=(requests.RequestException,),
        label="webhook Discord",
    )


def send(
    webhook_url: str,
    payloads: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    """Envoie les messages dans l'ordre, avec une courte pause entre chacun."""
    timeout = int(config["general"].get("http_timeout", 20))
    attempts = int(config.get("discord", {}).get("max_attempts", 4))
    for position, payload in enumerate(payloads, start=1):
        _post(webhook_url, payload, timeout, attempts)
        log.info("Message %d/%d envoyé", position, len(payloads))
        if position < len(payloads):
            time.sleep(1.0)


def send_error(webhook_url: str, message: str, timeout: int = 15) -> None:
    """Envoie un message d'erreur court (deux essais maximum)."""
    payload = {
        "username": BOT_NAME,
        "content": _trim(f"🔴 **{BOT_NAME}** — échec de la veille\n```{message}```", MAX_CONTENT),
        "allowed_mentions": {"parse": []},
    }
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
