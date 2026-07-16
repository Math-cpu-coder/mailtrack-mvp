"""
MailTrack MVP - Backend de tracking d'ouverture d'emails
==========================================================

Ce fichier contient les routes qui font tout le travail :
- reçoit l'appel du pixel de tracking (quand le destinataire ouvre l'email)
- enregistre l'événement dans une base Postgres (persistante, contrairement
  au SQLite utilisé au tout début du projet, qui était remis à zéro à
  chaque redémarrage du service Render gratuit)
- renvoie un gif transparent 1x1 (invisible dans l'email)

Variable d'environnement requise :
    DATABASE_URL — la chaîne de connexion Postgres (fournie par Render
    quand tu crées une base "PostgreSQL", section "Internal Database URL"
    si le backend tourne aussi sur Render).

Pour lancer en local (si jamais) :
    pip install -r requirements.txt
    export DATABASE_URL="postgresql://..."
    uvicorn main:app --reload
"""

import ipaddress
import os
import uuid as uuid_lib
from datetime import datetime, timezone

import psycopg2
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

app = FastAPI(title="MailTrack MVP")

# Autorise les appels fetch() faits depuis Gmail (mail.google.com) et
# depuis la popup de l'extension (origine chrome-extension://..., dont
# l'identifiant change à chaque installation) vers ce backend. Sans ça,
# le navigateur bloque silencieusement les requêtes fetch() par sécurité
# (politique CORS) — contrairement aux balises <img>, qui elles ne sont
# jamais soumises à cette restriction, d'où le fait que le pixel de
# tracking marchait déjà très bien sans ce middleware.
#
# ⚠️ MVP : on autorise "*" pour simplifier (l'ID d'extension change par
# installation). À resserrer avant un vrai lancement public (ex : lister
# précisément les origines autorisées, ou valider une clé API).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ["DATABASE_URL"]

# Le user-agent que Google utilise parfois quand IL télécharge l'image en
# amont (pour la mettre en cache sur ses propres serveurs). Ça arrive
# souvent dans les 1-2 secondes après l'envoi, PAS quand le destinataire
# ouvre vraiment le mail. Il faut filtrer ça pour ne pas compter un "faux
# positif". On a constaté que ce marqueur n'est pas TOUJOURS présent dans
# le user-agent (Google ne l'ajoute pas systématiquement) — d'où l'ajout
# ci-dessous d'une détection complémentaire par plage d'IP, plus fiable.
GOOGLE_PROXY_MARKERS = ["GoogleImageProxy", "via ggpht.com"]

# Plages d'IP connues appartenant à Google (utilisées entre autres par leurs
# proxys de préchargement d'images dans Gmail). Liste non exhaustive
# (Google en possède beaucoup d'autres, et ça évolue), mais elle couvre les
# plages les plus fréquemment vues dans ce genre de trafic. À défaut d'une
# API officielle simple pour ça, c'est une heuristique "best effort" — le
# but est de réduire les faux négatifs du filtre par user-agent, pas
# d'être parfaitement exhaustif.
GOOGLE_IP_RANGES = [
    ipaddress.ip_network(cidr)
    for cidr in [
        "66.102.0.0/20",
        "74.125.0.0/16",
        "108.177.8.0/21",
        "142.250.0.0/15",
        "172.217.0.0/16",
        "172.253.0.0/16",
        "209.85.128.0/17",
        "216.58.0.0/16",
        "64.233.160.0/19",
        "35.190.0.0/17",
    ]
]

# Si deux événements "réels" (ni proxy, ni auto-vue de l'expéditeur)
# arrivent à moins de X secondes d'intervalle, on considère que c'est la
# même session de lecture (ex : plusieurs images/ressources se rechargent
# en un coup d'œil) plutôt que deux ouvertures distinctes du mail.
SESSION_GROUPING_WINDOW_SECONDS = 30

# Le plus petit gif transparent valide qui existe (1x1 pixel), en bytes bruts.
TRANSPARENT_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def is_google_ip(ip_address: str) -> bool:
    """Vérifie si une IP appartient à une plage connue de Google."""
    try:
        ip = ipaddress.ip_address(ip_address)
    except ValueError:
        return False
    return any(ip in network for network in GOOGLE_IP_RANGES)


def is_likely_google_proxy(user_agent: str, ip_address: str) -> bool:
    """Combine la détection par user-agent (historique) et par plage d'IP
    (plus fiable, ajoutée après avoir constaté que Google n'ajoute pas
    toujours son marqueur dans le user-agent)."""
    has_marker = any(marker in user_agent for marker in GOOGLE_PROXY_MARKERS)
    return has_marker or is_google_ip(ip_address)


def get_db():
    """Ouvre une connexion Postgres. Chaque appel ouvre sa propre connexion
    et doit être fermé par l'appelant (pas de pool pour ce MVP — le volume
    de requêtes reste très faible).

    connect_timeout=10 : sans ça, si la connexion bloque (mauvaise config
    réseau, base pas encore prête, etc.), l'appel peut attendre
    indéfiniment sans jamais échouer ni réussir — ce qui empêchait Render
    de détecter que le service avait démarré (timeout de 15 min observé).
    Avec cette limite, on échoue vite et clairement à la place.
    """
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def init_db():
    """Crée les tables si elles n'existent pas encore. Appelé une fois au
    démarrage du service.

    On attrape toute exception ici plutôt que de laisser planter le
    démarrage : mieux vaut un service qui répond (même avec un message
    d'erreur clair sur /) qu'un démarrage qui timeout silencieusement côté
    Render sans aucune info exploitable dans les logs.
    """
    try:
        conn = get_db()
    except Exception as exc:
        print(f"[MailTrack] ERREUR de connexion à Postgres au démarrage : {exc!r}")
        raise

    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS opens (
            id SERIAL PRIMARY KEY,
            tracking_id TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            is_likely_google_proxy INTEGER DEFAULT 0
        )
        """
    )
    # Table qui retient l'adresse IP de l'expéditeur pour chaque tracking_id.
    # Utile pour distinguer "l'expéditeur qui a lui-même chargé le pixel au
    # moment de l'envoi" (son propre navigateur voit l'image dans le DOM
    # avant même que le mail ne parte) d'une vraie ouverture par le
    # destinataire.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sender_ips (
            tracking_id TEXT PRIMARY KEY,
            sender_ip TEXT NOT NULL,
            registered_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


@app.get("/track/{tracking_id}")
@app.head("/track/{tracking_id}")
async def track_open(tracking_id: str, request: Request):
    """
    Route appelée par le pixel <img> injecté dans l'email.
    tracking_id = identifiant unique généré au moment de l'envoi de l'email.

    On accepte GET et HEAD : Gmail (et d'autres clients) font souvent une
    requête HEAD pour valider une "image par URL" avant de l'insérer dans
    le brouillon. Si on ne répond qu'à GET, Gmail refuse d'insérer l'image
    avec une erreur "impossible de trouver l'image".
    """
    user_agent = request.headers.get("user-agent", "")
    ip_address = request.client.host if request.client else "unknown"

    is_proxy = is_likely_google_proxy(user_agent, ip_address)

    # On n'enregistre l'événement que pour les vraies requêtes GET.
    # Les requêtes HEAD sont juste des vérifications techniques (par Gmail
    # ou d'autres clients) et ne doivent pas compter comme une "ouverture".
    if request.method == "GET":
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO opens (tracking_id, opened_at, ip_address, user_agent, is_likely_google_proxy)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                tracking_id,
                datetime.now(timezone.utc).isoformat(),
                ip_address,
                user_agent,
                int(is_proxy),
            ),
        )
        conn.commit()
        cur.close()
        conn.close()

    # On renvoie TOUJOURS le pixel (même sur HEAD, sans body dans ce cas),
    # sinon l'image casse dans le mail ou Gmail refuse l'insertion.
    return Response(content=TRANSPARENT_GIF, media_type="image/gif")


@app.post("/register-sender/{tracking_id}")
async def register_sender(tracking_id: str, request: Request):
    """
    Appelée par l'extension juste après avoir injecté le pixel dans un email
    (en tâche de fond, sans bloquer l'envoi). Comme cette requête part
    directement du navigateur de l'expéditeur, l'IP vue ici EST l'IP de
    l'expéditeur. On la stocke pour pouvoir ensuite, dans /stats, exclure
    les événements qui viennent de cette même IP (= l'expéditeur qui a vu
    sa propre image en la composant, pas le destinataire qui ouvre le mail).
    """
    sender_ip = request.client.host if request.client else "unknown"

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sender_ips (tracking_id, sender_ip, registered_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (tracking_id) DO UPDATE SET sender_ip = EXCLUDED.sender_ip
        """,
        (tracking_id, sender_ip, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {"tracking_id": tracking_id, "sender_ip_registered": True}


@app.get("/stats/{tracking_id}")
async def get_stats(tracking_id: str):
    """
    Renvoie l'historique des événements pour un tracking_id donné, avec le
    calcul de "vraies ouvertures probables" (en excluant le proxy Google et
    l'expéditeur lui-même).
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT opened_at, ip_address, user_agent, is_likely_google_proxy
        FROM opens
        WHERE tracking_id = %s
        ORDER BY opened_at ASC
        """,
        (tracking_id,),
    )
    rows = cur.fetchall()

    cur.execute(
        "SELECT sender_ip FROM sender_ips WHERE tracking_id = %s",
        (tracking_id,),
    )
    sender_row = cur.fetchone()
    cur.close()
    conn.close()

    sender_ip = sender_row[0] if sender_row else None

    events = []
    for row in rows:
        ip_address = row[1]
        is_self_view = sender_ip is not None and ip_address == sender_ip
        events.append(
            {
                "opened_at": row[0],
                "ip_address": ip_address,
                "user_agent": row[2],
                "is_likely_google_proxy": bool(row[3]),
                "is_likely_self_view": is_self_view,
            }
        )

    # Une "vraie" ouverture, c'est ni le proxy Google qui précharge l'image,
    # ni l'expéditeur lui-même qui a vu le pixel se charger dans son propre
    # navigateur au moment de l'envoi.
    real_opens = [
        e for e in events if not e["is_likely_google_proxy"] and not e["is_likely_self_view"]
    ]

    # Regroupe les "vraies ouvertures" rapprochées dans le temps en une
    # seule "session de lecture" — évite de compter 3 ou 4 "ouvertures"
    # quand il s'agit en fait d'un seul coup d'œil au mail qui déclenche
    # plusieurs rechargements de ressources en quelques secondes.
    open_sessions = 0
    previous_time = None
    for event in real_opens:
        current_time = datetime.fromisoformat(event["opened_at"])
        if previous_time is None or (current_time - previous_time).total_seconds() > SESSION_GROUPING_WINDOW_SECONDS:
            open_sessions += 1
        previous_time = current_time

    return {
        "tracking_id": tracking_id,
        "total_events": len(events),
        "likely_real_opens": len(real_opens),
        "open_sessions": open_sessions,
        "sender_ip": sender_ip,
        "events": events,
    }


@app.post("/generate-id")
async def generate_tracking_id():
    """
    Petit endpoint utilitaire : génère un UUID pour un nouvel email à tracker.
    """
    new_id = str(uuid_lib.uuid4())
    return {"tracking_id": new_id}


@app.get("/")
async def root():
    return {"status": "MailTrack MVP backend is running"}
