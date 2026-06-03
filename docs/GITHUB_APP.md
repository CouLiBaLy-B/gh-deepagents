# GitHub App authentication (multi-tenant, prod)

Le mode **PAT** (`GITHUB_TOKEN`) est parfait pour la CLI ou un seul repo. Dès que
tu héberges le serveur webhook pour plusieurs orgs/repos, passe à une **GitHub
App** : tokens éphémères 1h, périmètre de permissions explicite, audit propre,
multi-tenant natif.

## 1. Créer la GitHub App

1. https://github.com/settings/apps → **New GitHub App**
2. **Homepage URL** : ton URL publique
3. **Webhook URL** : `https://deepagent.example.com/webhook`
4. **Webhook secret** : la valeur de `DEEPAGENT_WEBHOOK_SECRET`
5. **Repository permissions** :
   - Contents : **Read & write**
   - Issues : **Read & write**
   - Pull requests : **Read & write**
   - Metadata : **Read**
6. **Subscribe to events** : Issues, Issue comment, Pull request
7. Crée l'App, puis **Generate a private key** → fichier `.pem`.
8. Note l'**App ID** (en haut de la page de l'App).
9. **Install App** → choisis ton org / tes repos.

## 2. Configurer le serveur

Sur la machine qui héberge `gh-deepagent` :

```bash
sudo mkdir -p /etc/gh-deepagent
sudo mv ~/gh-deepagent-app.private-key.pem /etc/gh-deepagent/app.pem
sudo chown deepagent:deepagent /etc/gh-deepagent/app.pem
sudo chmod 600 /etc/gh-deepagent/app.pem
```

Dans `/etc/gh-deepagent.env` (ou `.env`):

```env
DEEPAGENT_GITHUB_APP_ID=123456
DEEPAGENT_GITHUB_APP_PRIVATE_KEY_PATH=/etc/gh-deepagent/app.pem
DEEPAGENT_WEBHOOK_SECRET=********

# IMPORTANT : retirer GITHUB_TOKEN sinon l'App est ignorée n'est pas activée
# (priorité : si DEEPAGENT_GITHUB_APP_ID + clé sont présents, le mode App est utilisé)
```

Avec Docker Compose :

```yaml
services:
  gh-deepagent:
    environment:
      DEEPAGENT_GITHUB_APP_ID: ${DEEPAGENT_GITHUB_APP_ID}
      DEEPAGENT_GITHUB_APP_PRIVATE_KEY_PATH: /run/secrets/app.pem
      DEEPAGENT_WEBHOOK_SECRET: ${DEEPAGENT_WEBHOOK_SECRET}
    secrets:
      - app.pem

secrets:
  app.pem:
    file: ./secrets/app.private-key.pem
```

## 3. Vérifier

```bash
gh-deepagent app-info
# Auth mode: app
# App: My Coding Bot (id=123456, slug=my-coding-bot)
# Installations:
#   • id=789  account=my-org
#   • id=790  account=another-user
```

## 4. Comment ça marche en interne

```
webhook payload arrives
    │
    ▼
GitHubCredentials.shared().remember_installation(repo, payload.installation.id)
    │                            ▲
    ▼                            │ (utilisé partout en aval)
runner → GitHubOps → creds.clone_token_for_repo(repo)
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ if cached_token.expires_at > now + 60s: return it   │
│ else:                                               │
│   create JWT (signed with app private key)          │
│   POST /app/installations/{id}/access_tokens        │
│   cache for ~55 min                                 │
└─────────────────────────────────────────────────────┘
    │
    ▼
git clone https://x-access-token:<TOKEN>@github.com/owner/repo.git
```

- **Pas d'appel API par requête** pour résoudre l'installation : l'id arrive
  directement dans la payload webhook (`installation.id`), on le mémorise.
- **Token cache thread-safe** (`threading.Lock`) — le `ThreadPoolExecutor` du
  serveur peut servir plusieurs orgs en parallèle sans collision.
- **Retry transparent sur 401** : si un push échoue parce que le token a expiré
  pile entre l'auth et le push, `GitHubOps.push()` invalide le cache et retente
  avec un token fraîchement émis.

## 5. Rétro-compat PAT

Le mode App et le mode PAT cohabitent : si seul `GITHUB_TOKEN` est défini, tout
fonctionne comme avant. Si `DEEPAGENT_GITHUB_APP_ID` + clé sont présents, le
mode App prend la priorité.

```python
# Sélection (priorité)
1. DEEPAGENT_GITHUB_APP_ID + DEEPAGENT_GITHUB_APP_PRIVATE_KEY[_PATH]  → "app"
2. GITHUB_TOKEN                                                       → "pat"
3. → erreur explicite au boot
```

## 6. Sécurité

- La clé `.pem` doit avoir `chmod 600` et un owner dédié.
- Active le **webhook secret** (`DEEPAGENT_WEBHOOK_SECRET`) — sans lui, n'importe
  qui peut déclencher l'agent.
- Les tokens d'installation **ne sont jamais loggués**.
- Pour les repos non-confiance, combine App + `DEEPAGENT_BACKEND=daytona|modal` :
  l'agent tourne dans un sandbox, et seul l'host (qui n'exécute pas de code
  utilisateur) voit les tokens.

## 7. Migration depuis un PAT

```bash
# 1. Crée la GitHub App, installe-la sur tes repos
# 2. Remplis DEEPAGENT_GITHUB_APP_ID + _PRIVATE_KEY_PATH
# 3. Supprime GITHUB_TOKEN du serveur
# 4. Redémarre :
docker compose restart gh-deepagent
gh-deepagent app-info     # vérifie "Auth mode: app"
```

Les commits faits par l'App apparaîtront sous l'identité `<app-slug>[bot]` au
lieu de ton compte perso — plus propre dans l'historique git.
