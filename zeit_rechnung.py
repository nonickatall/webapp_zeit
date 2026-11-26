"""zeit_rechnung.py — Tagesrechnung der Zeiterfassung (Umbau Schritt 2): Soll, Ist, Tagessaldo, laufender Saldo.

Nur lesen. Regeln (Konzept Zeiterfassung/umbau_soll_saldo.md, Abschnitt 3):
  Tagesplan  = Tagesabweichung, sonst Wochenplan der geltenden Zuordnung fuer den Wochentag
  Soll       = Tagesplan.soll (0 bei freiem Tag); ohne Zuordnung: kein Soll (None)
  Arbeit     = Summe netto der abgeschlossenen Datensaetze mit Kommen an dem Tag (Ortszeit)
  Gutschrift = Feiertag (Soll, halber: Soll/2) + Abwesenheiten (ganz: Soll, halb: Soll/2, Stunden;
               Art mit Gutschrift "keine": 0)
  Ist        = Arbeit + Gutschrift
  TSaldo     = Ist - Soll                           (erst ab heute; heute nicht, solange jemand da ist)
  Saldo      = Saldo Vortag + TSaldo, Start: Monatsabschluss bzw. Startsaldo (der spaetere gilt)
"""
import datetime

from zeit_db import ortszeit, tag_grenzen_utc

FEIERTAG_KUERZEL = "FT"
WOCHENTAGE = ("mo", "di", "mi", "do", "fr", "sa", "so")
WT_NAMEN = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")


def monatsgrenzen(jahr, monat):
    von = datetime.date(jahr, monat, 1)
    bis = datetime.date(jahr + (monat == 12), monat % 12 + 1, 1) - datetime.timedelta(days=1)
    return von, bis


class Rechnung:
    """Laedt die Stammdaten einmal und rechnet beliebige Zeitraeume fuer einen Mitarbeiter."""

    def __init__(self, db):
        self.db = db
        z = db.z
        self.tp = {r["id"]: r for r in db.abfrage(
            f"SELECT id, nr, name, farbe, frei, soll_minuten FROM {z}.tagesplan")}
        self.wp = {r["id"]: r for r in db.abfrage(f"SELECT * FROM {z}.wochenplan")}
        self.arten = {r["id"]: r for r in db.abfrage(f"SELECT * FROM {z}.abwesenheitsart")}
        # Anzeige der Feiertage: feste Abwesenheitsart "FT" (Farbe in Stammdaten -> Abwesenheitsarten einstellbar)
        ft = next((a for a in self.arten.values() if a["kuerzel"] == FEIERTAG_KUERZEL and a["aktiv"]), None)
        self.ft_farbe = ft["farbe"] if ft else "#7cb342"

    # ---------------- Daten eines Mitarbeiters
    def _laden(self, uid, von, bis):
        z = self.db.z
        b, e = tag_grenzen_utc(von)[0], tag_grenzen_utc(bis)[1]
        d = {
            "zuordnungen": self.db.abfrage(
                f"SELECT gueltig_ab, wochenplan_id FROM {z}.user_wochenplan WHERE user_id = %s "
                f"AND aufgehoben_am IS NULL ORDER BY gueltig_ab, id", (uid,)),
            "abweichung": {r["datum"]: r["tagesplan_id"] for r in self.db.abfrage(
                f"SELECT datum, tagesplan_id FROM {z}.tag_abweichung WHERE user_id = %s AND datum BETWEEN %s AND %s "
                f"AND aufgehoben_am IS NULL", (uid, von, bis))},
            "feiertage": {r["datum"]: r for r in self.db.abfrage(
                f"SELECT datum, name, halb FROM {z}.feiertag WHERE datum BETWEEN %s AND %s AND aktiv = 1", (von, bis))},
            "abwesenheiten": {},
            "datensaetze": {},
        }
        for r in self.db.abfrage(
                f"SELECT datum, art_id, umfang, minuten, notiz FROM {z}.abwesenheit WHERE user_id = %s "
                f"AND datum BETWEEN %s AND %s AND aufgehoben_am IS NULL ORDER BY id", (uid, von, bis)):
            d["abwesenheiten"].setdefault(r["datum"], []).append(r)
        for r in self.db.abfrage(
                f"SELECT id, kommen, gehen, status, netto_minuten, pause_minuten, klaeren FROM {z}.zeiterfassung "
                f"WHERE user_id = %s AND kommen >= %s AND kommen < %s ORDER BY kommen, id", (uid, b, e)):
            d["datensaetze"].setdefault(ortszeit(r["kommen"]).date(), []).append(r)
        return d

    def _plan(self, d, tag):
        tp_id = d["abweichung"].get(tag)
        if tp_id is None:
            z = None
            for zu in d["zuordnungen"]:
                if zu["gueltig_ab"] <= tag:
                    z = zu
            if z is None:
                return None
            w = self.wp.get(z["wochenplan_id"])
            tp_id = w["tp_" + WOCHENTAGE[tag.weekday()]] if w else None
        return self.tp.get(tp_id)

    # ---------------- ein Tag
    def tag(self, d, tag, heute):
        tp = self._plan(d, tag)
        soll = None if tp is None else (0 if tp["frei"] else int(tp["soll_minuten"]))
        zeile = {"datum": tag, "wt": WT_NAMEN[tag.weekday()], "tp": tp, "soll": soll, "arbeit": 0,
                 "gutschrift": 0, "ist": None, "tsaldo": None, "beginn": None, "ende": None, "mehr": False,
                 "warnung": [], "laeuft": False, "abw": [], "feiertag": None,
                 "wochenende": tag.weekday() >= 5, "zukunft": tag > heute}
        s = soll or 0
        f = d["feiertage"].get(tag)
        if f:
            zeile["feiertag"] = f["name"] + (" (halb)" if f["halb"] else "")
            g = s // 2 if f["halb"] else s
            zeile["gutschrift"] += g
            if g:
                zeile["abw"].append({"kuerzel": "FT", "name": f["name"], "farbe": self.ft_farbe, "minuten": g})
        for a in d["abwesenheiten"].get(tag, []):
            art = self.arten.get(a["art_id"]) or {"kuerzel": "?", "name": "?", "farbe": "#999999", "gutschrift": "soll"}
            if art["gutschrift"] == "keine":
                g = 0
            elif a["umfang"] == "ganz":
                g = s
            elif a["umfang"] == "halb":
                g = s // 2
            else:
                g = int(a["minuten"] or 0)
            zeile["gutschrift"] += g
            zeile["abw"].append({"kuerzel": art["kuerzel"], "name": art["name"], "farbe": art["farbe"], "minuten": g,
                                 "umfang": a["umfang"]})
        ds = d["datensaetze"].get(tag, [])
        if ds:
            zeile["beginn"] = ortszeit(ds[0]["kommen"])
            gehen = [r["gehen"] for r in ds if r["gehen"]]
            zeile["ende"] = ortszeit(max(gehen)) if gehen else None
            zeile["mehr"] = len(ds) > 1
        for r in ds:
            if r["status"] == "offen":
                zeile["laeuft"] = True
            elif r["status"] == "automatisch_beendet":
                zeile["warnung"].append("Gehen fehlt")
            elif r["netto_minuten"] is None:
                zeile["warnung"].append("ohne Pause/Plan")
            else:
                zeile["arbeit"] += int(r["netto_minuten"])
            if r["klaeren"]:
                zeile["warnung"].append("zu klären")
        if zeile["laeuft"] and tag < heute:
            zeile["warnung"].append("offen")
        zeile["warnung"] = sorted(set(zeile["warnung"]))
        if not zeile["zukunft"]:
            zeile["ist"] = zeile["arbeit"] + zeile["gutschrift"]
            if soll is not None and not (zeile["laeuft"] and tag == heute):
                zeile["tsaldo"] = zeile["ist"] - soll
        return zeile

    # ---------------- Saldo
    def _start(self, uid, vor_tag):
        """(datum, saldo) des spaetesten Startpunkts vor vor_tag: Monatsabschluss (Folgetag) oder Startsaldo."""
        z = self.db.z
        kandidaten = []
        r = self.db.eins(f"SELECT monat, saldo_ende FROM {z}.monatsabschluss WHERE user_id = %s AND monat < %s "
                         f"ORDER BY monat DESC LIMIT 1", (uid, vor_tag.replace(day=1)))
        if r:
            m = r["monat"]
            kandidaten.append((monatsgrenzen(m.year, m.month)[1] + datetime.timedelta(days=1), int(r["saldo_ende"])))
        r = self.db.eins(f"SELECT stichtag, minuten FROM {z}.saldo_start WHERE user_id = %s AND stichtag <= %s "
                         f"AND aktiv = 1 ORDER BY stichtag DESC LIMIT 1", (uid, vor_tag))
        if r:
            kandidaten.append((r["stichtag"], int(r["minuten"])))
        return max(kandidaten) if kandidaten else None

    def monat(self, uid, jahr, monat, heute):
        """Tageszeilen eines Monats mit laufendem Saldo + Summen."""
        von, bis = monatsgrenzen(jahr, monat)
        start = self._start(uid, von)
        # Startsalden, die innerhalb des Monats liegen, setzen den Saldo dort neu
        neu = {r["stichtag"]: int(r["minuten"]) for r in self.db.abfrage(
            f"SELECT stichtag, minuten FROM {self.db.z}.saldo_start WHERE user_id = %s AND aktiv = 1 "
            f"AND stichtag BETWEEN %s AND %s", (uid, von, bis))}
        saldo = None
        if start is not None:
            s_tag, saldo = start
            if s_tag < von:
                d0 = self._laden(uid, s_tag, von - datetime.timedelta(days=1))
                t = s_tag
                while t < von:
                    z = self.tag(d0, t, heute)
                    if z["tsaldo"] is not None:
                        saldo += z["tsaldo"]
                    t += datetime.timedelta(days=1)
        saldo_vormonat = saldo
        d = self._laden(uid, von, bis)
        zeilen = []
        summe = {"soll": 0, "arbeit": 0, "gutschrift": 0, "ist": 0, "tsaldo": 0, "warnungen": 0, "ohne_plan": 0}
        t = von
        while t <= bis:
            z = self.tag(d, t, heute)
            if t in neu:
                saldo = neu[t]
                z["saldo_neu"] = True
            if z["tsaldo"] is not None and saldo is not None:
                saldo += z["tsaldo"]
            z["saldo"] = saldo if not z["zukunft"] else None
            if not z["zukunft"]:
                summe["soll"] += z["soll"] or 0
                summe["arbeit"] += z["arbeit"]
                summe["gutschrift"] += z["gutschrift"]
                summe["ist"] += z["ist"] or 0
                summe["tsaldo"] += z["tsaldo"] or 0
                summe["warnungen"] += 1 if z["warnung"] else 0
                summe["ohne_plan"] += 1 if z["soll"] is None and z["ist"] else 0
            zeilen.append(z)
            t += datetime.timedelta(days=1)
        letzte = [z["saldo"] for z in zeilen if z["saldo"] is not None]
        summe["saldo_ende"] = letzte[-1] if letzte else None
        summe["saldo_vormonat"] = saldo_vormonat
        summe["soll_monat"] = sum(z["soll"] or 0 for z in zeilen)
        return zeilen, summe
