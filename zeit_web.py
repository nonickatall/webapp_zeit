"""zeit_web.py — Verwaltung der Zeiterfassung (webapp_zeit), Stufe A: nur lesen.

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

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for
from jinja2 import Undefined

from zeit_auth import MIN_PASSWORT, AuthDB
from zeit_db import ZeitDB, lade_konfig, ortszeit, zeitraum_utc

cfg = lade_konfig()
logging.basicConfig(filename=cfg["log_datei"], level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webapp_zeit")

db = ZeitDB(cfg)
auth = AuthDB(cfg["auth_db"], cfg["leerlauf_sek"], cfg["max_fehlversuche"], cfg["sperre_sek"])

app = Flask(__name__)
COOKIE = "zeit_session"
RECHT_LESEN = "zef_lesen"
OEFFENTLICH = {"login", "passwort", "static"}


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
    b = auth.sitzung_pruefen(request.cookies.get(COOKIE))
    if b is None:
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
                           leerlauf_min=cfg["leerlauf_sek"] // 60, oeffentlich=True)


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
    return render_template("anwesenheit.html", a=db.anwesenheit(), seite="anwesenheit")


def _datensaetze_param():
    heute = _heute()
    von = _datum("von", heute.replace(day=1))
    bis = _datum("bis", heute)
    if bis < von:
        von, bis = bis, von
    return _int("user"), von, bis


@app.route("/zeit/datensaetze")
def datensaetze():
    uid, von, bis = _datensaetze_param()
    zeilen = db.datensaetze(uid, *zeitraum_utc(von, bis))
    return render_template("datensaetze.html", zeilen=zeilen, uid=uid, von=von, bis=bis,
                           mitarbeiter=db.mitarbeiter(), seite="datensaetze")


@app.route("/zeit/datensaetze.csv")
def datensaetze_csv():
    uid, von, bis = _datensaetze_param()
    zeilen = db.datensaetze(uid, *zeitraum_utc(von, bis))
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
    return render_template("datensatz.html", d=d, seite="datensaetze")


@app.route("/zeit/offen")
def offen():
    return render_template("offen.html", f=db.offene_faelle(), seite="offen")


@app.route("/zeit/zeitregeln")
def zeitregeln():
    uid = _int("user")
    return render_template("zeitregeln.html", regeln=db.zeitregeln(uid), uid=uid,
                           mitarbeiter=db.mitarbeiter(), seite="zeitregeln")


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
                           seite="summen")


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
