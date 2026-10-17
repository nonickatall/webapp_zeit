"""zeit_benutzer.py — Zugaenge der Zeiterfassungs-Verwaltung verwalten (nur auf dem Server).

    cd /etc/webapp_zeit && venv/bin/python zeit_benutzer.py anlegen <benutzername> <myhome_user_id>
    ... liste
    ... link <benutzername>          neuer Einmal-Link (Passwort vergessen), beendet Sitzungen
    ... sperren <benutzername>       Zugang deaktivieren, beendet Sitzungen
    ... entsperren <benutzername>

Der Einmal-Link gilt 24 h und setzt das Passwort. Eine Benutzerverwaltung in der
Webapp gibt es bewusst nicht. Anlegen nur fuer einen aktiven myhome.user (id <> 0),
jeder myhome.user hoechstens einmal.
"""
import sys

from zeit_auth import AuthDB
from zeit_db import ZeitDB, lade_konfig


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cfg = lade_konfig()
    auth = AuthDB(cfg["auth_db"], cfg["leerlauf_sek"], cfg["max_fehlversuche"], cfg["sperre_sek"])
    db = ZeitDB(cfg)
    basis = input_basis()
    cmd = sys.argv[1]
    if cmd == "anlegen" and len(sys.argv) == 4:
        name, uid = sys.argv[2].strip(), int(sys.argv[3])
        mu = db.myhome_user(uid)
        if mu is None:
            sys.exit(f"ABBRUCH: myhome.user {uid} existiert nicht, ist gesperrt oder ist id 0.")
        try:
            token = auth.anlegen(name, uid)
        except Exception as e:
            sys.exit(f"ABBRUCH: {e} (Benutzername oder myhome.user schon vergeben?)")
        db.logdaten(name, "benutzer_anlegen", f"angelegt fuer myhome.user {uid}", "kommandozeile")
        print(f"Angelegt: {name} -> myhome.user {uid} ({mu['name']})")
        print(f"Einmal-Link (24 h): {basis}/zeit/passwort?t={token}")
    elif cmd == "link" and len(sys.argv) == 3:
        token = auth.token_neu(sys.argv[2])
        if token is None:
            sys.exit("ABBRUCH: unbekannter Benutzername")
        db.logdaten(sys.argv[2], "benutzer_link", "neuer Einmal-Link", "kommandozeile")
        print(f"Einmal-Link (24 h): {basis}/zeit/passwort?t={token}")
    elif cmd in ("sperren", "entsperren") and len(sys.argv) == 3:
        if not auth.aktiv_setzen(sys.argv[2], cmd == "entsperren"):
            sys.exit("ABBRUCH: unbekannter Benutzername")
        db.logdaten(sys.argv[2], "benutzer_" + cmd, cmd, "kommandozeile")
        print("ok")
    elif cmd == "liste":
        for b in auth.liste():
            print(b)
    else:
        sys.exit(__doc__)


def input_basis():
    import configparser, os
    cp = configparser.ConfigParser()
    cp.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp_zeit.conf"), encoding="utf-8")
    return cp.get("webapp_zeit", "basis_url", fallback="https://192.168.0.3:5010").rstrip("/")


if __name__ == "__main__":
    main()
