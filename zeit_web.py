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
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for
from jinja2 import Undefined

from zeit_auth import MIN_PASSWORT, AuthDB
from zeit_db import ZeitDB, lade_konfig, ortszeit, zeitraum_utc
from zeit_db import tag_grenzen_utc as zeit_db_tag_grenzen
from zeit_hilfe import markdown_html
import zeit_mqtt
import zeit_rechnung

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
RECHT_KLAEREN = "zef_klaeren"
RECHT_STAMMDATEN = "zef_stammdaten"
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


@app.template_filter("mitarbeiter")
def f_mitarbeiter(name, uid):
    """Mitarbeiter mit myhome-User-ID, z.B. 'Herr Peter Mueller (78)' -- erleichtert die Zuordnung."""
    if not name or isinstance(name, Undefined):
        name = "?"
    return f"{name} ({uid})" if uid is not None and not isinstance(uid, Undefined) else name


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
def db_sitzung():
    db.sitzung_beginnen()


@app.teardown_request
def db_sitzung_ende(exc):
    db.sitzung_ende()


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
    "klaeren": ("zu klären", lambda r: bool(r.get("klaeren"))),
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
    darf_anlegen = db.hat_recht(g.mu["id"], RECHT_KORREKTUR)
    return render_template("datensaetze.html", zeilen=zeilen, uid=uid, von=von, bis=bis, status=status,
                           status_auswahl=[(k, v[0]) for k, v in STATUS_FILTER.items()],
                           mitarbeiter=db.mitarbeiter(), darf_anlegen=darf_anlegen,
                           alle_user=db.aktive_user() if darf_anlegen else [], heute=_heute(),
                           seite="datensaetze", hilfe="Datensaetze")


@app.route("/zeit/datensaetze/neu", methods=["POST"])
def datensatz_neu():
    """Auftrag "Datensatz manuell anlegen" (Plan 5.4 Fall 4) -- derselbe Befehl zef_korrektur
    mit art=anlegen. Nur abgeschlossene Datensaetze (Kommen und Gehen)."""
    d = _auftrag_anfrage()
    try:
        param_extra = {"user_id": int(d.get("user_id"))}
    except (TypeError, ValueError):
        return {"ok": False, "fehler": "Bitte einen Mitarbeiter wählen."}, 400
    return _korrektur_senden(d, None, "anlegen", param_extra)


@app.route("/zeit/datensaetze.csv")
def datensaetze_csv():
    uid, von, bis, status = _datensaetze_param()
    zeilen = _datensaetze_gefiltert(uid, von, bis, status)
    kopf = ["ID", "Mitarbeiter-ID", "Mitarbeiter", "Kommen", "Zählt ab", "Gehen", "Brutto", "Pause", "Korrektur",
            "Netto", "Status", "Korrigiert", "Manuell", "Terminal Kommen", "Terminal Gehen"]
    daten = [[r["id"], r["user_id"], r["name"], f_zeit(r["kommen"]), f_zeit(r["zaehlt_ab"]) if r["zaehlt_ab"] else "",
              f_zeit(r["gehen"]), f_dauer(r["minuten"]),
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
                     # Gehen nachtragen: Datum vorbelegt (Tag des Kommens), Uhrzeit leer und Pflicht
                     "datum": ortszeit(ze["kommen"]).strftime("%Y-%m-%d"),
                     # beim Nachtragen ist das automatische Gehen nur ein Platzhalter -> leer anbieten
                     "gehen": "" if art == "gehen_nachtragen" else lokal(ze["gehen"]),
                     "stand_kommen": stand(ze["kommen"]), "stand_gehen": stand(ze["gehen"]),
                     "stand_status": ze["status"]}
        if art == "stempelzeit":
            # Abzug/Zuschlag nur mit Pause (sonst gibt es kein Netto)
            korrektur["abzug"] = ze["pause_minuten"] is not None
            korrektur["korrektur_minuten"] = int(ze.get("korrektur_minuten") or 0)
            # Zeitregel zuordnen nur, wenn fuer den Tag keine Regel nach gueltig_ab gilt
            tag = ortszeit(ze["kommen"]).date()
            regeln = db.zeitregeln(ze["user_id"])
            if not ze.get("tagesplan_id") and not any(r["gueltig_ab"] <= tag for r in regeln):
                korrektur["regeln"] = regeln
    darf_klaeren = db.hat_recht(g.mu["id"], RECHT_KLAEREN)
    return render_template("datensatz.html", d=d, korrektur=korrektur, darf_klaeren=darf_klaeren,
                           zurueck=_zurueck_ziel(),
                           seite="datensaetze", hilfe="Datensatz")


def _zurueck_ziel():
    """Zurueck-Knopf: die aufrufende Liste dieser Webapp (mit ihren Filtern), sonst die Datensaetze.
    Nur eigene /zeit/-Seiten, nie eine fremde Adresse und nie die Detailseite selbst."""
    ref = urlsplit(request.referrer or "")
    if (ref.netloc == request.host and ref.path.startswith("/zeit/")
            and not ref.path.startswith("/zeit/datensatz/") and ref.path != "/zeit/login"):
        return ref.path + ("?" + ref.query if ref.query else "")
    return url_for("datensaetze")


OFFEN_GRUPPEN = [("zu_klaeren", "Zur Klärung markiert"),
                 ("automatisch_beendet", "Automatisch beendet (Gehen fehlt)"),
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


ZEITREGEL_ANZEIGE = {"": "alle", "gueltig": "heute gültige", "aktuell": "gültige und künftige"}


def _regel_stand(regeln, heute):
    """Je Regel r["stand"]: 'gueltig' (je Mitarbeiter die mit dem groessten gueltig_ab <= heute),
    'kuenftig' (gueltig_ab nach heute) oder 'abgeloest'."""
    aktuell = {}
    for r in regeln:
        a = aktuell.get(r["user_id"])
        if r["gueltig_ab"] <= heute and (a is None or r["gueltig_ab"] > a["gueltig_ab"]):
            aktuell[r["user_id"]] = r
    for r in regeln:
        r["stand"] = ("kuenftig" if r["gueltig_ab"] > heute
                      else "gueltig" if aktuell.get(r["user_id"]) is r else "abgeloest")


@app.route("/zeit/zeitregeln")
def zeitregeln():
    uid = _int("user")
    anzeige = request.args.get("anzeige", "")
    if anzeige not in ZEITREGEL_ANZEIGE:
        anzeige = ""
    heute = _heute()
    regeln = db.zeitregeln(uid)
    _regel_stand(regeln, heute)
    if anzeige == "gueltig":
        regeln = [r for r in regeln if r["stand"] == "gueltig"]
    elif anzeige == "aktuell":
        regeln = [r for r in regeln if r["stand"] != "abgeloest"]
    darf = db.hat_recht(g.mu["id"], RECHT_ZEITREGEL)
    return render_template("zeitregeln.html", regeln=regeln, uid=uid, anzeige=anzeige,
                           anzeige_optionen=ZEITREGEL_ANZEIGE,
                           mitarbeiter=db.mitarbeiter(), darf_anlegen=darf,
                           alle_user=db.aktive_user() if darf else [], heute=heute,
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


# ---------------------------------------------------------------- Stammdaten (Umbau Schritt 1)
STAMMDATEN_TEILE = {
    "tagesplaene": "Tagespläne", "wochenplaene": "Wochenpläne", "zuordnung": "Zuordnung",
    "abwesenheitsarten": "Abwesenheitsarten", "feiertage": "Feiertage", "salden": "Startsalden",
    "urlaub": "Urlaubskonto",
}
STAMMDATEN_SPUR = {
    "tagesplaene": ("tagesplan",), "wochenplaene": ("wochenplan",), "zuordnung": ("user_wochenplan",),
    "abwesenheitsarten": ("abwesenheitsart",), "feiertage": ("feiertag",), "salden": ("saldo_start",),
    "urlaub": ("urlaubskonto",),
}
BUNDESLAENDER = {
    "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg", "HB": "Bremen",
    "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland", "SN": "Sachsen",
    "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "TH": "Thüringen",
}
WOCHENTAGE = (("mo", "Mo"), ("di", "Di"), ("mi", "Mi"), ("do", "Do"), ("fr", "Fr"), ("sa", "Sa"), ("so", "So"))
# Erlaubte Felder je Auftragsart; geprueft wird verbindlich im zeiterfassung_server (zeit_stammdaten.py)
STAMMDATEN_FELDER = {
    "tagesplan": ("id", "nr", "name", "farbe", "frei", "soll_minuten", "sollbeginn", "abschneiden",
                  "kernzeit_ende", "pause1_minuten", "pause1_ab_minuten", "pause2_minuten",
                  "pause2_ab_minuten", "aktiv"),
    "wochenplan": ("id", "nr", "name", "tp_mo", "tp_di", "tp_mi", "tp_do", "tp_fr", "tp_sa", "tp_so", "aktiv"),
    "zuordnung": ("user_id", "gueltig_ab", "wochenplan_id"),
    "zuordnung_aufheben": ("id",),
    "abwesenheitsart": ("id", "kuerzel", "name", "farbe", "gutschrift", "urlaub", "reihenfolge", "aktiv"),
    "feiertage_erzeugen": ("jahr", "bundesland"),
    "feiertag": ("datum", "name", "bundesland", "halb", "aktiv"),
    "saldo_start": ("user_id", "stichtag", "minuten", "aktiv"),
    "urlaubskonto": ("user_id", "jahr", "anspruch_tage", "uebertrag_tage"),
    "neu_berechnen": ("user_id", "ab"),
    "abwesenheit": ("user_id", "von", "bis", "art_id", "umfang", "minuten", "notiz"),
    "abwesenheit_aufheben": ("id",),
    "tag_abweichung": ("user_id", "datum", "tagesplan_id"),
    "tag_abweichung_aufheben": ("user_id", "datum"),
}
# Felder, die als Stunden "h:mm" (auch "-26:44") eingegeben werden duerfen -> Minuten
STUNDEN_FELDER = {"soll_minuten", "pause1_ab_minuten", "pause2_ab_minuten", "minuten"}
STUNDEN_RE = re.compile(r"^([+-]?)(\d{1,4}):([0-5]\d)$")
DEZIMAL_RE = re.compile(r"^([+-]?)(\d{1,4})[.,](\d{1,2})$")


def _stunden_minuten(v):
    """h:mm oder Dezimalstunden (5,5 / 5.5 = 5:30) -> Minuten."""
    t = str(v if v is not None else "").strip()
    m = STUNDEN_RE.match(t)
    if m:
        minuten = int(m.group(2)) * 60 + int(m.group(3))
    else:
        m = DEZIMAL_RE.match(t)
        if not m:
            return t     # Zahl (Minuten) oder ungueltig -> der Dienst prueft
        minuten = round(float(m.group(2) + "." + m.group(3)) * 60)
    return -minuten if m.group(1) == "-" else minuten


def _zuordnung_stand(zeilen, heute):
    """stand je Zuordnung: gueltig | kuenftig | abgeloest | aufgehoben."""
    je_user = {}
    for z in zeilen:
        if z["aufgehoben_am"] is not None:
            z["stand"] = "aufgehoben"
        else:
            je_user.setdefault(z["user_id"], []).append(z)
    for liste in je_user.values():
        liste.sort(key=lambda z: z["gueltig_ab"])
        aktuell = None
        for z in liste:
            if z["gueltig_ab"] <= heute:
                aktuell = z
        for z in liste:
            z["stand"] = ("kuenftig" if z["gueltig_ab"] > heute else
                          "gueltig" if z is aktuell else "abgeloest")


@app.route("/zeit/stammdaten")
def stammdaten():
    teil = request.args.get("teil", "")
    if teil not in STAMMDATEN_TEILE:
        teil = "tagesplaene"
    heute = _heute()
    jahr = _int("jahr") or heute.year
    if not 2000 <= jahr <= 2100:
        jahr = heute.year
    darf = db.hat_recht(g.mu["id"], RECHT_STAMMDATEN)
    tps = db.tagesplaene()
    ctx = {"teil": teil, "teile": STAMMDATEN_TEILE, "darf": darf, "jahr": jahr, "heute": heute,
           "tagesplaene": tps, "tp_nach_id": {t["id"]: t for t in tps}, "wochentage": WOCHENTAGE,
           "bundeslaender": BUNDESLAENDER, "bundesland": cfg.get("bundesland", "RP"),
           "alle_user": db.aktive_user(), "spur": db.stammdaten_spur(STAMMDATEN_SPUR[teil])}
    if teil in ("wochenplaene", "zuordnung"):
        wps = db.wochenplaene()
        for w in wps:
            w["summe"] = sum(int((ctx["tp_nach_id"].get(w["tp_" + t]) or {}).get("soll_minuten") or 0)
                             for t, _ in WOCHENTAGE)
        ctx["wochenplaene"] = wps
    if teil == "zuordnung":
        uid = _int("user")
        zeilen = db.zuordnungen(uid)
        _zuordnung_stand(zeilen, heute)
        ctx.update(zuordnungen=zeilen, uid=uid)
    elif teil == "abwesenheitsarten":
        ctx["arten"] = db.abwesenheitsarten()
    elif teil == "feiertage":
        ctx["feiertage"] = db.feiertage(jahr)
    elif teil == "salden":
        ctx["salden"] = db.salden_start()
    elif teil == "urlaub":
        ctx["konten"] = db.urlaubskonten(jahr)
    return render_template("stammdaten.html", seite="stammdaten", hilfe="Stammdaten", **ctx)


@app.route("/zeit/stammdaten/auftrag", methods=["POST"])
def stammdaten_auftrag():
    """Auftrag zef_stammdaten an die Engine; inhaltlich prueft der zeiterfassung_server."""
    d = _auftrag_anfrage()
    if not db.hat_recht(g.mu["id"], RECHT_STAMMDATEN):
        _log_db(g.mu["name"], "stammdaten", f"abgewiesen:kein_recht {RECHT_STAMMDATEN}")
        return {"ok": False, "fehler": "Kein Recht zum Pflegen der Stammdaten."}, 403
    anfrage_id = str(d.get("anfrage_id") or "").lower() or str(uuid.uuid4())
    if not ANFRAGE_ID_RE.match(anfrage_id):
        return {"ok": False, "fehler": "Ungültige Anfrage."}, 400
    art = str(d.get("art") or "")
    if art not in STAMMDATEN_FELDER:
        return {"ok": False, "fehler": "Unbekannte Art."}, 400
    param = {"art": art, "begruendung": str(d.get("begruendung") or "").strip()[:255]}
    for f in STAMMDATEN_FELDER[art]:
        v = d.get(f)
        if isinstance(v, str):
            v = v.strip()[:255]
        param[f] = _stunden_minuten(v) if f in STUNDEN_FELDER else v
    nummer = (g.mu.get("nummer") or "").strip()
    if not nummer:
        return {"ok": False, "fehler": "Für Ihren Zugang ist keine Rufnummer hinterlegt."}, 403
    try:
        zeit_mqtt.senden(cfg, RECHT_STAMMDATEN, nummer, anfrage_id, zeit_mqtt.param_kodieren(param))
    except Exception as e:
        log.error(f"stammdaten_auftrag: Versand fehlgeschlagen: {e}")
        return {"ok": False, "fehler": "Der Auftrag konnte nicht gesendet werden."}, 503
    _log_db(g.mu["name"], "stammdaten", f"auftrag {art} gesendet")
    log.info(f"stammdaten_auftrag: {g.mu['name']} {art}, anfrage_id={anfrage_id}")
    return {"ok": True, "anfrage_id": anfrage_id}


@app.route("/zeit/datensatz/<int:ze_id>/korrektur", methods=["POST"])
def korrektur_neu(ze_id):
    """Auftrag "Korrektur" (Gehen nachtragen, Stempelzeit, Zeitregel zuordnen, Abzug/Zuschlag) an die
    Engine. Die Webapp schreibt nicht selbst; verbindlich prueft der Eingang das Recht, inhaltlich der
    zeiterfassung_server (Status, Stand, Ueberschneidung, Zukunft, Tag)."""
    d = _auftrag_anfrage()
    art = str(d.get("art") or "")
    if art not in ("gehen_nachtragen", "stempelzeit", "abzug", "zeitregel"):
        return {"ok": False, "fehler": "Ungültige Korrekturart."}, 400
    param = {"stand": {"kommen": str(d.get("stand_kommen") or ""), "gehen": str(d.get("stand_gehen") or ""),
                       "status": str(d.get("stand_status") or "")}}
    if art == "abzug":
        try:
            param["korrektur_minuten"] = int(d.get("korrektur_minuten"))
        except (TypeError, ValueError):
            return {"ok": False, "fehler": "Bitte die Minuten als ganze Zahl angeben (z.B. -90)."}, 400
    elif art == "zeitregel":
        try:
            param["zeitregel_id"] = int(d.get("zeitregel_id"))
        except (TypeError, ValueError):
            return {"ok": False, "fehler": "Bitte eine Zeitregel wählen."}, 400
    return _korrektur_senden(d, ze_id, art, param)


@app.route("/zeit/datensatz/<int:ze_id>/klaerung", methods=["POST"])
def klaerung_neu(ze_id):
    """Auftrag "zur Klaerung markieren" (Notiz) bzw. "Klaerung aufheben" (Ergebnis) -- Befehl zef_klaeren."""
    d = _auftrag_anfrage()
    if not db.hat_recht(g.mu["id"], RECHT_KLAEREN):
        _log_db(g.mu["name"], "klaerung", f"abgewiesen:kein_recht {RECHT_KLAEREN}")
        return {"ok": False, "fehler": "Kein Recht zum Markieren."}, 403
    anfrage_id = str(d.get("anfrage_id") or "").lower() or str(uuid.uuid4())
    if not ANFRAGE_ID_RE.match(anfrage_id):
        return {"ok": False, "fehler": "Ungültige Anfrage."}, 400
    art = str(d.get("art") or "")
    if art not in ("markieren", "aufheben"):
        return {"ok": False, "fehler": "Ungültige Angabe."}, 400
    text = str(d.get("text") or "").strip()[:255]
    if len(text) < 3:
        return {"ok": False, "fehler": "Bitte eine Notiz (beim Markieren) bzw. das Ergebnis (beim Aufheben) "
                                       "angeben, mindestens 3 Zeichen."}, 400
    nummer = (g.mu.get("nummer") or "").strip()
    if not nummer:
        return {"ok": False, "fehler": "Für Ihren Zugang ist keine Rufnummer hinterlegt."}, 403
    param = {"zeiterfassung_id": ze_id, "art": art, "text": text}
    try:
        zeit_mqtt.senden(cfg, RECHT_KLAEREN, nummer, anfrage_id, zeit_mqtt.param_kodieren(param))
    except Exception as e:
        log.error(f"klaerung: Versand fehlgeschlagen: {e}")
        return {"ok": False, "fehler": "Der Auftrag konnte nicht gesendet werden."}, 503
    _log_db(g.mu["name"], "klaerung", f"auftrag gesendet fuer zeiterfassung {ze_id} ({art})")
    log.info(f"klaerung: {g.mu['name']} -> zeiterfassung {ze_id} {art}, anfrage_id={anfrage_id}")
    return {"ok": True, "anfrage_id": anfrage_id}


def _korrektur_senden(d, ze_id, art, param):
    """Gemeinsamer Teil aller zef_korrektur-Auftraege: Recht, anfrage_id, Zeiten, Begruendung, Versand."""
    if not db.hat_recht(g.mu["id"], RECHT_KORREKTUR):
        _log_db(g.mu["name"], "korrektur", f"abgewiesen:kein_recht {RECHT_KORREKTUR}")
        return {"ok": False, "fehler": "Kein Recht für Korrekturen."}, 403
    anfrage_id = str(d.get("anfrage_id") or "").lower() or str(uuid.uuid4())
    if not ANFRAGE_ID_RE.match(anfrage_id):
        return {"ok": False, "fehler": "Ungültige Anfrage."}, 400
    param = dict(param, art=art, begruendung=str(d.get("begruendung") or "").strip()[:255])
    if ze_id is not None:
        param["zeiterfassung_id"] = ze_id
    if art in ("gehen_nachtragen", "stempelzeit", "anlegen"):
        param["kommen"] = str(d.get("kommen") or "") if art != "gehen_nachtragen" else ""
        param["gehen"] = str(d.get("gehen") or "")
        if not param["gehen"] or (art != "gehen_nachtragen" and not param["kommen"]):
            return {"ok": False, "fehler": "Bitte die Zeiten ausfüllen."}, 400
    if len(param["begruendung"]) < 3:
        return {"ok": False, "fehler": "Bitte eine Begründung angeben (mindestens 3 Zeichen)."}, 400
    nummer = (g.mu.get("nummer") or "").strip()
    if not nummer:
        return {"ok": False, "fehler": "Für Ihren Zugang ist keine Rufnummer hinterlegt."}, 403
    try:
        zeit_mqtt.senden(cfg, RECHT_KORREKTUR, nummer, anfrage_id, zeit_mqtt.param_kodieren(param))
    except Exception as e:
        log.error(f"korrektur: Versand fehlgeschlagen: {e}")
        return {"ok": False, "fehler": "Der Auftrag konnte nicht gesendet werden."}, 503
    ziel = f"zeiterfassung {ze_id}" if ze_id is not None else f"user {param.get('user_id')}"
    _log_db(g.mu["name"], "korrektur", f"auftrag gesendet fuer {ziel} ({art})")
    log.info(f"korrektur: {g.mu['name']} -> {ziel} {art}, anfrage_id={anfrage_id}")
    return {"ok": True, "anfrage_id": anfrage_id}


GRUND_TEXT = {
    "rueckwirkend": "„gültig ab“ darf nicht in der Vergangenheit liegen.",
    "bereits_vorhanden": "Für diesen Mitarbeiter gibt es ab diesem Tag schon einen Eintrag.",
    "nr_vergeben": "Diese Nummer ist schon vergeben.",
    "kuerzel_vergeben": "Dieses Kürzel ist schon vergeben.",
    "frei_mit_soll": "Ein freier Tag hat kein Soll (0:00).",
    "abschneiden_ohne_sollbeginn": "„Vor Sollbeginn nicht anrechnen“ braucht einen Sollbeginn.",
    "kernzeit_ende_vor_beginn": "Das Kernzeit-Ende muss nach dem Sollbeginn liegen.",
    "pause_stufen": "Pause 2 muss später greifen als Pause 1 („ab“ größer).",
    "in_benutzung": "Wird noch benutzt und kann nicht deaktiviert werden.",
    "tagesplan_unbekannt": "Ein gewählter Tagesplan fehlt oder ist nicht aktiv.",
    "wochenplan_unbekannt": "Der Wochenplan fehlt oder ist nicht aktiv.",
    "monat_abgeschlossen": "Der Monat ist bereits abgeschlossen.",
    "objekt_unbekannt": "Den Eintrag gibt es nicht (mehr).",
    "bereits_aufgehoben": "Der Eintrag ist schon aufgehoben.",
    "art_unbekannt": "Die Abwesenheitsart fehlt oder ist nicht aktiv.",
    "keine_arbeitstage": "Im Zeitraum gibt es keinen Arbeitstag ohne diese Abwesenheit.",
    "zeitraum_nur_ganz": "Halbe Tage und Stunden nur für einen einzelnen Tag.",
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
    "netto_negativ": "Der Abzug ist größer als die Netto-Arbeitszeit.",
    "ohne_zeitregel": "Ohne Pause gibt es kein Netto – bitte zuerst eine Zeitregel zuordnen.",
    "regel_vorhanden": "Für diesen Tag gilt bereits eine Zeitregel.",
    "zeitregel_unbekannt": "Diese Zeitregel gehört nicht zum Mitarbeiter.",
    "bereits_markiert": "Der Datensatz ist schon zur Klärung markiert.",
    "nicht_markiert": "Der Datensatz ist nicht (mehr) zur Klärung markiert.",
    "notiz_fehlt": "Bitte eine Notiz angeben.",
    "ergebnis_fehlt": "Bitte das Ergebnis der Klärung angeben.",
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
            "zeitregel_id": r["zeitregel_id"], "objekt": r.get("objekt"),
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


@app.route("/zeit/monat")
def monat():
    """Monatsuebersicht (Umbau Schritt 2): ohne Mitarbeiter alle mit Wochenplan, mit Mitarbeiter je Tag."""
    j, m = _monat()
    heute = _heute()
    uid = _int("user")
    r = zeit_rechnung.Rechnung(db)
    ma = db.plan_mitarbeiter()
    vor = f"{j - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"
    nach = f"{j + (m == 12):04d}-{m % 12 + 1:02d}"
    ctx = {"monat": f"{j:04d}-{m:02d}", "monat_text": f"{zeit_rechnung.MONATE[m - 1]} {j}", "vor": vor,
           "nach": nach, "mitarbeiter_liste": ma, "uid": uid, "heute": heute, "seite": "monat",
           "hilfe": "Monatsuebersicht"}
    if uid:
        zeilen, summe = r.monat(uid, j, m, heute)
        ids = [x["id"] for x in ma]
        i = ids.index(uid) if uid in ids else None
        mu = db.eins(f"SELECT id, `user` AS name FROM {db.m}.`user` WHERE id = %s", (uid,))
        return render_template("monat.html", zeilen=zeilen, summe=summe, ma=mu,
                               vorheriger=ma[i - 1]["id"] if i else None,
                               naechster=ma[i + 1]["id"] if i is not None and i + 1 < len(ma) else None, **ctx)
    uebersicht = []
    for x in ma:
        _, summe = r.monat(x["id"], j, m, heute)
        uebersicht.append(dict(x, **summe))
    return render_template("monat.html", uebersicht=uebersicht, **ctx)


TAG_GRUENDE = ("Buchung vergessen", "Keyfob vergessen", "laut Mitarbeiter", "laut Krankmeldung",
               "laut Urlaubsantrag", "Terminal gestört", "Berufsschule", "Absprache mit Vorgesetztem")


@app.route("/zeit/tag")
def tag():
    """Tagesfenster (Umbau Schritt 3): alles zu einem Mitarbeiter und Tag an einer Stelle --
    Datensaetze korrigieren/anlegen, Abwesenheit, anderer Tagesplan."""
    uid = _int("user")
    heute = _heute()
    datum = _datum("datum", heute)
    mu = db.eins(f"SELECT id, `user` AS name FROM {db.m}.`user` WHERE id = %s", (uid,)) if uid else None
    if mu is None:
        return redirect(url_for("monat"))
    r = zeit_rechnung.Rechnung(db)
    zeilen, summe = r.monat(uid, datum.year, datum.month, heute)
    z = next(x for x in zeilen if x["datum"] == datum)
    naechster_pruefen = next((x["datum"] for x in zeilen if x["datum"] > datum and x["warnung"]), None)
    b, e = zeit_db_tag_grenzen(datum)
    saetze = db.datensaetze(uid, b, e)
    darf_korr = db.hat_recht(g.mu["id"], RECHT_KORREKTUR)

    def stand(t):
        return t.strftime("%Y-%m-%d %H:%M:%S") if t else ""
    for s_ in saetze:
        s_["stand_kommen"], s_["stand_gehen"] = stand(s_["kommen"]), stand(s_["gehen"])
        s_["art_korr"] = {"automatisch_beendet": "gehen_nachtragen", "abgeschlossen": "stempelzeit"}.get(s_["status"])
    return render_template(
        "tag.html", ma=mu, datum=datum, z=z, summe=summe, saetze=saetze,
        abwesenheiten=db.abwesenheiten_tag(uid, datum), abweichung=db.abweichung_tag(uid, datum),
        arten=[a for a in db.abwesenheitsarten() if a["aktiv"]],
        tagesplaene=[t for t in db.tagesplaene() if t["aktiv"]],
        urlaub=db.urlaub_rest(uid, datum.year), gruende=TAG_GRUENDE, heute=heute,
        vortag=datum - datetime.timedelta(days=1), folgetag=datum + datetime.timedelta(days=1),
        naechster_pruefen=naechster_pruefen, darf_korr=darf_korr,
        darf_stamm=db.hat_recht(g.mu["id"], RECHT_STAMMDATEN), darf_klaeren=db.hat_recht(g.mu["id"], RECHT_KLAEREN),
        seite="monat", hilfe="Tag")


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
    kopf = ["Mitarbeiter-ID", "Mitarbeiter", "Tage", "Datensaetze", "Brutto", "Pause", "Netto", "Netto vollstaendig",
            "Offen", "Ohne Zeitregel", "Automatisch beendet"]
    daten = [[s["user_id"], s["name"], s["tage"], s["datensaetze"], f_dauer(s["brutto"]), f_dauer(s["pause"]),
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
