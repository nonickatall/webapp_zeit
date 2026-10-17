"""zeit_selbsttest.py — fuehrt jede Leseabfrage der Webapp einmal gegen die echte DB aus
(mit den Rechten des webapp_zeit-Users) und zeigt Ergebnis bzw. Fehler. Schreibt nur einen
logdaten-Eintrag "selbsttest".

    cd /etc/webapp_zeit && venv/bin/python zeit_selbsttest.py [myhome_user_id]
"""
import datetime
import sys
import traceback

from zeit_auth import AuthDB
from zeit_db import ZeitDB, lade_konfig, zeitraum_utc


def pruef(name, f):
    try:
        erg = f()
        n = len(erg) if hasattr(erg, "__len__") else erg
        print(f"OK     {name:28} -> {n if not isinstance(erg, dict) else {k: (len(v) if isinstance(v, list) else v) for k, v in erg.items()}}")
        return erg
    except Exception as e:
        print(f"FEHLER {name:28} -> {e}")
        traceback.print_exc(limit=1)
        return None


def main():
    cfg = lade_konfig()
    db = ZeitDB(cfg)
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print("Konfiguration:", {k: v for k, v in cfg.items() if k != "sql_passwd"})
    pruef("jetzt_utc", lambda: [db.jetzt_utc()])
    pruef(f"myhome_user({uid})", lambda: [db.myhome_user(uid)])
    pruef(f"hat_recht({uid}, zef_lesen)", lambda: [db.hat_recht(uid, "zef_lesen")])
    pruef("mitarbeiter", db.mitarbeiter)
    pruef("anwesenheit", db.anwesenheit)
    heute = datetime.date.today()
    zeilen = pruef("datensaetze (Monat, alle)", lambda: db.datensaetze(None, *zeitraum_utc(heute.replace(day=1), heute)))
    pruef(f"datensaetze (Monat, {uid})", lambda: db.datensaetze(uid, *zeitraum_utc(heute.replace(day=1), heute)))
    if zeilen:
        pruef(f"datensatz({zeilen[0]['id']})", lambda: db.datensatz(zeilen[0]["id"]))
    pruef("offene_faelle", db.offene_faelle)
    pruef("zeitregeln (alle)", lambda: db.zeitregeln(None))
    pruef("summen (Monat)", lambda: db.summen(heute.year, heute.month)[0])
    pruef("logdaten INSERT", lambda: [db.logdaten("selbsttest", "selbsttest", "selbsttest", "")])
    pruef("auth.db", lambda: AuthDB(cfg["auth_db"], cfg["leerlauf_sek"], cfg["max_fehlversuche"],
                                    cfg["sperre_sek"]).liste())
    # Gegenprobe: darf NICHT schreiben koennen
    try:
        db.abfrage(f"UPDATE {cfg['sql_db_zeit']}.zeiterfassung SET korrigiert = korrigiert WHERE id = -1")
        print("FEHLER Schreibschutz myhome_zeit       -> UPDATE wurde ERLAUBT")
    except Exception as e:
        print(f"OK     Schreibschutz myhome_zeit       -> verweigert ({e.args[0] if e.args else e})")


if __name__ == "__main__":
    main()
