# URL Monitor

Un petit service qui vérifie toutes les 60 secondes une liste d'URL, enregistre pour chaque vérification le code de statut, le temps de réponse et l'horodatage, et expose les résultats récents en JSON sur `GET /status`.

Stack : Python 3.13, FastAPI, httpx, SQLite. Livré sous forme d'image Docker.

Instance en ligne :

- https://monitor.newsmap.ch/status
- https://monitor.newsmap.ch/health

## Démarrage rapide

```bash
docker build -t url-monitor .
docker run --rm -p 127.0.0.1:8000:8000 url-monitor
curl http://127.0.0.1:8000/status
```

L'image contient une liste d'URL publiques par défaut (`urls.txt`), elle fonctionne donc sans aucun montage. Pour utiliser sa propre liste et conserver les résultats entre deux redémarrages :

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -v ./my-urls.txt:/app/urls.txt:ro \
  -v monitor-data:/data \
  url-monitor
```

Sur un système avec SELinux (Fedora, RHEL), ajouter `,z` au montage du fichier : `:ro,z`.

### Sans Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn url_monitor.main:app
```

L'application doit tourner dans **un seul processus** : le worker vit à l'intérieur (pas d'option `--workers`).

## Configuration

Le fichier d'URL contient une URL par ligne. Les lignes vides et les lignes commençant par `#` sont ignorées :

```
# Sites surveillés
https://example.com
https://www.python.org/about/
```

Le service refuse de démarrer si le fichier est invalide : schéma autre que `http`/`https`, hôte manquant, identifiants dans l'URL, port invalide ou liste vide. Toutes les lignes fautives sont signalées en une fois. Les doublons sont ignorés.

| Variable | Défaut (hors Docker) | Défaut (Docker) |
|---|---|---|
| `URLS_FILE` | `urls.txt` | `/app/urls.txt` |
| `DB_PATH` | `data/monitor.db` | `/data/monitor.db` |

Valeurs fixées dans le code : intervalle de 60 s, délai maximal de 5 s par vérification, 10 vérifications simultanées au plus, conservation des résultats pendant 7 jours.

## API

### `GET /status?limit=10`

Les `limit` derniers résultats (10 par défaut, de 1 à 100) de chaque URL du fichier, du plus récent au plus ancien :

```json
{
  "urls": [
    {
      "url": "https://example.com",
      "checks": [
        {"checked_at": "2026-11-24T12:01:00.000000+00:00", "status_code": 200, "error": null, "response_time_ms": 111.3},
        {"checked_at": "2026-11-24T12:00:00.000000+00:00", "status_code": null, "error": "timeout", "response_time_ms": 5000.0}
      ]
    }
  ]
}
```

- Chaque vérification a soit un `status_code`, soit une `error`, jamais les deux.
- Valeurs possibles de `error` : `timeout`, `dns_error`, `connection_error`, `tls_error`, `blocked` (adresse non publique, voir Sécurité), `other_error`, `internal_error` (bug dans le service lui-même).
- `response_time_ms` mesure le temps entre l'envoi de la requête et la réception des en-têtes de réponse, sans la résolution DNS. Il vaut `0.0` pour `dns_error` et `blocked`, car aucune requête n'a été envoyée.
- Tous les horodatages sont en UTC.

### `GET /health`

`200 {"status": "ok"}` si le worker tourne et que la base répond, sinon `503 {"status": "unhealthy"}`. Utilisé par le `HEALTHCHECK` de l'image.

## Tests

```bash
.venv/bin/pytest
```

Les tests couvrent le chargement de la liste d'URL, le modèle de données, le stockage (dont la rétention), le garde-fou SSRF, le checker, le worker (concurrence, cadence, gestion des erreurs) et l'API. Aucun test n'accède au réseau : le DNS et les serveurs HTTP sont simulés (`httpx.MockTransport`).

## Sécurité

Le worker envoie des requêtes depuis le réseau où il tourne. La principale menace est donc la **SSRF** : une URL qui pointerait, directement ou via le DNS, vers un service interne (interface d'administration, routeur, endpoint de métadonnées cloud).

- **Seules les adresses publiques sont contactées.** Les adresses privées, de loopback, link-local, multicast, réservées, etc., en IPv4 comme en IPv6, sont refusées (`blocked`). La vérification a lieu à chaque requête, pas seulement au démarrage.
- **Protection contre le DNS rebinding.** Le checker se connecte à l'adresse IP exacte qui a été validée, tout en envoyant le vrai nom d'hôte (en-tête `Host` et SNI). Le certificat TLS reste vérifié pour ce nom.
- **Les variables de proxy de l'environnement sont ignorées**, car un proxy contournerait cette vérification.
- **API en lecture seule**, sans aucun moyen d'ajouter des URL. `/docs` et `/openapi.json` sont désactivés. Les URL contenant des identifiants sont refusées, puisque `/status` les afficherait.
- **Le conteneur tourne avec un utilisateur non privilégié** (UID 10001). Le code lui appartient en lecture seule, et seul `/data` est accessible en écriture. Le `.dockerignore` fonctionne en liste blanche, pour qu'aucun fichier local (secret, base de données) n'entre dans l'image.

## Déploiement

Le service tourne sur un serveur Proxmox personnel, exposé publiquement via un Cloudflare Tunnel.

```
Internet ──HTTPS──▶ Cloudflare ◀──tunnel sortant── cloudflared ──HTTP──▶ 127.0.0.1:8000 (conteneur)
                                                   └────────── VM Ubuntu 24.04 ──────────┘
```

- **VM dédiée** (Ubuntu 24.04, 1 vCPU, 1 Go de RAM), clonée depuis un template cloud-init.
- **Docker installé depuis le dépôt officiel.** Les commandes passent par `sudo` : l'utilisateur n'est pas ajouté au groupe `docker`, qui équivaut à un accès root.
- **Conteneur lancé avec des options de durcissement** :

  ```bash
  sudo docker run -d --name url-monitor --restart unless-stopped \
    -p 127.0.0.1:8000:8000 -v monitor-data:/data \
    --read-only --tmpfs /tmp:size=16m \
    --cap-drop ALL --security-opt no-new-privileges \
    --memory 256m --pids-limit 100 \
    --log-opt max-size=10m --log-opt max-file=3 \
    url-monitor
  ```

  Ces options rendent le système de fichiers en lecture seule, hormis le volume `/data` et un petit `/tmp` en mémoire. Elles retirent toutes les capacités Linux, interdisent toute élévation de privilèges, limitent la mémoire et le nombre de processus, et assurent la rotation des journaux, que Docker ne fait pas par défaut.
- **Le port n'est publié que sur `127.0.0.1`.** L'application n'est joignable ni depuis le réseau local, ni depuis Internet en direct.
- **`cloudflared` tourne comme service systemd** sur la VM, avec un tunnel géré depuis le tableau de bord Cloudflare, et une route vers `http://localhost:8000`. Le tunnel est une connexion sortante : aucun port n'est ouvert sur le routeur, l'IP du domicile reste masquée, et Cloudflare fournit le HTTPS et absorbe le trafic abusif. Le jeton du tunnel n'est stocké que dans le service, jamais dans le dépôt.
- **Accès public, sans authentification.** Cloudflare Access peut être ajouté devant le domaine sans modifier le code.

## Limites et améliorations possibles

- L'intervalle, le délai, la concurrence et la rétention ne sont pas configurables sans modifier le code.
- Pas d'authentification : ni dans l'application, ni dans le déploiement actuel (voir Déploiement).
- Pas de limitation du nombre de requêtes sur l'adresse publique. Une règle de rate limiting Cloudflare peut être ajoutée.
- La VM peut joindre le réseau local. Le garde-fou SSRF est la seule barrière ; une règle du pare-feu Proxmox bloquant l'interface d'administration ajouterait une seconde ligne de défense.
- Pas de configuration de journalisation. Les erreurs et avertissements s'affichent avec le format par défaut de Python.
- Docker seul ne redémarre pas un conteneur `unhealthy` : il faut un orchestrateur pour cela.
- Les adresses privées ne peuvent pas être surveillées, ce qui exclut les services du réseau local.
- Le client de test de Starlette signale que `httpx` est déprécié au profit de `httpx2`.
