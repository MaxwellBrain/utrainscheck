"""Utrains Pulse — point d'entrée.

Enchaînement : collecte des flux → analyse Gemini → sélection → message
Discord. Un seul script, pas de base de données : l'état tient dans
sent.json (anti-doublon + date de dernière exécution).

Usage :
    python main.py                 # collecte, analyse et envoie
    python main.py --dry-run       # affiche le message, n'envoie rien
    python main.py --since-hours 48 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import notifier
from analyzer import analyze, resolve_model, select_topics
from collector import collect, load_config, normalize_url

log = logging.getLogger("utrains_pulse")


# ---------------------------------------------------------------------------
# Environnement et journalisation
# ---------------------------------------------------------------------------


def load_dotenv(path: Path = Path(".env")) -> None:
    """Charge un .env local (pratique en dev ; en CI, ce sont les secrets)."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def setup_logging(verbose: bool) -> None:
    """Logs lisibles dans la console GitHub Actions."""
    try:  # emojis et accents sur un terminal Windows
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# État (sent.json)
# ---------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    """Lit sent.json. Un fichier absent ou corrompu repart d'un état vide."""
    if not path.is_file():
        return {"last_run": None, "urls": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("structure inattendue")
        data.setdefault("last_run", None)
        urls = data.get("urls")
        data["urls"] = urls if isinstance(urls, dict) else {}
        return data
    except (OSError, ValueError) as exc:
        log.warning("%s illisible (%s) — on repart d'un état vide", path, exc)
        return {"last_run": None, "urls": {}}


def save_state(
    path: Path,
    state: dict[str, Any],
    new_urls: list[str],
    now: datetime,
    retention_days: int,
) -> None:
    """Ajoute les URL envoyées, purge au-delà de la rétention, écrit le fichier."""
    urls: dict[str, str] = dict(state.get("urls") or {})
    today = now.date().isoformat()
    for url in new_urls:
        urls[url] = today

    limit = (now - timedelta(days=retention_days)).date().isoformat()
    kept = {url: seen for url, seen in urls.items() if seen >= limit}
    removed = len(urls) - len(kept)

    payload = {"last_run": now.isoformat(), "urls": dict(sorted(kept.items()))}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info(
        "État enregistré : %d URL mémorisées (+%d, -%d purgées)",
        len(kept),
        len(new_urls),
        removed,
    )


def compute_since(state: dict[str, Any], config: dict[str, Any], now: datetime) -> datetime:
    """Fenêtre de collecte : depuis la dernière exécution, bornée par la config."""
    general = config["general"]
    default_window = now - timedelta(hours=int(general.get("lookback_hours", 24)))
    hard_limit = now - timedelta(hours=int(general.get("max_lookback_hours", 72)))

    last_run = state.get("last_run")
    if not last_run:
        return default_window
    try:
        parsed = datetime.fromisoformat(str(last_run))
    except ValueError:
        log.warning("last_run illisible (%s) — fenêtre par défaut", last_run)
        return default_window
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # Si le job n'a pas tourné depuis plusieurs jours, on rattrape le retard,
    # mais jamais au-delà de max_lookback_hours.
    return max(parsed, hard_limit)


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    general = config["general"]
    state_path = Path(args.state or general.get("state_file", "sent.json"))

    now = datetime.now(timezone.utc)
    state = load_state(state_path)
    if args.since_hours:
        since = now - timedelta(hours=args.since_hours)
    else:
        since = compute_since(state, config, now)
    log.info(
        "Fenêtre de collecte : %s → %s (UTC)",
        since.strftime("%Y-%m-%d %H:%M"),
        now.strftime("%Y-%m-%d %H:%M"),
    )

    # 1) Collecte
    collected = collect(config, since=since, now=now, already_sent=state["urls"].keys())

    # 2) Analyse Gemini (seulement s'il y a de la matière)
    if collected.articles:
        log.info("Modèle Gemini : %s", resolve_model(config))
        topics = analyze(collected.articles, config)
    else:
        log.info("Aucun article nouveau : pas d'appel à Gemini")
        topics = []

    # 3) Sélection finale
    selected = select_topics(topics, config)
    for topic in selected:
        log.info(
            "  [%d] %-9s %s — %s",
            topic.importance,
            topic.domaine,
            topic.categorie,
            topic.titre,
        )

    # 4) Message
    stats = notifier.RunStats(
        articles_analyzed=len(collected.articles),
        sources_ok=collected.sources_ok,
        failed_sources=tuple(collected.failed_sources),
    )
    payloads = notifier.build_payloads(selected, stats, config, now=now)

    if args.dry_run:
        print()
        print(notifier.payloads_to_text(payloads))
        log.info("--dry-run : rien n'a été envoyé, sent.json inchangé")
        return 0

    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    notifier.send(webhook, payloads, config)

    # 5) Anti-doublon : on mémorise tout ce qui a été analysé.
    save_state(
        state_path,
        state,
        [normalize_url(article.url) for article in collected.articles],
        now,
        int(general.get("state_retention_days", 30)),
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="utrains-pulse",
        description="Veille quotidienne DevOps / LLMOps vers Discord.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="affiche le message dans le terminal sans l'envoyer",
    )
    parser.add_argument("--config", default="config.yaml", help="chemin de config.yaml")
    parser.add_argument("--state", default=None, help="chemin du fichier d'état (sent.json)")
    parser.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="force la fenêtre de collecte (en heures) au lieu de la dernière exécution",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="logs de debug")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()

    # Vérifications avant tout traitement : on échoue vite et clairement.
    if not os.environ.get("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY manquant (voir .env.example)")
        return 2
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not args.dry_run:
        log.error("DISCORD_WEBHOOK_URL manquant (voir .env.example)")
        return 2

    started = datetime.now(timezone.utc)
    try:
        code = run(args)
    except Exception as exc:  # échec global : on prévient dans le Chat
        log.exception("Échec de la veille")
        if webhook and not args.dry_run:
            notifier.send_error(webhook, f"{type(exc).__name__} : {exc}")
        return 1
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("Terminé en %.1fs", duration)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
