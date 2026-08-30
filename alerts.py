"""Alertes de veille (§5.F audit veille, 30/08/2026, Lot 1 §1.4, dernier morceau après le
scheduler et le plafond de coût IA déjà livrés) : "tous les ingrédients d'un digest sont déjà
là" -- evidence_bucket_transitions, site_profiles.health_score, actor_events -- mais rien ne les
évalue en règles explicites ni ne les restitue comme delta consultable.

Quatre règles retenues sur les six proposées par l'audit (les deux autres -- seuil de
capability_spec franchi, nouveau candidat acteur au-dessus d'un score -- dépendent
respectivement d'une référence non définie et du pipeline de découverte du §4.D, non construit) :

- bucket_transition_existing : un couple marché/composant/opération suivi passe radar -> existing
  (evidence_bucket_transitions, en excluant les créations -- voir §10.9 : 104 des 109 lignes
  étaient des créations, seules 5 de vraies transitions).
- new_fact_high_value_actor : un nouveau fait 'existing' apparaît chez un acteur C1/C2.
- collection_incident : une anomalie de collecte vient d'être détectée pour un acteur (site_
  profiles.needs_reprofile passé à 1 -- voir scrapers.adaptive_decision), qu'il s'agisse d'un
  échec réseau répété ou d'une structure de page qui s'effondre.
- ma_funding_event : un actor_events de type brevet/investissement/acquisition (voir
  press.SIGNAL_KEYWORDS) vient d'être capté.

Idempotent par fingerprint (INSERT OR IGNORE), comme evidence_sources/offer_sources/
vocabulary_candidates : relancer capture_alerts() plusieurs fois ne duplique jamais une alerte
déjà vue. event_at porte l'horodatage de l'ÉVÉNEMENT (pas de la capture), pour que
GET /api/digest?since= ne restitue que du changement -- jamais un état répété (la règle que
l'audit pose explicitement : "un digest qui répète l'existant n'est plus lu au bout de trois
semaines").
"""

from __future__ import annotations

import datetime
import hashlib

from db import ACTORS_DB, MARKET_DB, connect, utc_now

# Fenêtre de rattrapage à chaque capture : suffisamment large pour ne rien manquer entre deux
# collectes espacées (ex: reprise après une pause), mais bornée -- fingerprint gère l'idempotence,
# ce n'est pas la fenêtre qui protège des doublons, seulement de la charge de la requête.
ALERTS_LOOKBACK_DAYS = 90

# event_type d'actor_events considérés comme signal M&A/financement (voir press.SIGNAL_KEYWORDS
# pour 'investment' ; 'acquisition' existe aussi en production comme valeur héritée d'avant ce
# lexique).
_MA_FUNDING_EVENT_TYPES = ("investment", "acquisition")


def _fp(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _bucket_transition_alerts(since: str) -> list[dict]:
    with connect(MARKET_DB) as db:
        rows = db.execute(
            """SELECT t.evidence_id,t.from_bucket,t.to_bucket,t.changed_at,e.actor_name,e.market,e.component,e.operation
               FROM evidence_bucket_transitions t JOIN evidence e ON e.id=t.evidence_id
               WHERE t.to_bucket='existing' AND t.from_bucket IS NOT NULL AND t.from_bucket!='existing' AND t.changed_at>=?""",
            (since,),
        ).fetchall()
    alerts = []
    for row in rows:
        summary = f"{row['actor_name']} : {row['market'] or '?'} / {row['component'] or '?'} / {row['operation'] or '?'} passe en production"
        alerts.append({
            "alert_type": "bucket_transition_existing",
            "actor_name": row["actor_name"],
            "summary": summary,
            "detail": f"{row['from_bucket']} -> {row['to_bucket']}",
            "source_url": None,
            "event_at": row["changed_at"],
            "fingerprint": _fp("bucket_transition_existing", str(row["evidence_id"]), row["changed_at"]),
        })
    return alerts


def _new_fact_high_value_actor_alerts(since: str) -> list[dict]:
    with connect(ACTORS_DB) as adb:
        high_value = {
            row["name"] for row in adb.execute("SELECT name FROM actors WHERE competitive_class IN ('C1','C2')")
        }
    if not high_value:
        return []
    with connect(MARKET_DB) as db:
        rows = db.execute(
            "SELECT id,actor_name,market,component,operation,source_url,created_at FROM evidence WHERE bucket='existing' AND created_at>=?",
            (since,),
        ).fetchall()
    alerts = []
    for row in rows:
        if row["actor_name"] not in high_value:
            continue
        summary = f"{row['actor_name']} (acteur prioritaire) : nouveau fait en production -- {row['market'] or '?'} / {row['component'] or '?'} / {row['operation'] or '?'}"
        alerts.append({
            "alert_type": "new_fact_high_value_actor",
            "actor_name": row["actor_name"],
            "summary": summary,
            "detail": None,
            "source_url": row["source_url"],
            "event_at": row["created_at"],
            "fingerprint": _fp("new_fact_high_value_actor", str(row["id"])),
        })
    return alerts


def _collection_incident_alerts(since: str) -> list[dict]:
    with connect(ACTORS_DB) as db:
        rows = db.execute(
            """SELECT a.name,p.last_error,p.health_score,p.last_profiled_at FROM site_profiles p
               JOIN actors a ON a.id=p.actor_id
               WHERE p.needs_reprofile=1 AND p.last_profiled_at IS NOT NULL AND p.last_profiled_at>=?""",
            (since,),
        ).fetchall()
    alerts = []
    for row in rows:
        summary = f"{row['name']} : incident de collecte détecté ({row['last_error'] or 'anomalie'})"
        alerts.append({
            "alert_type": "collection_incident",
            "actor_name": row["name"],
            "summary": summary,
            "detail": f"health_score={row['health_score']}",
            "source_url": None,
            "event_at": row["last_profiled_at"],
            "fingerprint": _fp("collection_incident", row["name"], row["last_profiled_at"]),
        })
    return alerts


def _ma_funding_alerts(since: str) -> list[dict]:
    placeholders = ",".join("?" for _ in _MA_FUNDING_EVENT_TYPES)
    with connect(ACTORS_DB) as db:
        rows = db.execute(
            f"""SELECT e.id,a.name,e.event_type,e.description,e.source_url,e.created_at FROM actor_events e
                JOIN actors a ON a.id=e.actor_id
                WHERE e.event_type IN ({placeholders}) AND e.review_status!='rejected' AND e.created_at>=?""",
            (*_MA_FUNDING_EVENT_TYPES, since),
        ).fetchall()
    alerts = []
    for row in rows:
        summary = f"{row['name']} : {row['event_type']} -- {row['description'][:140]}"
        alerts.append({
            "alert_type": "ma_funding_event",
            "actor_name": row["name"],
            "summary": summary,
            "detail": row["event_type"],
            "source_url": row["source_url"],
            "event_at": row["created_at"],
            "fingerprint": _fp("ma_funding_event", str(row["id"])),
        })
    return alerts


def capture_alerts() -> dict[str, int]:
    """Évalue les 4 règles sur la fenêtre de rattrapage et insère les nouvelles alertes
    (idempotent par fingerprint). Retourne {alert_type: nb inséré}, pour le journal d'appel
    (voir app._run_job)."""
    since_dt = datetime.datetime.fromisoformat(utc_now()) - datetime.timedelta(days=ALERTS_LOOKBACK_DAYS)
    since = since_dt.isoformat()

    candidates = (
        _bucket_transition_alerts(since)
        + _new_fact_high_value_actor_alerts(since)
        + _collection_incident_alerts(since)
        + _ma_funding_alerts(since)
    )
    stamp = utc_now()
    inserted: dict[str, int] = {}
    with connect(MARKET_DB) as db:
        for alert in candidates:
            before = db.total_changes
            db.execute(
                """INSERT OR IGNORE INTO alerts(alert_type,actor_name,summary,detail,source_url,fingerprint,event_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    alert["alert_type"], alert["actor_name"], alert["summary"], alert["detail"],
                    alert["source_url"], alert["fingerprint"], alert["event_at"], stamp,
                ),
            )
            if db.total_changes > before:
                inserted[alert["alert_type"]] = inserted.get(alert["alert_type"], 0) + 1
    return inserted
