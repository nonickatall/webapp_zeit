"""zeit_auth.py — Anmeldung der Zeiterfassungs-Verwaltung (webapp_zeit).

Eigene Zugaenge, unabhaengig vom Portal:
  * je Benutzer ein Zugang (Benutzername + Passwort, scrypt-Hash), fest genau
    EINER myhome.user.id zugeordnet -- ohne gueltige Zuordnung kein Zugang
  * angelegt wird nur auf dem Server per Kommandozeile (zeit_benutzer.py),
    das erste Passwort setzt der Benutzer ueber einen Einmal-Link
  * Sitzungen serverseitig in SQLite (gilt ueber alle gunicorn-Worker),
    Ablauf nach Leerlauf, kein "angemeldet bleiben"
  * Sperre nach zu vielen Fehlversuchen
Gespeichert wird nur der Hash der Sitzungs- und Einmal-Token, nie der Klartext.
"""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 64
MIN_PASSWORT = 12
TOKEN_GUELTIG_SEK = 24 * 3600

# Fuer unbekannte Benutzernamen wird trotzdem ein Hash gerechnet -> gleiche Laufzeit
_DUMMY_HASH = None


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def passwort_hash(passwort):
    salz = os.urandom(16)
    h = hashlib.scrypt(passwort.encode("utf-8"), salt=salz, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                       dklen=SCRYPT_LEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salz)}${_b64(h)}"


def passwort_pruefen(passwort, gespeichert):
    try:
        art, n, r, p, salz, soll = gespeichert.split("$")
        if art != "scrypt":
            return False
        ist = hashlib.scrypt(passwort.encode("utf-8"), salt=_unb64(salz), n=int(n), r=int(r), p=int(p),
                             dklen=len(_unb64(soll)), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(ist, _unb64(soll))
    except Exception:
        return False


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthDB:
    def __init__(self, pfad, leerlauf_sek, max_fehlversuche, sperre_sek):
        self.pfad = pfad
        self.leerlauf_sek = leerlauf_sek
        self.max_fehlversuche = max_fehlversuche
        self.sperre_sek = sperre_sek
        self._lokal = threading.local()   # Grund je Thread (gunicorn gthread)
        self._init()

    @property
    def letzter_grund(self):
        """Warum die letzte sitzung_pruefen() dieses Threads None lieferte (fuers Log)."""
        return getattr(self._lokal, "grund", "")

    @letzter_grund.setter
    def letzter_grund(self, wert):
        self._lokal.grund = wert

    def _conn(self):
        c = sqlite3.connect(self.pfad, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        verz = os.path.dirname(self.pfad)
        if verz:
            os.makedirs(verz, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS benutzer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                benutzername TEXT NOT NULL UNIQUE,
                myhome_user_id INTEGER NOT NULL UNIQUE,
                pw_hash TEXT,
                aktiv INTEGER NOT NULL DEFAULT 1,
                fehlversuche INTEGER NOT NULL DEFAULT 0,
                gesperrt_bis INTEGER NOT NULL DEFAULT 0,
                token_hash TEXT,
                token_ablauf INTEGER NOT NULL DEFAULT 0,
                erstellt INTEGER NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS sitzung (
                sid_hash TEXT PRIMARY KEY,
                benutzer_id INTEGER NOT NULL,
                erstellt INTEGER NOT NULL,
                letzte_aktivitaet INTEGER NOT NULL)""")

    # ---------------- Verwaltung (nur Kommandozeile) ----------------
    def anlegen(self, benutzername, myhome_user_id):
        token = secrets.token_urlsafe(32)
        jetzt = int(time.time())
        with self._conn() as c:
            c.execute("INSERT INTO benutzer (benutzername, myhome_user_id, token_hash, token_ablauf, erstellt) "
                      "VALUES (?, ?, ?, ?, ?)",
                      (benutzername, myhome_user_id, _token_hash(token), jetzt + TOKEN_GUELTIG_SEK, jetzt))
        return token

    def token_neu(self, benutzername):
        """Neuer Einmal-Link (Passwort vergessen); alte Sitzungen enden."""
        token = secrets.token_urlsafe(32)
        with self._conn() as c:
            cur = c.execute("UPDATE benutzer SET token_hash=?, token_ablauf=? WHERE benutzername=?",
                            (_token_hash(token), int(time.time()) + TOKEN_GUELTIG_SEK, benutzername))
            if cur.rowcount != 1:
                return None
            c.execute("DELETE FROM sitzung WHERE benutzer_id=(SELECT id FROM benutzer WHERE benutzername=?)",
                      (benutzername,))
        return token

    def aktiv_setzen(self, benutzername, aktiv):
        with self._conn() as c:
            cur = c.execute("UPDATE benutzer SET aktiv=? WHERE benutzername=?", (1 if aktiv else 0, benutzername))
            if not aktiv:
                c.execute("DELETE FROM sitzung WHERE benutzer_id=(SELECT id FROM benutzer WHERE benutzername=?)",
                          (benutzername,))
            return cur.rowcount == 1

    def liste(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, benutzername, myhome_user_id, aktiv, fehlversuche, gesperrt_bis, "
                "pw_hash IS NOT NULL AS passwort_gesetzt FROM benutzer ORDER BY benutzername")]

    # ---------------- Einmal-Link ----------------
    def token_benutzer(self, token):
        if not token:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM benutzer WHERE token_hash=? AND token_ablauf>? AND aktiv=1",
                          (_token_hash(token), int(time.time()))).fetchone()
        return dict(r) if r else None

    def passwort_setzen(self, token, passwort):
        b = self.token_benutzer(token)
        if b is None:
            return None
        with self._conn() as c:
            c.execute("UPDATE benutzer SET pw_hash=?, token_hash=NULL, token_ablauf=0, fehlversuche=0, "
                      "gesperrt_bis=0 WHERE id=?", (passwort_hash(passwort), b["id"]))
            c.execute("DELETE FROM sitzung WHERE benutzer_id=?", (b["id"],))
        return b

    # ---------------- Anmeldung ----------------
    def anmelden(self, benutzername, passwort):
        """Rueckgabe (benutzer|None, grund). grund: ok | unbekannt | inaktiv |
        gesperrt | passwort | jetzt_gesperrt | kein_passwort."""
        global _DUMMY_HASH
        jetzt = int(time.time())
        with self._conn() as c:
            r = c.execute("SELECT * FROM benutzer WHERE benutzername=?", (benutzername,)).fetchone()
        if r is None or not r["pw_hash"]:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = passwort_hash(secrets.token_hex(8))
            passwort_pruefen(passwort, _DUMMY_HASH)
            return None, ("unbekannt" if r is None else "kein_passwort")
        b = dict(r)
        if not b["aktiv"]:
            return None, "inaktiv"
        if b["gesperrt_bis"] > jetzt:
            return None, "gesperrt"
        if not passwort_pruefen(passwort, b["pw_hash"]):
            fv = b["fehlversuche"] + 1
            gesperrt_bis = jetzt + self.sperre_sek if fv >= self.max_fehlversuche else 0
            with self._conn() as c:
                c.execute("UPDATE benutzer SET fehlversuche=?, gesperrt_bis=? WHERE id=?",
                          (0 if gesperrt_bis else fv, gesperrt_bis, b["id"]))
            return None, ("jetzt_gesperrt" if gesperrt_bis else "passwort")
        with self._conn() as c:
            c.execute("UPDATE benutzer SET fehlversuche=0, gesperrt_bis=0 WHERE id=?", (b["id"],))
        return b, "ok"

    def sitzung_anlegen(self, benutzer_id):
        sid = secrets.token_urlsafe(32)
        jetzt = int(time.time())
        with self._conn() as c:
            c.execute("DELETE FROM sitzung WHERE letzte_aktivitaet < ?", (jetzt - self.leerlauf_sek,))
            c.execute("INSERT INTO sitzung (sid_hash, benutzer_id, erstellt, letzte_aktivitaet) VALUES (?,?,?,?)",
                      (_token_hash(sid), benutzer_id, jetzt, jetzt))
        return sid

    def sitzung_pruefen(self, sid):
        """Gueltige Sitzung -> Benutzer-Dict (Leerlauf wird zurueckgesetzt), sonst None."""
        if not sid:
            self.letzter_grund = "kein Cookie"
            return None
        jetzt = int(time.time())
        h = _token_hash(sid)
        with self._conn() as c:
            r = c.execute("SELECT b.*, s.letzte_aktivitaet FROM sitzung s JOIN benutzer b ON b.id = s.benutzer_id "
                          "WHERE s.sid_hash=?", (h,)).fetchone()
            if r is None:
                self.letzter_grund = "Sitzung unbekannt"
                return None
            if r["letzte_aktivitaet"] < jetzt - self.leerlauf_sek or not r["aktiv"]:
                self.letzter_grund = (f"Leerlauf {jetzt - r['letzte_aktivitaet']} s > {self.leerlauf_sek} s"
                                      if r["aktiv"] else "Zugang inaktiv")
                c.execute("DELETE FROM sitzung WHERE sid_hash=?", (h,))
                return None
            c.execute("UPDATE sitzung SET letzte_aktivitaet=? WHERE sid_hash=?", (jetzt, h))
        return dict(r)

    def sitzung_loeschen(self, sid):
        if sid:
            with self._conn() as c:
                c.execute("DELETE FROM sitzung WHERE sid_hash=?", (_token_hash(sid),))
