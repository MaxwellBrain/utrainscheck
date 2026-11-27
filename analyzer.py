"""Tri, regroupement et résumé des articles par l'API Gemini.

Garde-fou central : Gemini ne renvoie **que des identifiants d'articles**.
Les URL, titres d'origine et dates proviennent toujours du collecteur, jamais
du modèle. Tout identifiant inconnu est ignoré.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from collector import Article, retry_call

log = logging.getLogger(__name__)


class PermanentGeminiError(RuntimeError):
    """Erreur Gemini définitive : réessayer donnerait le même résultat."""

#: Clés renvoyées par Gemini -> libellé affiché dans Discord.
#: On fait choisir une clé simple au modèle, puis le code met l'emoji :
#: c'est plus fiable que de demander des emojis au modèle.
CATEGORY_LABELS: dict[str, str] = {
    "nouveaute": "🆕 Nouveauté",
    "deprecie": "⚠️ Déprécié / Remplacé",
    "securite": "🔐 Sécurité",
    "pratique": "💡 Nouvelle pratique",
    "tendance": "📈 Tendance",
}

#: Emoji seul, utilisé devant le titre dans la carte Discord.
CATEGORY_EMOJI: dict[str, str] = {
    key: label.split(" ", 1)[0] for key, label in CATEGORY_LABELS.items()
}

#: Domaines autorisés, dans l'ordre d'affichage du message.
DOMAINS: tuple[str, ...] = ("DevOps", "Cloud", "DevSecOps", "LLMOps")

SYSTEM_INSTRUCTION = """\
Tu es un ingénieur DevOps/LLMOps senior qui prépare une veille quotidienne pour \
une équipe francophone.

Règles absolues :
- Tu travailles UNIQUEMENT à partir des articles fournis. Tu n'ajoutes aucune \
information de mémoire, tu n'inventes rien, tu ne complètes rien.
- Tu ne produis JAMAIS d'URL, de nom de source ni de date : tu renvoies \
seulement les identifiants (ids) des articles fournis.
- Tu regroupes dans un même sujet les articles qui parlent du même événement.
- Tu écartes : les annonces marketing sans contenu technique, les articles hors \
périmètre (DevOps, DevSecOps, Cloud, LLMOps), les contenus purement \
commerciaux, événementiels ou RH.
- Les titres et résumés sont en français, clairs, sans jargon inutile et sans \
superlatifs. Le résumé fait exactement deux phrases.
- `remplacement` est renseigné (« X → Y ») seulement si un outil, une API ou \
une fonctionnalité est déprécié ou remplacé ; sinon tu renvoies null.
- `importance` : 5 = action requise rapidement (faille exploitée, dépréciation \
avec date butoir), 4 = impact direct sur les équipes, 3 = utile à connaître, \
1-2 = anecdotique.
"""

PROMPT_TEMPLATE = """\
Voici les {count} articles collectés ces dernières 24 heures.

{articles}

Produis la liste des sujets réellement intéressants pour une équipe DevOps / \
LLMOps, regroupés par événement, en respectant strictement le schéma JSON \
demandé. N'utilise que les ids ci-dessus.
"""


# ---------------------------------------------------------------------------
# Modèle de données
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Topic:
    """Un sujet de veille : la synthèse Gemini + les articles sources résolus."""

    titre: str
    resume: str
    categorie_key: str
    domaine: str
    importance: int
    remplacement: str | None = None
    articles: list[Article] = field(default_factory=list)

    @property
    def categorie(self) -> str:
        """Libellé complet, emoji inclus (logs, debug)."""
        return CATEGORY_LABELS.get(self.categorie_key, CATEGORY_LABELS["tendance"])

    @property
    def categorie_emoji(self) -> str:
        """Emoji de catégorie affiché devant le titre dans le message."""
        return CATEGORY_EMOJI.get(self.categorie_key, CATEGORY_EMOJI["tendance"])

    @property
    def published(self):
        """Date de publication de l'article principal (le plus pertinent)."""
        return self.articles[0].published


# ---------------------------------------------------------------------------
# Schéma de sortie imposé à Gemini
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    required=["sujets"],
    properties={
        "sujets": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                required=["ids", "titre", "resume", "categorie", "domaine", "importance"],
                properties={
                    "ids": types.Schema(
                        type=types.Type.ARRAY,
                        description="Identifiants des articles concernés (ex. a3).",
                        items=types.Schema(type=types.Type.STRING),
                    ),
                    "titre": types.Schema(
                        type=types.Type.STRING,
                        description="Titre court en français (90 caractères max).",
                    ),
                    "resume": types.Schema(
                        type=types.Type.STRING,
                        description="Exactement deux phrases en français.",
                    ),
                    "categorie": types.Schema(
                        type=types.Type.STRING,
                        enum=list(CATEGORY_LABELS),
                    ),
                    "remplacement": types.Schema(
                        type=types.Type.STRING,
                        nullable=True,
                        description="« X → Y » si dépréciation, sinon null.",
                    ),
                    "domaine": types.Schema(type=types.Type.STRING, enum=list(DOMAINS)),
                    "importance": types.Schema(type=types.Type.INTEGER),
                },
            ),
        )
    },
)


# ---------------------------------------------------------------------------
# Appel Gemini
# ---------------------------------------------------------------------------


def resolve_model(config: dict[str, Any]) -> str:
    """Modèle Gemini : variable d'environnement sinon valeur de config.yaml."""
    env_model = (os.environ.get("GEMINI_MODEL") or "").strip()
    return env_model or str(config["gemini"].get("default_model", "gemini-3.8-flash"))


def _render_articles(articles: Sequence[Article]) -> str:
    """Met les articles en texte brut, un bloc par article, id en tête."""
    blocks = []
    for article in articles:
        blocks.append(
            f"[{article.id}] source={article.source} | domaine={article.domain} | "
            f"date={article.published.date().isoformat()}\n"
            f"titre: {article.title}\n"
            f"contenu: {article.content}"
        )
    return "\n\n".join(blocks)


def _call_gemini(
    client: genai.Client,
    model: str,
    prompt: str,
    temperature: float,
    attempts: int,
) -> dict[str, Any]:
    """Appelle Gemini en JSON strict, avec retry/backoff, et renvoie le dict."""

    def _do() -> dict[str, Any]:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                ),
            )
        except genai_errors.ClientError as exc:
            status = getattr(exc, "code", None)
            if status == 429:  # quota : le retry a du sens
                raise
            hint = ""
            if status == 404:
                hint = (
                    f" — le modèle « {model} » n'existe pas ou n'est plus accessible ; "
                    "ajuste gemini.default_model dans config.yaml ou le secret GEMINI_MODEL"
                )
            raise PermanentGeminiError(f"Gemini a refusé la requête ({status}){hint} : {exc}")
        raw = (response.text or "").strip()
        if not raw:
            raise ValueError("réponse Gemini vide")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("réponse Gemini inattendue (objet JSON attendu)")
        return payload

    return retry_call(
        _do,
        attempts=attempts,
        base_delay=3.0,
        max_delay=45.0,
        fatal=(PermanentGeminiError,),
        label="Gemini",
    )


def _order_articles(articles: list[Article], topic_text: str) -> list[Article]:
    """Source officielle (éditeur de l'outil) d'abord, puis par date décroissante."""
    return sorted(
        articles,
        key=lambda art: (0 if art.is_official_for(topic_text) else 1, -art.published.timestamp()),
    )


def analyze(
    articles: Sequence[Article],
    config: dict[str, Any],
    api_key: str | None = None,
) -> list[Topic]:
    """Fait trier et résumer les articles par Gemini, puis revalide tout côté code."""
    if not articles:
        return []

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY manquant")

    gemini_config = config["gemini"]
    model = resolve_model(config)
    client = genai.Client(api_key=key)
    prompt = PROMPT_TEMPLATE.format(count=len(articles), articles=_render_articles(articles))

    log.info("Analyse de %d articles avec le modèle %s", len(articles), model)
    payload = _call_gemini(
        client,
        model=model,
        prompt=prompt,
        temperature=float(gemini_config.get("temperature", 0.2)),
        attempts=int(gemini_config.get("max_attempts", 4)),
    )

    by_id = {article.id: article for article in articles}
    topics: list[Topic] = []

    for raw_topic in payload.get("sujets") or []:
        if not isinstance(raw_topic, dict):
            continue

        # Résolution des ids -> articles réels. Un id inventé est ignoré.
        resolved: list[Article] = []
        for article_id in raw_topic.get("ids") or []:
            article = by_id.get(str(article_id).strip())
            if article is not None and article not in resolved:
                resolved.append(article)
        if not resolved:
            log.warning("Sujet ignoré : aucun id valide (%s)", raw_topic.get("titre"))
            continue

        titre = str(raw_topic.get("titre") or "").strip()
        resume = str(raw_topic.get("resume") or "").strip()
        if not titre or not resume:
            log.warning("Sujet ignoré : titre ou résumé vide")
            continue

        category_key = str(raw_topic.get("categorie") or "tendance").strip().lower()
        if category_key not in CATEGORY_LABELS:
            category_key = "tendance"

        domaine = str(raw_topic.get("domaine") or "").strip()
        if domaine not in DOMAINS:
            domaine = resolved[0].domain if resolved[0].domain in DOMAINS else "DevOps"

        remplacement = raw_topic.get("remplacement")
        remplacement = str(remplacement).strip() if remplacement else None
        if remplacement and remplacement.lower() in {"null", "none", "-"}:
            remplacement = None

        try:
            importance = int(raw_topic.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        importance = max(1, min(5, importance))

        topics.append(
            Topic(
                titre=titre,
                resume=resume,
                categorie_key=category_key,
                domaine=domaine,
                importance=importance,
                remplacement=remplacement,
                articles=_order_articles(resolved, f"{titre} {resume}"),
            )
        )

    log.info("Gemini a retenu %d sujets", len(topics))
    return topics


# ---------------------------------------------------------------------------
# Sélection finale
# ---------------------------------------------------------------------------


def select_topics(topics: Sequence[Topic], config: dict[str, Any]) -> list[Topic]:
    """Garde les sujets importants, triés, avec au moins un sujet LLMOps."""
    general = config["general"]
    min_importance = int(general.get("min_importance", 3))
    max_topics = int(general.get("max_topics", 10))

    eligible = [topic for topic in topics if topic.importance >= min_importance]
    eligible.sort(key=lambda topic: (-topic.importance, -topic.published.timestamp()))
    selected = eligible[:max_topics]

    # Au moins un sujet LLMOps si l'actualité en propose un.
    if selected and not any(topic.domaine == "LLMOps" for topic in selected):
        llmops = next((topic for topic in eligible if topic.domaine == "LLMOps"), None)
        if llmops is not None:
            selected[-1] = llmops
            selected.sort(key=lambda topic: (-topic.importance, -topic.published.timestamp()))
            log.info("Sujet LLMOps ajouté pour garantir la couverture du domaine")

    log.info(
        "Sélection : %d sujets publiés (importance ≥ %d, plafond %d)",
        len(selected),
        min_importance,
        max_topics,
    )
    return selected
