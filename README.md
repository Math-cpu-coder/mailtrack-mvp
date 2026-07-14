# MailTrack MVP — Backend de tracking

Backend minimal pour trackeer l'ouverture d'emails via un pixel invisible.
C'est l'étape 1 du projet : on valide que le tracking marche avant de toucher
à l'extension Chrome.

## Installation (Arch Linux)

```bash
cd mailtrack-mvp

# Créer un environnement virtuel (recommandé, Arch est strict sur les paquets système)
python -m venv venv
source venv/bin/activate

# Installer les dépendances
pip install -r requirements.txt
```

## Lancer le serveur

```bash
uvicorn main:app --reload
```

Le serveur tourne sur `http://localhost:8000`.

## Tester manuellement

1. **Générer un ID de tracking** (simule ce que fera l'extension avant l'envoi d'un mail) :

```bash
curl -X POST http://localhost:8000/generate-id
# → {"tracking_id": "un-uuid-genere"}
```

2. **Simuler l'ouverture de l'email** — colle cette URL dans ton navigateur
   (remplace l'ID par celui généré à l'étape 1, ou utilise "test-123" pour aller vite) :

```
http://localhost:8000/track/test-123
```

Tu devrais voir un pixel transparent se charger sans erreur.

3. **Vérifier que l'événement a bien été enregistré** :

```bash
curl http://localhost:8000/stats/test-123
```

Tu devrais voir une réponse JSON avec l'événement, ton IP, ton user-agent,
et `is_likely_google_proxy: false` (puisque tu n'es pas passé par Gmail).

4. **Test en conditions réelles (le vrai test qui compte)** :
   - Envoie-toi un email à toi-même (Gmail vers Gmail, ou Gmail vers un autre compte)
     contenant du HTML avec l'image :
     `<img src="http://TON_IP_PUBLIQUE:8000/track/mon-test-reel" width="1" height="1">`
   - ⚠️ `localhost` ne marchera pas ici puisque c'est Gmail (donc les serveurs
     de Google) qui va charger l'image, pas ton PC directement. Pour ce test
     réel, il te faudra exposer temporairement ton serveur via un tunnel
     comme `ngrok` ou `cloudflared`, ou déployer sur Railway/Fly.io.
   - Ouvre le mail depuis un autre appareil/compte, puis regarde `/stats/mon-test-reel`.
   - Regarde bien s'il y a un premier événement qui arrive presque instantanément
     après l'envoi (= probablement le proxy Google) suivi d'un second événement
     plus tard quand tu ouvres vraiment le mail.

## Structure de la base de données

Une table SQLite unique `opens` est créée automatiquement au premier lancement
(fichier `tracking.db` généré à côté de `main.py`). Pas besoin de rien configurer.

## Prochaines étapes (pas encore dans ce squelette)

- [ ] Déployer ce backend sur Railway ou Fly.io pour avoir une URL publique stable
- [ ] Remplacer SQLite par Postgres si tu veux passer en prod plus tard
- [ ] Construire l'extension Chrome qui appelle `/generate-id` et injecte le pixel
      automatiquement au moment de l'envoi dans Gmail
- [ ] Ajouter l'auth utilisateur (magic link ou OAuth Google)
- [ ] Ajouter la limite freemium (5 trackings/mois) + intégration Stripe
