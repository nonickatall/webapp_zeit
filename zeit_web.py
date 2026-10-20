"""zeit_web.py — Verwaltung der Zeiterfassung (webapp_zeit), Stufe A lesen, Stufe B Auftraege an die Engine.

Getrennt von der Standard-Webapp. Eigene Anmeldung (zeit_auth.py), jede Route
ausser Anmeldung/Einmal-Link verlangt eine gueltige Sitzung -- keine Ausnahme
fuer interne Requests, kein Fallback-User. Jeder Zugang ist genau einem aktiven
myhome.user zugeordnet; angezeigt wird nur mit dem Recht zef_lesen.
Die Webapp schreibt nie in myhome_zeit.

Start: gunicorn -w 2 -b 0.0.0.0:5010 --certfile ... --keyfile ... zeit_web:app
"""
import csv
import datetime
import io
import logging
import re
import uuid

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for
from jinja2 import Undefined

from zeit_auth import MIN_PASSWORT, AuthDB
from zeit_db import ZeitDB, lade_konfig, ortszeit, zeitraum_utc
from zeit_hilfe import markdown_html
import zeit_mqtt

cfg = lade_konfig()
logging.basicConfig(filename=cfg["log_datei"], level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webapp_zeit")

db = ZeitDB(cfg)
auth = AuthDB(cfg["auth_db"], cfg["leerlauf_sek"], cfg["max_fehlversuche"], cfg["sperre_sek"])

app = Flask(__name__)
COOKIE = "zeit_session"
RECHT_LESEN = "zef_lesen"
RECHT_ZEITREGEL = "zef_zeitregel"
RECHT_KORREKTUR = "zef_korrektur"
ANFRAGE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
OEFFENTLICH = {"login", "passwort", "static"}
# Die Hilfe zur Anmeldung ist auch ohne Sitzung lesbar, alle anderen nur angemeldet.
HILFE_OEFFENTLICH = {"Anmeldung"}


def client_ip():
    return request.remote_addr or ""


def _log_db(berechtigter, anforderung, aktion):
    """Anmeldeereignisse in myhome.logdaten; ein Fehler hier blockiert nichts."""
    try:
        db.logdaten(berechtigter, anforderung, aktion, client_ip())
    except Exception as e:
        log.error(f"logdaten fehlgeschlagen ({anforderung}/{aktion}): {e}")


# ---------------------------------------------------------------- Filter
@app.template_filter("zeit")
def f_zeit(utc, fmt="%d.%m.%Y %H:%M"):
    o = ortszeit(utc) if isinstance(utc, datetime.datetime) else None
    return o.strftime(fmt) if o else "–"


@app.template_filter("dauer")
def f_dauer(minuten):
    # None oder fehlende Spalte (z.B. korrektur_minuten vor Stufe B) -> "–"
    if minuten is None or isinstance(minuten, Undefined):
        return "–"
    m = int(minuten)
    vz = "-" if m < 0 else ""
    m = abs(m)
    return f"{vz}{m // 60}:{m % 60:02d}"


@app.context_processor
def vorlagen_werte():
    return {"config_leerlauf": cfg["leerlauf_sek"]}


# ---------------------------------------------------------------- Anmeldung
@app.before_request
def anmeldung_pruefen():
    if request.endpoint in OEFFENTLICH:
        return None
    if request.endpoint == "hilfe" and (request.view_args or {}).get("ueberschrift") in HILFE_OEFFENTLICH:
        return None
    b = auth.sitzung_pruefen(request.cookies.get(COOKIE))
    if b is None:
        log.info(f"keine gueltige Sitzung fuer {request.path} von {client_ip()}: {auth.letzter_grund}")
        return redirect(url_for("login"))
    mu = db.myhome_user(b["myhome_user_id"])
    if mu is None:
        auth.sitzung_loeschen(request.cookies.get(COOKIE))
        _log_db(b["benutzername"], "zugriff", "abgewiesen:myhome_user_ungueltig")
        return redirect(url_for("login"))
    if not db.hat_recht(mu["id"], RECHT_LESEN):
        _log_db(mu["name"], "zugriff", f"abgewiesen:kein_recht {RECHT_LESEN}")
        return render_template("kein_recht.html", mu=mu), 403
    g.benutzer, g.mu = b, mu
    return None


@app.after_request
def kopfzeilen(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    return resp


def _cookie_setzen(resp, sid):
    # Sitzungs-Cookie ohne max_age: endet mit dem Browser, kein "angemeldet bleiben"
    resp.set_cookie(COOKIE, sid, secure=True, httponly=True, samesite="Strict", path="/zeit")
    return resp


@app.route("/zeit/hilfe/<ueberschrift>")
def hilfe(ueberschrift):
    """Hilfetext einer Seite aus myhome_zeit.hilfe, als sicheres HTML."""
    try:
        r = db.hilfe(ueberschrift)
    except Exception as e:
        log.error(f"hilfe({ueberschrift}): {e}")
        return {"ueberschrift": ueberschrift, "html": "<p>Die Hilfe ist gerade nicht verfügbar.</p>"}, 500
    if r is None or not (r["text"] or "").strip():
        return {"ueberschrift": ueberschrift, "html": "<p>Für diese Seite gibt es noch keinen Hilfetext.</p>"}
    return {"ueberschrift": r["ueberschrift"], "html": markdown_html(r["text"])}


@app.route("/")
@app.route("/zeit/")
def start():
    return redirect(url_for("anwesenheit"))


@app.route("/zeit/login", methods=["GET", "POST"])
def login():
    fehler = None
    if request.method == "POST":
        name = (request.form.get("benutzername") or "").strip()
        pw = request.form.get("passwort") or ""
        b, grund = auth.anmelden(name, pw)
        if b is None:
            _log_db(name, "login", f"abgewiesen:{grund}")
            log.warning(f"login {name!r} von {client_ip()}: abgewiesen ({grund})")
            fehler = ("Zu viele Fehlversuche – der Zugang ist vorübergehend gesperrt."
                      if grund in ("gesperrt", "jetzt_gesperrt") else "Anmeldung fehlgeschlagen.")
        else:
            mu = db.myhome_user(b["myhome_user_id"])
            if mu is None:
                _log_db(name, "login", "abgewiesen:myhome_user_ungueltig")
                fehler = "Anmeldung fehlgeschlagen."
            elif not db.hat_recht(mu["id"], RECHT_LESEN):
                _log_db(mu["name"], "login", f"abgewiesen:kein_recht {RECHT_LESEN}")
                fehler = "Für diesen Zugang ist die Zeiterfassung nicht freigegeben."
            else:
                auth.sitzung_loeschen(request.cookies.get(COOKIE))
                sid = auth.sitzung_anlegen(b["id"])
                _log_db(mu["name"], "login", "angemeldet")
                log.info(f"login {name!r} -> myhome.user {mu['id']} von {client_ip()}")
                return _cookie_setzen(redirect(url_for("anwesenheit")), sid)
    return render_template("login.html", fehler=fehler, abgelaufen=request.args.get("abgelaufen"),
                           leerlauf_min=cfg["leerlauf_sek"] // 60, oeffentlich=True, hilfe="Anmeldung")


@app.route("/zeit/abmelden", methods=["POST"])
def abmelden():
    auth.sitzung_loeschen(request.cookies.get(COOKIE))
    _log_db(g.mu["name"], "logout", "abgemeldet")
    resp = redirect(url_for("login"))
    resp.delete_cookie(COOKIE, path="/zeit")
    return resp


@app.route("/zeit/passwort", methods=["GET", "POST"])
def passwort():
    token = request.values.get("t", "")
    b = auth.token_benutzer(token)
    if b is None:
        return render_template("passwort.html", ungueltig=True, oeffentlich=True), 404
    fehler = None
    if request.method == "POST":
        p1, p2 = request.form.get("p1") or "", request.form.get("p2") or ""
        if p1 != p2:
            fehler = "Die beiden Eingaben stimmen nicht überein."
        elif len(p1) < MIN_PASSWORT:
            fehler = f"Mindestens {MIN_PASSWORT} Zeichen."
        else:
            auth.passwort_setzen(token, p1)
            _log_db(b["benutzername"], "passwort", "gesetzt")
            return redirect(url_for("login"))
    return render_template("passwort.html", token=token, benutzername=b["benutzername"],
                           fehler=fehler, min_laenge=MIN_PASSWORT, oeffentlich=True)


# ---------------------------------------------------------------- Parameter
def _int(name):
    try:
        v = int(request.args.get(name, ""))
        return v if v > 0 else None
    except ValueError:
        return None


def _datum(name, vorgabe):
    try:
        return datetime.date.fromisoformat(request.args.get(name, ""))
    except ValueError:
        return vorgabe


def _heute():
    return ortszeit(db.jetzt_utc()).date()


def _csv(name, kopf, zeilen):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(kopf)
    w.writerows(zeilen)
    # BOM, damit Excel die Umlaute richtig liest
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- Seiten
@app.route("/zeit/anwesenheit")
def anwesenheit():
    uid = _int("user")
    a = db.anwesenheit()
    if uid:
        a["offen"] = [r for r in a["offen"] if r["user_id"] == uid]
    return render_template("anwesenheit.html", a=a, uid=uid, mitarbeiter=db.mitarbeiter(),
                           seite="anwesenheit", hilfe="Anwesenheit")


# Statusfilter der Datensaetze: Schluessel -> (Anzeige, Pruefung je Zeile)
STATUS_FILTER = {
    "": ("Alle", lambda r: True),
    "offen": ("offen", lambda r: r["status"] == "offen"),
    "abgeschlossen": ("abgeschlossen", lambda r: r["status"] == "abgeschlossen"),
    "automatisch_beendet": ("automatisch beendet", lambda r: r["status"] == "automatisch_beendet"),
    "ohne_zeitregel": ("ohne Zeitregel", lambda r: r["status"] == "abgeschlossen" and r["pause_minuten"] is None),
    "korrigiert": ("korrigiert", lambda r: bool(r["korrigiert"])),
    "manuell": ("manuell angelegt", lambda r: bool(r["manuell"])),
}


def _datensaetze_param():
    heute = _heute()
    von = _datum("von", heute.replace(day=1))
    bis = _datum("bis", heute)
    if bis < von:
        von, bis = bis, von
    status = request.args.get("status", "")
    if status not in STATUS_FILTER:
        status = ""
    return _int("user"), von, bis, status


def _datensaetze_gefiltert(uid, von, bis, status):
    pruef = STATUS_FILTER[status][1]
    return [r for r in db.datensaetze(uid, *zeitraum_utc(von, bis)) if pruef(r)]


@app.route("/zeit/datensaetze")
def datensaetze():
    uid, von, bis, status = _datensaetze_param()
    zeilen = _datensaetze_gefiltert(uid, von, bis, status)
    return render_template("datensaetze.html", zeilen=zeilen, uid=uid, von=von, bis=bis, status=status,
                           status_auswahl=[(k, v[0]) for k, v in STATUS_FILTER.items()],
                           mitarbeiter=db.mitarbeiter(), seite="datensaetze", hilfe="Datensaetze")


@app.route("/zeit/datensaetze.csv")
def datensaetze_csv():
    uid, von, bis, status = _datensaetze_param()
    zeilen = _datensaetze_gefiltert(uid, von, bis, status)
    kopf = ["ID", "Mitarbeiter", "Kommen", "Gehen", "Brutto", "Pause", "Korrektur", "Netto", "Status",
            "Korrigiert", "Manuell", "Terminal Kommen", "Terminal Gehen"]
    daten = [[r["id"], r["name"], f_zeit(r["kommen"]), f_zeit(r["gehen"]), f_dauer(r["minuten"]),
              f_dauer(r["pause_minuten"]), f_dauer(r.get("korrektur_minuten")), f_dauer(r.get("netto_minuten")),
              r["status"], "ja" if r["korrigiert"] else "nein", "ja" if r["manuell"] else "nein",
              r["terminal_kommen"] or "", r["terminal_gehen"] or ""] for r in zeilen]
    return _csv(f"zeiterfassung_{von}_{bis}.csv", kopf, daten)


@app.route("/zeit/datensatz/<int:ze_id>")
def datensatz(ze_id):
    d = db.datensatz(ze_id)
    if d is None:
        abort(404)
    ze = d["ze"]
    # Korrigierbar: automatisch beendet (Gehen nachtragen) oder abgeschlossen (Stempelzeit), nie offen
    art = {"automatisch_beendet": "gehen_nachtragen", "abgeschlossen": "stempelzeit"}.get(ze["status"])
    korrektur = None
    if art and db.hat_recht(g.mu["id"], RECHT_KORREKTUR):
        def lokal(t):
            return ortszeit(t).strftime("%Y-%m-%dT%H:%M") if t else ""
        def stand(t):
            return t.strftime("%Y-%m-%d %H:%M:%S") if t else ""
        korrektur = {"art": art, "kommen": lokal(ze["kommen"]),
                     # beim Nachtragen ist das automatische Gehen nur ein Platzhalter -> leer anbieten
                     "gehen": "" if art == "gehen_nachtragen" else lokal(ze["gehen"]),
                     "stand_kommen": stand(ze["kommen"]), "stand_gehen": stand(ze["gehen"]),
                     "stand_status": ze["status"]}
    return render_template("datensatz.html", d=d, korrektur=korrektur, seite="datensaetze", hilfe="Datensatz")


OFFEN_GRUPPEN = [("automatisch_beendet", "Automatisch beendet (Gehen fehlt)"),
                 ("ohne_zeitregel", "Ohne Pause wegen fehlender Zeitregel"),
                 ("offen_alt", "Offen seit vor heute")]


@app.route("/zeit/offen")
def offen():
    uid = _int("user")
    art = request.args.get("art", "")
    if art not in dict(OFFEN_GRUPPEN):
        art = ""
    f = db.offene_faelle()
    if uid:
        for schluessel, _ in OFFEN_GRUPPEN:
            f[schluessel] = [r for r in f[schluessel] if r["user_id"] == uid]
    return render_template("offen.html", f=f, uid=uid, art=art, gruppen=OFFEN_GRUPPEN,
                           mitarbeiter=db.mitarbeiter(), seite="offen", hilfe="Offen")


@app.route("/zeit/zeitregeln")
def zeitregeln():
    uid = _int("user")
    darf = db.hat_recht(g.mu["id"], RECHT_ZEITREGEL)
    return render_template("zeitregeln.html", regeln=db.zeitregeln(uid), uid=uid,
                           mitarbeiter=db.mitarbeiter(), darf_anlegen=darf,
                           alle_user=db.aktive_user() if darf else [], heute=_heute(),
                           seite="zeitregeln", hilfe="Zeitregeln")


# ---------------------------------------------------------------- Auftraege (Stufe B)
def _auftrag_anfrage():
    """Nur per fetch aus der eigenen Seite: JSON + eigener Kopf (zusaetzlich zu SameSite=Strict)."""
    if request.headers.get("X-Zeit-Auftrag") != "1" or not request.is_json:
        abort(400)
    return request.get_json(silent=True) or {}


@app.route("/zeit/zeitregeln/neu", methods=["POST"])
def zeitregel_neu():
    """Auftrag "Zeitregel anlegen" an die Engine. Die Webapp schreibt nicht selbst;
    verbindlich prueft der Eingang das Recht, inhaltlich prueft der zeiterfassung_server."""
    d = _auftrag_anfrage()
    if not db.hat_recht(g.mu["id"], RECHT_ZEITREGEL):
        _log_db(g.mu["name"], "zeitregel", f"abgewiesen:kein_recht {RECHT_ZEITREGEL}")
        return {"ok": False, "fehler": "Kein Recht zum Anlegen von Zeitregeln."}, 403
    # dieselbe anfrage_id beim "Erneut versuchen" -> der Dienst erkennt die Wiederholung
    anfrage_id = str(d.get("anfrage_id") or "").lower() or str(uuid.uuid4())
    if not ANFRAGE_ID_RE.match(anfrage_id):
        return {"ok": False, "fehler": "Ungültige Anfrage."}, 400
    try:
        param = {
            "user_id": int(d.get("user_id")),
            "gueltig_ab": str(d.get("gueltig_ab") or ""),
            "pause_minuten": int(d.get("pause_minuten")),
            "pause_ab_minuten": int(d.get("pause_ab_minuten")),
            "sollbeginn": str(d.get("sollbeginn") or ""),
            "begruendung": str(d.get("begruendung") or "").strip()[:255],
        }
    except (TypeError, ValueError):
        return {"ok": False, "fehler": "Bitte alle Pflichtfelder als Zahl/Datum ausfüllen."}, 400
    if len(param["begruendung"]) < 3:
        return {"ok": False, "fehler": "Bitte eine Begründung angeben (mindestens 3 Zeichen)."}, 400
    nummer = (g.mu.get("nummer") or "").strip()
    if not nummer:
        return {"ok": False, "fehler": "Für Ihren Zugang ist keine Rufnummer hinterlegt."}, 403
    try:
        zeit_mqtt.senden(cfg, RECHT_ZEITREGEL, nummer, anfrage_id, zeit_mqtt.param_kodieren(param))
    except Exception as e:
        log.error(f"zeitregel_neu: Versand fehlgeschlagen: {e}")
        return {"ok": False, "fehler": "Der Auftrag konnte nicht gesendet werden."}, 503
    _log_db(g.mu["name"], "zeitregel", f"auftrag gesendet fuer user {param['user_id']}")
    log.info(f"zeitregel_neu: {g.mu['name']} -> user {param['user_id']} ab {param['gueltig_ab']}, "
             f"anfrage_id={anfrage_id}")
    return {"ok": True, "anfrage_id": anfrage_id}


@app.route("/zeit/datensatz/<int:ze_id>/korrektur", methods=["POST"])
def korrektur_neu(ze_id):
    """Auftrag "Korrektur" (Gehen nachtragen / Stempelzeit aendern) an die Engine. Die Webapp
    schreibt nicht selbst; verbindlich prueft der Eingang das Recht, inhaltlich der zeiterfassung_server
    (Status, Stand, Ueberschneidung, Zukunft, Tag)."""
    d = _auftrag_anfrage()
    if not db.hat_recht(g.mu["id"], RECHT_KORREKTUR):
        _log_db(g.mu["name"], "korrektur", f"abgewiesen:kein_recht {RECHT_KORREKTUR}")
        return {"ok": False, "fehler": "Kein Recht für Korrekturen."}, 403
    anfrage_id = str(d.get("anfrage_id") or "").lower() or str(uuid.uuid4())
    if not ANFRAGE_ID_RE.match(anfrage_id):
        return {"ok": False, "fehler": "Ungültige Anfrage."}, 400
    art = str(d.get("art") or "")
    if art not in ("gehen_nachtragen", "stempelzeit"):
        return {"ok": False, "fehler": "Ungültige Korrekturart."}, 400
    param = {
        "zeiterfassung_id": ze_id, "art": art,
        "kommen": str(d.get("kommen") or "") if art == "stempelzeit" else "",
        "gehen": str(d.get("gehen") or ""),
        "stand": {"kommen": str(d.get("stand_kommen") or ""), "gehen": str(d.get("stand_gehen") or ""),
                  "status": str(d.get("stand_status") or "")},
        "begruendung": str(d.get("begruendung") or "").strip()[:255],
    }
    if not param["gehen"] or (art == "stempelzeit" and not param["kommen"]):
        return {"ok": False, "fehler": "Bitte die Zeiten ausfüllen."}, 400
    if len(param["begruendung"]) < 3:
        return {"ok": False, "fehler": "Bitte eine Begründung angeben (mindestens 3 Zeichen)."}, 400
    nummer = (g.mu.get("nummer") or "").strip()
    if not nummer:
        return {"ok": False, "fehler": "Für Ihren Zugang ist keine Rufnummer hinterlegt."}, 403
    try:
        zeit_mqtt.senden(cfg, RECHT_KORREKTUR, nummer, anfrage_id, zeit_mqtt.param_kodieren(param))
    except Exception as e:
        log.error(f"korrektur_neu: Versand fehlgeschlagen: {e}")
        return {"ok": False, "fehler": "Der Auftrag konnte nicht gesendet werden."}, 503
    _log_db(g.mu["name"], "korrektur", f"auftrag gesendet fuer zeiterfassung {ze_id} ({art})")
    log.info(f"korrektur_neu: {g.mu['name']} -> zeiterfassung {ze_id} {art}, anfrage_id={anfrage_id}")
    return {"ok": True, "anfrage_id": anfrage_id}


GRUND_TEXT = {
    "rueckwirkend": "„gültig ab“ darf nicht in der Vergangenheit liegen.",
    "bereits_vorhanden": "Für diesen Mitarbeiter gibt es ab diesem Tag schon eine Zeitregel.",
    "mitarbeiter_unbekannt": "Der Mitarbeiter ist unbekannt oder gesperrt.",
    "begruendung_fehlt": "Die Begründung fehlt.",
    "bearbeiter_unbekannt": "Ihr Zugang konnte keinem aktiven Benutzer zugeordnet werden.",
    "datensatz_unbekannt": "Den Datensatz gibt es nicht.",
    "inzwischen_geaendert": "Der Datensatz wurde inzwischen geändert. Bitte die Seite neu laden.",
    "falscher_status": "Dieser Datensatz kann so nicht korrigiert werden (Status passt nicht).",
    "gehen_vor_kommen": "Gehen muss nach Kommen liegen.",
    "in_zukunft": "Die Zeit darf nicht in der Zukunft liegen.",
    "anderer_tag": "Kommen darf nicht auf einen anderen Tag verschoben werden.",
    "keine_aenderung": "Es wurde nichts geändert.",
    "ueberschneidung": "Die Zeiten überschneiden sich mit einem anderen Datensatz des Mitarbeiters.",
    "zeit_ungueltig": "Die Zeitangabe ist ungültig.",
    "zeit_umstellung": "Diese Uhrzeit gibt es wegen der Zeitumstellung nicht eindeutig.",
}


@app.route("/zeit/auftrag/<anfrage_id>")
def auftrag(anfrage_id):
    """Ergebnis eines eigenen Auftrags -- nur aus zeiterfassung_auftrag, nie optimistisch.
    Ungueltig, nicht vorhanden oder fremd: dieselbe 404."""
    anfrage_id = (anfrage_id or "").lower()
    r = db.auftrag(anfrage_id, g.mu["id"]) if ANFRAGE_ID_RE.match(anfrage_id) else None
    if r is None:
        return {"ok": False}, 404
    grund = r["grund"] or ""
    text = GRUND_TEXT.get(grund) or (f"Ungültige Angabe ({grund.split(':', 1)[1]})."
                                     if grund.startswith("param_ungueltig:") else grund)
    return {"ok": True, "ergebnis": r["ergebnis"], "grund": grund, "text": text,
            "zeitregel_id": r["zeitregel_id"],
            "zeiterfassung_id": r["zeiterfassung_id"], "uhrzeit": f_zeit(r["eingegangen"], "%H:%M")}


def _monat():
    heute = _heute()
    try:
        j, m = (int(x) for x in request.args.get("monat", "").split("-"))
        if 2000 <= j <= 2100 and 1 <= m <= 12:
            return j, m
    except ValueError:
        pass
    return heute.year, heute.month


@app.route("/zeit/summen")
def summen():
    j, m = _monat()
    zeilen, von, bis = db.summen(j, m)
    return render_template("summen.html", zeilen=zeilen, monat=f"{j:04d}-{m:02d}", von=von, bis=bis,
                           seite="summen", hilfe="Summen")


@app.route("/zeit/summen.csv")
def summen_csv():
    j, m = _monat()
    zeilen, von, bis = db.summen(j, m)
    kopf = ["Mitarbeiter", "Tage", "Datensaetze", "Brutto", "Pause", "Netto", "Netto vollstaendig",
            "Offen", "Ohne Zeitregel", "Automatisch beendet"]
    daten = [[s["name"], s["tage"], s["datensaetze"], f_dauer(s["brutto"]), f_dauer(s["pause"]),
              f_dauer(s["netto"]), "ja" if s["netto_vollstaendig"] else "nein",
              s["offen"], s["ohne_regel"], s["auto"]] for s in zeilen]
    return _csv(f"zeiterfassung_summen_{j:04d}-{m:02d}.csv", kopf, daten)


@app.errorhandler(404)
def nicht_gefunden(e):
    return render_template("fehler.html", text="Nicht gefunden."), 404


@app.errorhandler(500)
def serverfehler(e):
    log.exception("Serverfehler")
    return render_template("fehler.html", text="Interner Fehler – Details im Log."), 500
