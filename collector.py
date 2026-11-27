"""Collecte des actualités depuis les flux RSS/Atom déclarés dans config.yaml.

Le collecteur est la seule source de vérité pour les URL, les titres d'origine
et les dates de publication : Gemini ne manipule ensuite que des identifiants.
"""

from __future__ import annotations

import html
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml

log = logging.getLogger(__name__)

T = TypeVar("T")

#: User-Agent générique (aucune donnée personnelle).
USER_AGENT = "UtrainsPulse/1.0 (veille DevOps; +https://github.com/topics/devops)"

#: Paramètres de tracking retirés des URL avant comparaison / affichage.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "mkt_tok",
    "gclid",
    "fbclid",
}

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Modèle de données
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Article:
    """Un article collecté. `id` est la seule chose manipulée par Gemini."""

    id: str
    source: str
    domain: str
    title: str
    url: str
    published: datetime  # toujours timezone-aware, en UTC
    content: str
    official_for: tuple[str, ...] = ()

    def is_official_for(self, text: str) -> bool:
        """True si cette source est l'éditeur officiel de l'outil cité dans `text`."""
        haystack = f"{text} {self.title}".lower()
        return any(tool and tool.lower() in haystack for tool in self.official_for)


@dataclass(slots=True)
class CollectResult:
    """Résultat d'une passe de collecte."""

    articles: list[Article] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)
    sources_total: int = 0
    skipped_duplicates: int = 0
    skipped_out_of_scope: int = 0
    skipped_bad_url: int = 0

    @property
    def sources_ok(self) -> int:
        return self.sources_total - len(self.failed_sources)


# ---------------------------------------------------------------------------
# Utilitaires partagés
# ---------------------------------------------------------------------------


def retry_call(
    func: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    fatal: tuple[type[BaseException], ...] = (),
    label: str = "appel",
) -> T:
    """Exécute `func` avec un retry exponentiel et un jitter.

    Utilisé pour les flux, l'API Gemini et le webhook Discord.
    Les exceptions listées dans `fatal` remontent immédiatement : inutile de
    réessayer une requête que le serveur refusera à l'identique.
    """
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except fatal:  # erreur définitive : on ne perd pas de temps en retries
            raise
        except exceptions as exc:  # noqa: PERF203 - on veut le retry par tentative
            last_error = exc
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= 0.8 + 0.4 * random.random()  # jitter
            log.warning(
                "%s : échec (tentative %d/%d) : %s — nouvel essai dans %.1fs",
                label,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def load_config(path: str | Path) -> dict[str, Any]:
    """Charge config.yaml et vérifie le minimum vital."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if not isinstance(config, dict):
        raise ValueError(f"{config_path} : le YAML racine doit être un dictionnaire")
    if not config.get("sources"):
        raise ValueError(f"{config_path} : aucune source déclarée")

    config.setdefault("general", {})
    config.setdefault("gemini", {})
    config.setdefault("chat", {})
    config.setdefault("keywords", {}).setdefault("include", [])
    return config


def normalize_url(url: str) -> str:
    """Normalise une URL pour l'anti-doublon (sans paramètres de tracking)."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def clean_text(raw: str, max_chars: int) -> str:
    """Retire le HTML, normalise les espaces et tronque proprement."""
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if max_chars > 0 and len(text) > max_chars:
        cut = text[:max_chars]
        # On coupe sur le dernier espace pour éviter de hacher un mot.
        if " " in cut[-80:]:
            cut = cut[: cut.rindex(" ")]
        text = cut.rstrip() + "…"
    return text


def build_keyword_matcher(keywords: Sequence[str]) -> Callable[[str], bool]:
    """Construit un test « ce texte parle-t-il du périmètre ? ».

    Les mots-clés sont comparés sur des frontières de mots pour éviter les
    faux positifs du type « git » dans « digital ».
    """
    patterns = [
        re.compile(rf"(?<!\w){re.escape(kw.strip().lower())}(?!\w)")
        for kw in keywords
        if kw and kw.strip()
    ]

    def matches(text: str) -> bool:
        lowered = text.lower()
        return any(pattern.search(lowered) for pattern in patterns)

    return matches


# ---------------------------------------------------------------------------
# Collecte
# ---------------------------------------------------------------------------


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    return session


def _entry_datetime(entry: Any) -> datetime | None:
    """Date de publication d'une entrée, en UTC. None si absente/illisible."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_content(entry: Any) -> str:
    """Contenu brut le plus riche disponible pour une entrée."""
    contents = entry.get("content") or []
    if contents and isinstance(contents, list):
        value = contents[0].get("value") if isinstance(contents[0], dict) else None
        if value:
            return str(value)
    for key in ("summary", "description", "subtitle"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _fetch_feed(session: requests.Session, url: str, timeout: int, attempts: int) -> list[Any]:
    """Télécharge et parse un flux. Lève une exception si le flux est inutilisable."""

    def _do() -> list[Any]:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        entries = list(parsed.entries or [])
        if not entries and parsed.get("bozo"):
            raise ValueError(f"flux illisible : {parsed.get('bozo_exception')}")
        return entries

    return retry_call(
        _do,
        attempts=attempts,
        base_delay=1.5,
        max_delay=10.0,
        exceptions=(requests.RequestException, ValueError),
        label=f"flux {url}",
    )


def _url_is_reachable(session: requests.Session, url: str, timeout: int) -> bool:
    """Vérifie qu'une URL répond 200 (HEAD, puis GET si HEAD est refusé)."""
    try:
        response = session.head(url, timeout=timeout, allow_redirects=True)
        if response.status_code == 200:
            return True
        # Beaucoup de sites n'implémentent pas HEAD correctement.
        if response.status_code in (403, 405, 404, 501, 400):
            response = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
            response.close()
            return response.status_code == 200
        return False
    except requests.RequestException as exc:
        log.debug("URL injoignable (%s) : %s", url, exc)
        return False


def _enabled_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        source
        for source in config["sources"]
        if source.get("enabled", True) and source.get("url") and source.get("name")
    ]


def collect(
    config: dict[str, Any],
    since: datetime,
    now: datetime,
    already_sent: Iterable[str] = (),
) -> CollectResult:
    """Collecte les articles publiés entre `since` et `now`.

    Les articles déjà envoyés (`already_sent`, URL normalisées), hors périmètre
    ou dont l'URL ne répond pas 200 sont écartés.
    """
    general = config["general"]
    timeout = int(general.get("http_timeout", 20))
    max_chars = int(general.get("max_article_chars", 2000))
    max_per_source = int(general.get("max_articles_per_source", 25))
    default_filter = bool(general.get("filter_keywords_by_default", True))
    sent = set(already_sent)
    in_scope = build_keyword_matcher(config["keywords"]["include"])

    sources = _enabled_sources(config)
    result = CollectResult(sources_total=len(sources))
    session = _new_session()

    # 1) Téléchargement des flux en parallèle : une source en panne n'empêche
    #    pas les autres de remonter.
    def _load(source: dict[str, Any]) -> tuple[dict[str, Any], list[Any] | None]:
        try:
            entries = _fetch_feed(session, source["url"], timeout, attempts=3)
            return source, entries
        except Exception as exc:  # une source en panne est juste signalée
            log.error("Source indisponible : %s (%s)", source["name"], exc)
            return source, None

    workers = max(1, int(general.get("feed_workers", 8)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        loaded = list(pool.map(_load, sources))

    # 2) Filtrage : fenêtre temporelle, périmètre, doublons.
    candidates: list[Article] = []
    seen_urls: set[str] = set()

    for source, entries in loaded:
        if entries is None:
            result.failed_sources.append(source["name"])
            continue

        kept_for_source = 0
        filter_keywords = bool(source.get("filter_keywords", default_filter))
        official_for = tuple(str(tool) for tool in source.get("official_for") or ())

        for entry in entries:
            if kept_for_source >= max_per_source:
                break

            link = (entry.get("link") or "").strip()
            title = clean_text(entry.get("title") or "", 300)
            if not link.startswith(("http://", "https://")) or not title:
                continue

            published = _entry_datetime(entry)
            if published is None:
                log.debug("Article sans date ignoré : %s", link)
                continue
            if not (since <= published <= now):
                continue

            key = normalize_url(link)
            if key in sent:
                result.skipped_duplicates += 1
                continue
            if key in seen_urls:
                result.skipped_duplicates += 1
                continue

            content = clean_text(_entry_content(entry), max_chars)
            if filter_keywords and not in_scope(f"{title} {content}"):
                result.skipped_out_of_scope += 1
                continue

            seen_urls.add(key)
            kept_for_source += 1
            candidates.append(
                Article(
                    id="",  # attribué après tri, pour rester stable
                    source=source["name"],
                    domain=str(source.get("domain", "DevOps")),
                    title=title,
                    url=link,
                    published=published,
                    content=content or title,
                    official_for=official_for,
                )
            )

    # 3) Les plus récents d'abord, puis plafond global.
    candidates.sort(key=lambda art: art.published, reverse=True)
    max_articles = int(general.get("max_articles_to_analyze", 120))
    if max_articles > 0 and len(candidates) > max_articles:
        log.info(
            "Plafond atteint : %d articles collectés, %d transmis à l'analyse",
            len(candidates),
            max_articles,
        )
        candidates = candidates[:max_articles]

    # 4) Vérification des URL (http 200) en parallèle.
    if general.get("verify_urls", True) and candidates:
        check_workers = max(1, int(general.get("url_check_workers", 12)))
        with ThreadPoolExecutor(max_workers=check_workers) as pool:
            reachable = list(
                pool.map(lambda art: _url_is_reachable(session, art.url, timeout), candidates)
            )
        checked: list[Article] = []
        for article, ok in zip(candidates, reachable):
            if ok:
                checked.append(article)
            else:
                result.skipped_bad_url += 1
                log.warning("URL écartée (pas de réponse 200) : %s", article.url)
        candidates = checked

    # 5) Identifiants définitifs (a1, a2, ...) utilisés par Gemini.
    for index, article in enumerate(candidates, start=1):
        article.id = f"a{index}"

    result.articles = candidates
    session.close()

    log.info(
        "Collecte : %d articles retenus — %d sources OK / %d — "
        "écartés : %d doublons, %d hors périmètre, %d URL invalides",
        len(result.articles),
        result.sources_ok,
        result.sources_total,
        result.skipped_duplicates,
        result.skipped_out_of_scope,
        result.skipped_bad_url,
    )
    return result
