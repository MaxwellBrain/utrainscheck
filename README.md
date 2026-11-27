# 📡 Utrains Pulse

Bot de veille quotidienne **DevOps / DevSecOps / Cloud / LLMOps**.
Chaque jour à **8h00 (America/Chicago)**, GitHub Actions lance un script Python qui :

1. collecte les actualités des dernières 24 h depuis des **flux officiels** (RSS/Atom),
2. les fait trier, regrouper et résumer en français par l'**API Gemini**,
3. publie un message par sujet dans un **salon Discord** via un webhook, illustré quand une image est disponible.

Pas d'interface web, pas de base de données.

## Règle n°1 : chaque info a sa source

* Chaque sujet affiche la **source**, sa **date de publication** et un **lien cliquable** vers l'article d'origine.
* **Gemini ne produit jamais d'URL.** Chaque article collecté reçoit un identifiant (`a1`, `a2`, …) ; le modèle ne renvoie que des identifiants, et le code retrouve l'URL, le titre d'origine et la date dans le flux collecté. Tout identifiant inconnu est ignoré.
* Un article dont l'URL ne répond pas `200` (vérification `HEAD` puis `GET`) est écarté.
* Quand plusieurs sources parlent du même sujet, la **source officielle** (l'éditeur de l'outil, via `official_for` dans `config.yaml`) est affichée en premier.

## Arborescence

```
main.py                         orchestration, état (sent.json), CLI
collector.py                    collecte des flux, nettoyage, vérification des URL
analyzer.py                     appel Gemini (JSON strict) + sélection des sujets
notifier.py                     construction et envoi des messages Discord
config.yaml                     sources, mots-clés, seuils (modifiable sans toucher au code)
requirements.txt
.env.example
.github/workflows/daily-pulse.yml
```

## Installation locale

```bash
python -m venv .venv
. .venv/bin/activate        # Windows : .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env        # puis renseigner les deux valeurs
```

### 1. Créer le webhook Discord

1. Dans Discord, clic droit sur le **salon** qui recevra la veille → **Modifier le salon**.
2. Onglet **Intégrations** → **Webhooks** → **Nouveau webhook**.
3. Nommer le webhook (`Utrains Pulse`), choisir une image si besoin, puis **Copier l'URL du webhook**.

L'URL ressemble à `https://discord.com/api/webhooks/123456789/AbCdEf...`.
Il faut être administrateur du serveur, ou avoir la permission **Gérer les webhooks**.

> Cette URL est un secret : elle suffit à poster dans le salon. Si elle fuite,
> supprime le webhook et recrée-en un.

### 2. Obtenir la clé Gemini

1. Aller sur [Google AI Studio](https://aistudio.google.com/apikey).
2. **Create API key**, puis copier la clé (`AIza…`).

### 3. Ajouter les secrets GitHub

Dépôt → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** :

| Secret | Obligatoire | Valeur |
| --- | --- | --- |
| `GEMINI_API_KEY` | oui | la clé Google AI Studio |
| `DISCORD_WEBHOOK_URL` | oui | l'URL du webhook Discord |
| `GEMINI_MODEL` | non | modèle essayé en premier ; sinon la liste `gemini.models` de `config.yaml` |

## Lancer un test

```bash
python main.py --dry-run                   # affiche le message, n'envoie rien
python main.py --dry-run --since-hours 48  # fenêtre élargie si l'actu est calme
python main.py --dry-run -v                # logs détaillés (sources, rejets)
python main.py                             # envoi réel dans Discord
```

`--dry-run` ne modifie pas `sent.json` : le prochain envoi réel reste complet.
Depuis GitHub : onglet **Actions** → **Utrains Pulse** → **Run workflow** (le
déclenchement manuel ignore le contrôle d'horaire).

## Ajouter une source

Ajouter un bloc dans la section `sources` de `config.yaml` :

```yaml
  - name: Grafana Blog
    url: https://grafana.com/blog/index.xml
    domain: DevOps          # DevOps | DevSecOps | Cloud | LLMOps
    enabled: true
    filter_keywords: true   # true pour une source généraliste
    official_for: [grafana, loki, tempo]
```

* `filter_keywords: true` → l'article doit contenir au moins un mot-clé de `keywords.include`.
* `official_for` → les outils dont cette source est l'éditeur officiel (priorité d'affichage).
* `enabled: false` suffit à désactiver une source sans la supprimer.

Les seuils se règlent dans `general` : `max_topics`, `min_importance`,
`lookback_hours`, `max_article_chars`, `state_retention_days`…

## Format du message

**Un message Discord par sujet**, façon fil d'actualité illustré : s'il y a de
l'actualité sur Terraform, Ansible, Docker et AWS, cela fait quatre messages
distincts, pas un seul bloc.

Chaque message est un embed :

```
☸️ DevOps                                  ← domaine (couleur de la barre)
🆕 Kubernetes 1.35 est disponible          ← titre cliquable vers l'article
Résumé en deux phrases.

🔗 Kubernetes Blog — 26/11/2026 · Lire l'article
🔗 CNCF Blog — 26/11/2026 · Lire l'article
[ photo de l'article ]
Kubernetes Blog · importance 5/5
```

Un message d'en-tête ouvre la série :
`📡 Utrains Pulse — Veille du jeudi 27 novembre 2026 · 8 sujets · 23 articles
analysés depuis 23 sources`.

* 8 à 10 sujets maximum, importance ≥ 3, triés par domaine (DevOps, Cloud, DevSecOps, LLMOps) puis par importance, avec au moins un sujet LLMOps si l'actualité en propose.
* **Illustration** : image fournie par le flux, sinon balise Open Graph de la page de l'article. Chaque image est vérifiée (HTTP 200 et type `image/*`) avant envoi ; sans image valide, le message s'affiche simplement sans photo.
* Catégories : `🆕 Nouveauté`, `⚠️ Déprécié / Remplacé`, `🔐 Sécurité`, `💡 Nouvelle pratique`, `📈 Tendance`. Pour la fiabilité, Gemini renvoie une clé courte (`nouveaute`, `deprecie`, `securite`, `pratique`, `tendance`) et le code ajoute l'emoji.
* En cas de dépréciation, une ligne `Remplacement : X → Y` est ajoutée.
* Aucune actu importante → `Rien de majeur aujourd'hui ✅`.

Réglages dans la section `discord` de `config.yaml` :

| Clé | Effet |
| --- | --- |
| `message_per_topic` | `true` (défaut) : un message par sujet. `false` : un seul message groupé par domaine, découpé si trop long. |
| `send_header` | message d'en-tête avec la date et la volumétrie |
| `image_style` | `large` (photo sous le texte), `thumbnail` (vignette), `none` |
| `username` | nom affiché comme auteur des messages |

## Fiabilité

* Une source en panne n'interrompt pas la collecte : elle est listée en bas du message (`Sources indisponibles : …`).
* Plusieurs modèles Gemini sont essayés dans l'ordre (`gemini.models`) : un modèle inconnu (404) est écarté, un modèle surchargé (503) ou au quota (429) laisse la place au suivant, puis une nouvelle série est tentée. Si aucun ne répond, le message d'erreur liste les modèles réellement accessibles avec la clé.
* Retry avec backoff exponentiel et jitter sur les flux, l'API Gemini et le webhook ; le rate limit Discord (`429`) est respecté via `retry_after`.
* Échec complet du script → message d'erreur court dans le salon ; échec plus en amont (dépendances, checkout) → message envoyé par l'étape `failure()` du workflow.
* Anti-doublon : `sent.json` conserve 30 jours d'URL déjà envoyées et la date de la dernière exécution ; le workflow le commite après chaque envoi. Si le job n'a pas tourné pendant plusieurs jours, la fenêtre de rattrapage est plafonnée à `max_lookback_hours`.
* Aucune mention automatique : `allowed_mentions` est vidé, un titre d'article contenant `@here` ne ping personne.

## Horaire

GitHub Actions ne planifie qu'en UTC. 8h00 à Chicago vaut 14h00 UTC en hiver et
13h00 UTC en été : le workflow se déclenche aux deux heures et une première
étape ne laisse passer que l'exécution correspondant à 8h00 locales.

> GitHub désactive les workflows planifiés après 60 jours sans activité sur le
> dépôt ; il suffit de les réactiver depuis l'onglet **Actions**.
