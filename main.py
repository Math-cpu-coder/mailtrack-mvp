"""
MailTrack MVP - Backend de tracking d'ouverture d'emails
==========================================================

Ce fichier contient une seule route qui fait tout le travail :
- reçoit l'appel du pixel de tracking (quand le destinataire ouvre l'email)
- enregistre l'événement dans SQLite
- renvoie un gif transparent 1x1 (invisible dans l'email)

Pour lancer :
    pip install -r requirements.txt
    uvicorn main:app --reload

Puis teste dans ton navigateur :
    http://localhost:8000/track/test-123

Et regarde les événements enregistrés :
    http://localhost:8000/stats/test-123
"""

from fastapi import FastAPI, Request
from fastapi.responses import Response
from datetime import datetime, timezone
import sqlite3
import uuid as uuid_lib

app = FastAPI(title="MailTrack MVP")

DB_PATH = "tracking.db"

# Le user-agent que Google utilise quand IL télécharge l'image en amont
# (pour la mettre en cache sur ses propres serveurs). Ça arrive souvent
# dans les 1-2 secondes après l'envoi, PAS quand le destinataire ouvre
# vraiment le mail. Il faut filtrer ça pour ne pas compter un "faux positif".
GOOGLE_PROXY_MARKERS = ["GoogleImageProxy", "via ggpht.com"]

# Le plus petit gif transparent valide qui existe (1x1 pixel), en bytes bruts.
TRANSPARENT_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def get_db():
    """Ouvre une connexion SQLite et s'assure que la table existe."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_id TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            is_likely_google_proxy INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


@app.on_event("startup")
def startup():
    # Crée la base au démarrage si elle n'existe pas encore
    get_db().close()


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

    is_proxy = any(marker in user_agent for marker in GOOGLE_PROXY_MARKERS)

    # On n'enregistre l'événement que pour les vraies requêtes GET.
    # Les requêtes HEAD sont juste des vérifications techniques (par Gmail
    # ou d'autres clients) et ne doivent pas compter comme une "ouverture".
    if request.method == "GET":
        conn = get_db()
        conn.execute(
            """
            INSERT INTO opens (tracking_id, opened_at, ip_address, user_agent, is_likely_google_proxy)
            VALUES (?, ?, ?, ?, ?)
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
        conn.close()

    # On renvoie TOUJOURS le pixel (même sur HEAD, sans body dans ce cas),
    # sinon l'image casse dans le mail ou Gmail refuse l'insertion.
    return Response(content=TRANSPARENT_GIF, media_type="image/gif")


@app.get("/stats/{tracking_id}")
async def get_stats(tracking_id: str):
    """
    Route de debug pour voir les événements enregistrés pour un email donné.
    Dans la vraie extension, ce sera cette route (ou une variante) qui alimentera
    le badge "Ouvert il y a 2min" affiché dans Gmail.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT opened_at, ip_address, user_agent, is_likely_google_proxy
        FROM opens
        WHERE tracking_id = ?
        ORDER BY opened_at ASC
        """,
        (tracking_id,),
    ).fetchall()
    conn.close()

    events = [
        {
            "opened_at": row[0],
            "ip_address": row[1],
            "user_agent": row[2],
            "is_likely_google_proxy": bool(row[3]),
        }
        for row in rows
    ]

    # On calcule un "vrai" nombre d'ouvertures en excluant les probables
    # requêtes du proxy Google, pour donner un chiffre plus honnête.
    real_opens = [e for e in events if not e["is_likely_google_proxy"]]

    return {
        "tracking_id": tracking_id,
        "total_events": len(events),
        "likely_real_opens": len(real_opens),
        "events": events,
    }


@app.post("/generate-id")
async def generate_tracking_id():
    """
    Petit endpoint utilitaire : génère un UUID pour un nouvel email à tracker.
    Plus tard, l'extension appellera cette route juste avant l'envoi du mail,
    puis injectera <img src="http://ton-domaine/track/{tracking_id}"> dedans.
    """
    new_id = str(uuid_lib.uuid4())
    return {"tracking_id": new_id}


@app.get("/")
async def root():
    return {"status": "MailTrack MVP backend is running"}
