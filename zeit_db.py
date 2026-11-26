"""zeit_db.py — Konfiguration und Lesezugriffe der Zeiterfassungs-Verwaltung.

Die Webapp schreibt NIE in myhome_zeit. Ihr MySQL-User darf dort nur lesen;
geschrieben wird nur myhome.logdaten (Anmeldungen, Fehlversuche).
Alle Zeiten liegen in UTC in der DB; angezeigt wird Ortszeit (Europe/Berlin).
"jetzt" kommt vom DB-Server (UTC_TIMESTAMP()). Klarnamen per Join aus myhome.user.
SQL ausschliesslich parametrisiert.
"""
import configparser
import datetime
import os
import threading
from zoneinfo import ZoneInfo

import pymysql
import pymysql.cursors

ZONE = ZoneInfo("Europe/Berlin")
UTC = datetime.timezone.utc
HIER = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- Konfiguration
def _env_laden(pfad):
    """.env ohne Zusatzpaket lesen; vorhandene Umgebungswerte (systemd) gewinnen."""
    if not os.path.exists(pfad):
        return
    for zeile in open(pfad, encoding="utf-8"):
        zeile = zeile.strip()
        if zeile and not zeile.startswith("#") and "=" in zeile:
            k, v = zeile.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def lade_konfig():
    """webapp_zeit.conf (Abschnitte [webapp_zeit], [datenbank], [anmeldung], [mqtt]) + .env.

    Konvention wie bei den myhome-Diensten: Adressen und Benutzernamen in der conf,
    Passwoerter NUR in der .env (ZEITWEB_SQL_PASSWD, ZEITWEB_MQTT_PASSWD).
    Uebergang: Fehlt ein Wert in seinem Abschnitt, gilt der gleichnamige Schluessel in
    [webapp_zeit] (alte, flache conf); Benutzernamen notfalls noch aus der .env
    (ZEITWEB_SQL_USER / ZEITWEB_MQTT_USER).
    """
    _env_laden(os.path.join(HIER, ".env"))
    cp = configparser.ConfigParser()
    datei = os.path.join(HIER, "webapp_zeit.conf")
    if not cp.read(datei, encoding="utf-8"):
        raise RuntimeError(f"Konfiguration fehlt: {datei}")

    def wert(abschnitt, schluessel, vorgabe=None):
        for a in (abschnitt, "webapp_zeit"):
            if cp.has_option(a, schluessel) and cp.get(a, schluessel).strip():
                return cp.get(a, schluessel).strip()
        if vorgabe is None:
            raise RuntimeError(f"{datei}: [{abschnitt}] {schluessel} fehlt")
        return vorgabe

    sql_user = wert("datenbank", "sql_user", os.environ.get("ZEITWEB_SQL_USER", ""))
    sql_passwd = os.environ.get("ZEITWEB_SQL_PASSWD", "")
    if not sql_user or not sql_passwd:
        raise RuntimeError("[datenbank] sql_user in der conf bzw. ZEITWEB_SQL_PASSWD in der .env fehlt")
    return {
        "sql_address": wert("datenbank", "sql_address"),     # exakt der Host des MySQL-Users, nie localhost
        "sql_db_zeit": wert("datenbank", "sql_db_zeit", "myhome_zeit"),
        "sql_db_myhome": wert("datenbank", "sql_db_myhome", "myhome"),
        "sql_user": sql_user,
        "sql_passwd": sql_passwd,
        "auth_db": wert("anmeldung", "auth_db"),
        "leerlauf_sek": int(wert("anmeldung", "leerlauf_minuten", "15")) * 60,
        "max_fehlversuche": int(wert("anmeldung", "max_fehlversuche", "5")),
        "sperre_sek": int(wert("anmeldung", "sperre_minuten", "15")) * 60,
        "log_datei": wert("webapp_zeit", "log_datei", "webapp_zeit.log"),
        "herkunft": wert("webapp_zeit", "herkunft", "webapp_zeit"),
        "bundesland": wert("webapp_zeit", "bundesland", "RP"),   # Vorgabe fuer Feiertage
        # Auftraege an die Engine (Stufe B): ohne Host/User sind nur Lesefunktionen aktiv
        "mqtt_host": wert("mqtt", "mqtt_host", ""),
        "mqtt_port": int(wert("mqtt", "mqtt_port", "1883")),
        "eingang_topic": wert("mqtt", "eingang_topic", "/myhome/interner_mqtt_eingang"),
        "mqtt_user": wert("mqtt", "mqtt_user", os.environ.get("ZEITWEB_MQTT_USER", "")),
        "mqtt_passwd": os.environ.get("ZEITWEB_MQTT_PASSWD", ""),
    }


# ---------------------------------------------------------------- Zeit
def ortszeit(utc_naiv):
    return utc_naiv.replace(tzinfo=UTC).astimezone(ZONE) if utc_naiv else None


def tag_grenzen_utc(tag):
    """Kalendertag (date, Ortszeit) -> [beginn, ende) als naive UTC."""
    b = datetime.datetime.combine(tag, datetime.time(0, 0), ZONE)
    e = datetime.datetime.combine(tag + datetime.timedelta(days=1), datetime.time(0, 0), ZONE)
    return b.astimezone(UTC).replace(tzinfo=None), e.astimezone(UTC).replace(tzinfo=None)


def zeitraum_utc(von, bis):
    """Ortszeit-Tage von..bis (inklusive) -> [beginn, ende) in UTC."""
    return tag_grenzen_utc(von)[0], tag_grenzen_utc(bis)[1]


def minuten_zwischen(a, b):
    return max(0, int((b - a).total_seconds() // 60))


# ---------------------------------------------------------------- DB
class ZeitDB:
    def __init__(self, cfg):
        self.cfg = cfg
        self.z = cfg["sql_db_zeit"]
        self.m = cfg["sql_db_myhome"]
        self._lokal = threading.local()

    # Eine Verbindung je Seitenaufruf (Webapp: sitzung_beginnen/sitzung_ende um jeden Request).
    # Ohne Sitzung (Kommandozeilen-Werkzeuge) wie bisher eine Verbindung je Abfrage.
    # Verbindungsaufbau kostet ein Vielfaches einer Abfrage -- die Monatsuebersicht stellt
    # pro Mitarbeiter rund ein Dutzend Abfragen.
    def sitzung_beginnen(self):
        self.sitzung_ende()
        self._lokal.conn = None
        self._lokal.aktiv = True

    def sitzung_ende(self):
        c = getattr(self._lokal, "conn", None)
        self._lokal.conn, self._lokal.aktiv = None, False
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _ausleihen(self):
        """(conn, selbst_schliessen)"""
        if not getattr(self._lokal, "aktiv", False):
            return self._conn(), True
        if self._lokal.conn is None:
            self._lokal.conn = self._conn()
        return self._lokal.conn, False

    def _conn(self):
        return pymysql.connect(host=self.cfg["sql_address"], user=self.cfg["sql_user"],
                               password=self.cfg["sql_passwd"], database=self.z,
                               charset="utf8mb4", autocommit=True, connect_timeout=5,
                               cursorclass=pymysql.cursors.DictCursor)

    def abfrage(self, sql, args=()):
        conn, schliessen = self._ausleihen()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()
        except Exception:
            if not schliessen:          # kaputte Sitzungsverbindung nicht weiterverwenden
                self._lokal.conn = None
                schliessen = True
            raise
        finally:
            if schliessen:
                conn.close()

    def eins(self, sql, args=()):
        r = self.abfrage(sql, args)
        return r[0] if r else None

    def jetzt_utc(self):
        return self.eins("SELECT UTC_TIMESTAMP() AS j")["j"]

    # ---------------- Benutzer / Rechte
    def myhome_user(self, uid):
        """Genau ein aktiver myhome.user (nie id 0), sonst None."""
        if not uid:
            return None
        return self.eins(f"SELECT id, `user` AS name, nummer, status FROM {self.m}.`user` "
                         f"WHERE id = %s AND id <> 0 AND status = 1", (uid,))

    def hat_recht(self, uid, befehl):
        """Anzeige-Recht ueber die rechte-Tabelle der Engine (unabhaengig vom aktiven
        Schema). Verbindlich fuer Aenderungen bleibt die Engine (execute)."""
        return self.eins(
            f"SELECT 1 AS ok FROM {self.m}.rechte r JOIN {self.m}.befehle b ON b.id = r.id_befehl "
            f"JOIN {self.m}.`user` u ON u.id = r.id_user "
            f"WHERE b.befehl = %s AND r.id_user = %s AND r.erteilt = 1 AND u.status = 1 AND u.id <> 0",
            (befehl, uid)) is not None

    def logdaten(self, berechtigter, anforderung, aktion, parameter):
        conn = self._conn()     # eigene Verbindung: schreibt auch, wenn die Sitzungsverbindung gestoert ist
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self.m}.logdaten (herkunft, berechtigter, nummer, anforderung, aktion, parameter) "
                    f"VALUES (%s, %s, %s, %s, %s, %s)",
                    (self.cfg["herkunft"], (berechtigter or "")[:100], "", (anforderung or "")[:100],
                     (aktion or "")[:200], (parameter or "")[:100]))
        finally:
            conn.close()

    def aktive_user(self):
        """Auswahl fuer neue Zeitregeln: aktive myhome.user mit echter Rufnummer
        (nie id 0, keine technischen User wie +Jobserver)."""
        return self.abfrage(
            f"SELECT id, `user` AS name, status FROM {self.m}.`user` "
            f"WHERE id <> 0 AND status = 1 AND nummer REGEXP '^[+][0-9]+$' ORDER BY `user`")

    def auftrag(self, anfrage_id, bearbeiter_id):
        """Ergebnis eines eigenen Verwaltungsauftrags (sonst None)."""
        return self.eins(f"SELECT * FROM {self.z}.zeiterfassung_auftrag "
                         f"WHERE anfrage_id = %s AND bearbeiter_id = %s", (anfrage_id, bearbeiter_id))

    def mitarbeiter(self):
        """Auswahlliste: alle User mit Zeitdatensaetzen oder Zeitregeln (auch inaktive)."""
        return self.abfrage(
            f"SELECT u.id, u.`user` AS name, u.status FROM {self.m}.`user` u "
            f"WHERE u.id <> 0 AND (u.id IN (SELECT user_id FROM {self.z}.zeiterfassung) "
            f"OR u.id IN (SELECT user_id FROM {self.z}.user_zeitregel)) ORDER BY u.`user`")

    # ---------------- Hilfe
    def hilfe(self, ueberschrift):
        """Hilfetext einer Seite aus myhome_zeit.hilfe (None, wenn es keinen gibt)."""
        return self.eins(f"SELECT ueberschrift, text FROM {self.z}.hilfe WHERE ueberschrift = %s",
                         (ueberschrift,))

    # ---------------- 1. Anwesenheit
    def anwesenheit(self):
        jetzt = self.jetzt_utc()
        heute = ortszeit(jetzt).date()
        beginn, ende = tag_grenzen_utc(heute)
        offen = self.abfrage(
            f"SELECT ze.id, ze.user_id, ze.kommen, ze.zaehlt_ab, u.`user` AS name, bk.terminal "
            f"FROM {self.z}.zeiterfassung ze "
            f"LEFT JOIN {self.z}.zeiterfassung_buchung bk ON bk.id = ze.buchung_kommen_id "
            f"LEFT JOIN {self.m}.`user` u ON u.id = ze.user_id "
            f"WHERE ze.status = 'offen' ORDER BY u.`user`")
        heute_zeilen = self.abfrage(
            f"SELECT user_id, minuten FROM {self.z}.zeiterfassung "
            f"WHERE status = 'abgeschlossen' AND kommen >= %s AND kommen < %s", (beginn, ende))
        summe = {}
        for r in heute_zeilen:
            summe[r["user_id"]] = summe.get(r["user_id"], 0) + int(r["minuten"] or 0)
        for r in offen:
            # angerechnet: vor dem Sollbeginn der Zeitregel (zaehlt_ab) zaehlt nichts
            start = r["zaehlt_ab"] or r["kommen"]
            r["laufend_min"] = minuten_zwischen(start, jetzt)
            r["heute_min"] = summe.get(r["user_id"], 0) + minuten_zwischen(max(start, beginn), jetzt)
            r["vor_heute"] = r["kommen"] < beginn
        return {"jetzt": jetzt, "heute": heute, "offen": offen}

    # ---------------- 2. Datensaetze
    def datensaetze(self, uid, von_utc, bis_utc):
        sql = (f"SELECT ze.*, u.`user` AS name, bk.terminal AS terminal_kommen, bg.terminal AS terminal_gehen, "
               f"(ze.buchung_kommen_id IS NULL) AS manuell "
               f"FROM {self.z}.zeiterfassung ze "
               f"LEFT JOIN {self.z}.zeiterfassung_buchung bk ON bk.id = ze.buchung_kommen_id "
               f"LEFT JOIN {self.z}.zeiterfassung_buchung bg ON bg.id = ze.buchung_gehen_id "
               f"LEFT JOIN {self.m}.`user` u ON u.id = ze.user_id "
               f"WHERE ze.kommen >= %s AND ze.kommen < %s")
        args = [von_utc, bis_utc]
        if uid:
            sql += " AND ze.user_id = %s"
            args.append(uid)
        return self.abfrage(sql + " ORDER BY u.`user`, ze.kommen", args)

    # ---------------- 3. Detail
    def datensatz(self, ze_id):
        ze = self.eins(
            f"SELECT ze.*, u.`user` AS name, (ze.buchung_kommen_id IS NULL) AS manuell, "
            f"zr.pause_minuten AS regel_pause, zr.pause_ab_minuten AS regel_pause_ab, zr.gueltig_ab AS regel_ab, "
            f"TIME_FORMAT(zr.sollbeginn, '%%H:%%i') AS regel_soll, ra.begruendung AS regel_begruendung, "
            f"tp.nr AS tp_nr, tp.name AS tp_name, tp.pause1_minuten AS tp_p1, tp.pause1_ab_minuten AS tp_ab1, "
            f"tp.pause2_minuten AS tp_p2, tp.pause2_ab_minuten AS tp_ab2 "
            f"FROM {self.z}.zeiterfassung ze "
            f"LEFT JOIN {self.m}.`user` u ON u.id = ze.user_id "
            f"LEFT JOIN {self.z}.tagesplan tp ON tp.id = ze.tagesplan_id "
            f"LEFT JOIN {self.z}.user_zeitregel zr ON zr.id = ze.zeitregel_id "
            f"LEFT JOIN {self.z}.zeiterfassung_auftrag ra ON ra.zeitregel_id = zr.id AND ra.art = 'zeitregel' "
            f"AND ra.ergebnis = 'ausgefuehrt' "
            f"WHERE ze.id = %s", (ze_id,))
        if ze is None:
            return None
        ids = [i for i in (ze["buchung_kommen_id"], ze["buchung_gehen_id"]) if i]
        buchungen, anfragen = [], []
        if ids:
            ph = ", ".join(["%s"] * len(ids))
            buchungen = self.abfrage(f"SELECT * FROM {self.z}.zeiterfassung_buchung WHERE id IN ({ph}) ORDER BY id", ids)
            anfragen = self.abfrage(
                f"SELECT * FROM {self.z}.zeiterfassung_anfrage WHERE buchung_id IN ({ph}) "
                f"OR anfrage_id IN (SELECT anfrage_id FROM {self.z}.zeiterfassung_buchung WHERE id IN ({ph})) "
                f"ORDER BY eingegangen", ids + ids)
        aenderungen = self.abfrage(
            f"SELECT a.*, u.`user` AS bearbeiter FROM {self.z}.zeiterfassung_aenderung a "
            f"LEFT JOIN {self.m}.`user` u ON u.id = a.geaendert_von "
            f"WHERE a.zeiterfassung_id = %s ORDER BY a.geaendert_am, a.id", (ze_id,))
        return {"ze": ze, "buchungen": buchungen, "anfragen": anfragen, "aenderungen": aenderungen}

    # ---------------- 4. Offene Faelle
    def offene_faelle(self):
        jetzt = self.jetzt_utc()
        heute_beginn = tag_grenzen_utc(ortszeit(jetzt).date())[0]
        basis = (f"SELECT ze.*, u.`user` AS name FROM {self.z}.zeiterfassung ze "
                 f"LEFT JOIN {self.m}.`user` u ON u.id = ze.user_id WHERE ")
        faelle = {
            "zu_klaeren": self.abfrage(basis + "ze.klaeren = 1 ORDER BY ze.kommen"),
            "automatisch_beendet": self.abfrage(basis + "ze.status = 'automatisch_beendet' ORDER BY ze.kommen"),
            "ohne_zeitregel": self.abfrage(basis + "ze.status = 'abgeschlossen' AND ze.pause_minuten IS NULL "
                                                   "ORDER BY ze.kommen"),
            "offen_alt": self.abfrage(basis + "ze.status = 'offen' AND ze.kommen < %s ORDER BY ze.kommen",
                                      (heute_beginn,)),
        }
        return faelle

    # ---------------- 5. Zeitregeln
    def zeitregeln(self, uid):
        # Begruendung steht nicht an der Regel, sondern am Auftrag, der sie angelegt hat
        sql = (f"SELECT zr.*, TIME_FORMAT(zr.sollbeginn, '%%H:%%i') AS sollbeginn_hhmm, "
               f"u.`user` AS name, e.`user` AS ersteller, a.begruendung FROM {self.z}.user_zeitregel zr "
               f"LEFT JOIN {self.m}.`user` u ON u.id = zr.user_id "
               f"LEFT JOIN {self.m}.`user` e ON e.id = zr.erstellt_von "
               f"LEFT JOIN {self.z}.zeiterfassung_auftrag a ON a.zeitregel_id = zr.id AND a.art = 'zeitregel' "
               f"AND a.ergebnis = 'ausgefuehrt' ")
        if uid:
            return self.abfrage(sql + "WHERE zr.user_id = %s ORDER BY zr.gueltig_ab DESC", (uid,))
        return self.abfrage(sql + "ORDER BY u.`user`, zr.gueltig_ab DESC")

    # ---------------- 7. Stammdaten (Umbau Schritt 1)
    def tagesplaene(self):
        return self.abfrage(
            f"SELECT tp.*, TIME_FORMAT(tp.sollbeginn, '%%H:%%i') AS sollbeginn_hhmm, "
            f"TIME_FORMAT(tp.kernzeit_ende, '%%H:%%i') AS kernzeit_ende_hhmm "
            f"FROM {self.z}.tagesplan tp ORDER BY tp.nr")

    def wochenplaene(self):
        return self.abfrage(f"SELECT * FROM {self.z}.wochenplan ORDER BY nr")

    def zuordnungen(self, uid=None):
        sql = (f"SELECT z.*, u.`user` AS name, w.nr AS wp_nr, w.name AS wp_name, e.`user` AS ersteller, "
               f"a.`user` AS aufheber FROM {self.z}.user_wochenplan z "
               f"LEFT JOIN {self.m}.`user` u ON u.id = z.user_id "
               f"LEFT JOIN {self.z}.wochenplan w ON w.id = z.wochenplan_id "
               f"LEFT JOIN {self.m}.`user` e ON e.id = z.erstellt_von "
               f"LEFT JOIN {self.m}.`user` a ON a.id = z.aufgehoben_von ")
        if uid:
            return self.abfrage(sql + "WHERE z.user_id = %s ORDER BY z.gueltig_ab DESC, z.id DESC", (uid,))
        return self.abfrage(sql + "ORDER BY u.`user`, z.gueltig_ab DESC, z.id DESC")

    def abwesenheitsarten(self):
        return self.abfrage(f"SELECT * FROM {self.z}.abwesenheitsart ORDER BY reihenfolge, kuerzel")

    def feiertage(self, jahr):
        return self.abfrage(f"SELECT * FROM {self.z}.feiertag WHERE YEAR(datum) = %s ORDER BY datum", (jahr,))

    def salden_start(self):
        return self.abfrage(
            f"SELECT s.*, u.`user` AS name, e.`user` AS ersteller FROM {self.z}.saldo_start s "
            f"LEFT JOIN {self.m}.`user` u ON u.id = s.user_id "
            f"LEFT JOIN {self.m}.`user` e ON e.id = s.erstellt_von ORDER BY u.`user`, s.stichtag DESC")

    def urlaubskonten(self, jahr):
        return self.abfrage(
            f"SELECT k.*, u.`user` AS name FROM {self.z}.urlaubskonto k "
            f"LEFT JOIN {self.m}.`user` u ON u.id = k.user_id WHERE k.jahr = %s ORDER BY u.`user`", (jahr,))

    def stammdaten_spur(self, tabellen, anzahl=30):
        platz = ", ".join(["%s"] * len(tabellen))
        return self.abfrage(
            f"SELECT s.*, u.`user` AS bearbeiter FROM {self.z}.stammdaten_aenderung s "
            f"LEFT JOIN {self.m}.`user` u ON u.id = s.geaendert_von "
            f"WHERE s.tabelle IN ({platz}) ORDER BY s.id DESC LIMIT %s", (*tabellen, anzahl))

    def abwesenheiten_tag(self, uid, datum):
        return self.abfrage(
            f"SELECT a.*, t.kuerzel, t.name AS art_name, t.farbe, e.`user` AS ersteller FROM {self.z}.abwesenheit a "
            f"JOIN {self.z}.abwesenheitsart t ON t.id = a.art_id LEFT JOIN {self.m}.`user` e ON e.id = a.erstellt_von "
            f"WHERE a.user_id = %s AND a.datum = %s AND a.aufgehoben_am IS NULL ORDER BY a.id", (uid, datum))

    def abweichung_tag(self, uid, datum):
        return self.eins(
            f"SELECT a.*, tp.nr, tp.name, tp.farbe FROM {self.z}.tag_abweichung a "
            f"JOIN {self.z}.tagesplan tp ON tp.id = a.tagesplan_id "
            f"WHERE a.user_id = %s AND a.datum = %s AND a.aufgehoben_am IS NULL", (uid, datum))

    def urlaub_rest(self, uid, jahr):
        """Urlaubskonto eines Jahres: Anspruch, Uebertrag, genommen (ganz 1, halb 0,5), Rest. None ohne Konto."""
        k = self.eins(f"SELECT anspruch_tage, uebertrag_tage FROM {self.z}.urlaubskonto WHERE user_id = %s "
                      f"AND jahr = %s", (uid, jahr))
        g = self.eins(
            f"SELECT COALESCE(SUM(CASE a.umfang WHEN 'ganz' THEN 1 WHEN 'halb' THEN 0.5 ELSE 0 END), 0) AS tage "
            f"FROM {self.z}.abwesenheit a JOIN {self.z}.abwesenheitsart t ON t.id = a.art_id "
            f"WHERE a.user_id = %s AND YEAR(a.datum) = %s AND a.aufgehoben_am IS NULL AND t.urlaub = 1", (uid, jahr))
        genommen = float(g["tage"] or 0)
        if k is None:
            return {"konto": False, "genommen": genommen}
        gesamt = float(k["anspruch_tage"]) + float(k["uebertrag_tage"])
        return {"konto": True, "anspruch": float(k["anspruch_tage"]), "uebertrag": float(k["uebertrag_tage"]),
                "gesamt": gesamt, "genommen": genommen, "rest": gesamt - genommen}

    def plan_mitarbeiter(self):
        """Mitarbeiter mit (nicht aufgehobener) Wochenplan-Zuordnung -- Auswahl der Monatsuebersicht."""
        return self.abfrage(
            f"SELECT DISTINCT u.id, u.`user` AS name, u.status FROM {self.z}.user_wochenplan z "
            f"JOIN {self.m}.`user` u ON u.id = z.user_id WHERE z.aufgehoben_am IS NULL ORDER BY u.`user`")

    # ---------------- 6. Monatssummen
    def summen(self, jahr, monat):
        von = datetime.date(jahr, monat, 1)
        bis = (datetime.date(jahr + (monat == 12), monat % 12 + 1, 1) - datetime.timedelta(days=1))
        b, e = zeitraum_utc(von, bis)
        zeilen = self.datensaetze(None, b, e)
        erg = {}
        for r in zeilen:
            s = erg.setdefault(r["user_id"], {"user_id": r["user_id"], "name": r["name"], "tage": set(),
                                               "brutto": 0, "pause": 0, "netto": 0, "datensaetze": 0,
                                               "offen": 0, "ohne_regel": 0, "auto": 0})
            s["datensaetze"] += 1
            s["tage"].add(ortszeit(r["kommen"]).date())
            if r["status"] == "abgeschlossen":
                s["brutto"] += int(r["minuten"] or 0)
                if r["pause_minuten"] is None:
                    s["ohne_regel"] += 1
                else:
                    s["pause"] += int(r["pause_minuten"])
                if r.get("netto_minuten") is not None:
                    s["netto"] += int(r["netto_minuten"])
            elif r["status"] == "offen":
                s["offen"] += 1
            else:
                s["auto"] += 1
        for s in erg.values():
            s["tage"] = len(s["tage"])
            s["offene_faelle"] = s["offen"] + s["ohne_regel"] + s["auto"]
            # Netto nur belastbar, wenn kein Datensatz ohne Pause/abgeschlossen fehlt
            s["netto_vollstaendig"] = s["offene_faelle"] == 0
        return sorted(erg.values(), key=lambda s: s["name"] or ""), von, bis
