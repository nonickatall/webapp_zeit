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
    _env_laden(os.path.join(HIER, ".env"))
    cp = configparser.ConfigParser()
    datei = os.path.join(HIER, "webapp_zeit.conf")
    if not cp.read(datei, encoding="utf-8"):
        raise RuntimeError(f"Konfiguration fehlt: {datei}")
    s = cp["webapp_zeit"]
    user, passwd = os.environ.get("ZEITWEB_SQL_USER", ""), os.environ.get("ZEITWEB_SQL_PASSWD", "")
    if not user or not passwd:
        raise RuntimeError("ZEITWEB_SQL_USER / ZEITWEB_SQL_PASSWD fehlen in der .env")
    return {
        "sql_address": s["sql_address"],          # exakt der Host des MySQL-Users, nie localhost
        "sql_db_zeit": s.get("sql_db_zeit", "myhome_zeit"),
        "sql_db_myhome": s.get("sql_db_myhome", "myhome"),
        "sql_user": user,
        "sql_passwd": passwd,
        "auth_db": s["auth_db"],
        "leerlauf_sek": int(s.get("leerlauf_minuten", "15")) * 60,
        "max_fehlversuche": int(s.get("max_fehlversuche", "5")),
        "sperre_sek": int(s.get("sperre_minuten", "15")) * 60,
        "log_datei": s.get("log_datei", "webapp_zeit.log"),
        "herkunft": s.get("herkunft", "webapp_zeit"),
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

    def _conn(self):
        return pymysql.connect(host=self.cfg["sql_address"], user=self.cfg["sql_user"],
                               password=self.cfg["sql_passwd"], database=self.z,
                               charset="utf8mb4", autocommit=True, connect_timeout=5,
                               cursorclass=pymysql.cursors.DictCursor)

    def abfrage(self, sql, args=()):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()
        finally:
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
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self.m}.logdaten (herkunft, berechtigter, nummer, anforderung, aktion, parameter) "
                    f"VALUES (%s, %s, %s, %s, %s, %s)",
                    (self.cfg["herkunft"], (berechtigter or "")[:100], "", (anforderung or "")[:100],
                     (aktion or "")[:200], (parameter or "")[:100]))
        finally:
            conn.close()

    def mitarbeiter(self):
        """Auswahlliste: alle User mit Zeitdatensaetzen oder Zeitregeln (auch inaktive)."""
        return self.abfrage(
            f"SELECT u.id, u.`user` AS name, u.status FROM {self.m}.`user` u "
            f"WHERE u.id <> 0 AND (u.id IN (SELECT user_id FROM {self.z}.zeiterfassung) "
            f"OR u.id IN (SELECT user_id FROM {self.z}.user_zeitregel)) ORDER BY u.`user`")

    # ---------------- 1. Anwesenheit
    def anwesenheit(self):
        jetzt = self.jetzt_utc()
        heute = ortszeit(jetzt).date()
        beginn, ende = tag_grenzen_utc(heute)
        offen = self.abfrage(
            f"SELECT ze.id, ze.user_id, ze.kommen, u.`user` AS name, bk.terminal "
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
            r["laufend_min"] = minuten_zwischen(r["kommen"], jetzt)
            r["heute_min"] = summe.get(r["user_id"], 0) + minuten_zwischen(max(r["kommen"], beginn), jetzt)
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
            f"zr.pause_minuten AS regel_pause, zr.pause_ab_minuten AS regel_pause_ab, zr.gueltig_ab AS regel_ab "
            f"FROM {self.z}.zeiterfassung ze "
            f"LEFT JOIN {self.m}.`user` u ON u.id = ze.user_id "
            f"LEFT JOIN {self.z}.user_zeitregel zr ON zr.id = ze.zeitregel_id "
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
            "automatisch_beendet": self.abfrage(basis + "ze.status = 'automatisch_beendet' ORDER BY ze.kommen"),
            "ohne_zeitregel": self.abfrage(basis + "ze.status = 'abgeschlossen' AND ze.pause_minuten IS NULL "
                                                   "ORDER BY ze.kommen"),
            "offen_alt": self.abfrage(basis + "ze.status = 'offen' AND ze.kommen < %s ORDER BY ze.kommen",
                                      (heute_beginn,)),
        }
        # Klaerungen (Plan 5.4a) kommen erst mit Stufe B; ihr Schema steht noch nicht
        # fest -> nur anzeigen, OB die Tabelle existiert, keine Spalten voraussetzen.
        faelle["klaerung_tabelle"] = self.eins(
            "SELECT 1 AS ok FROM information_schema.tables WHERE table_schema = %s "
            "AND table_name = 'zeiterfassung_klaerung'", (self.z,)) is not None
        return faelle

    # ---------------- 5. Zeitregeln
    def zeitregeln(self, uid):
        sql = (f"SELECT zr.*, u.`user` AS name, e.`user` AS ersteller FROM {self.z}.user_zeitregel zr "
               f"LEFT JOIN {self.m}.`user` u ON u.id = zr.user_id "
               f"LEFT JOIN {self.m}.`user` e ON e.id = zr.erstellt_von ")
        if uid:
            return self.abfrage(sql + "WHERE zr.user_id = %s ORDER BY zr.gueltig_ab DESC", (uid,))
        return self.abfrage(sql + "ORDER BY u.`user`, zr.gueltig_ab DESC")

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
