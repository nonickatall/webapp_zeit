/* Nach Leerlauf zur Anmeldung wechseln. Verbindlich ist der Server: die Sitzung
   laeuft dort nach derselben Zeit ohne Seitenaufruf ab. Deshalb zaehlt auch hier
   nur der Seitenaufruf (nicht Klicks ohne Anfrage) -- das raeumt nur den Bildschirm. */
(function () {
    var sek = parseInt(document.body.getAttribute('data-leerlauf-sek') || '0', 10);
    if (!sek) return;
    setTimeout(function () {
        window.location.href = '/zeit/login?abgelaufen=1';
    }, (sek + 5) * 1000);
})();

/* Hilfe: Text der Seite aus myhome_zeit.hilfe laden und im Fenster zeigen.
   Das HTML kommt vom Server bereits sicher umgesetzt (zeit_hilfe.py). */
(function () {
    var knopf = document.getElementById('hilfe-knopf');
    var fenster = document.getElementById('hilfe-fenster');
    if (!knopf || !fenster) return;
    var titel = document.getElementById('hilfe-titel');
    var inhalt = document.getElementById('hilfe-inhalt');
    var overlay = document.getElementById('hilfe-overlay');

    function zu() { fenster.hidden = true; if (overlay) overlay.hidden = true; }

    knopf.addEventListener('click', function () {
        var schluessel = knopf.getAttribute('data-hilfe');
        titel.textContent = 'Hilfe';
        inhalt.textContent = 'wird geladen …';
        fenster.hidden = false;
        if (overlay) overlay.hidden = false;
        fetch('/zeit/hilfe/' + encodeURIComponent(schluessel), { cache: 'no-store', credentials: 'same-origin' })
            .then(function (r) {
                if (r.redirected || r.status === 401) { window.location.href = '/zeit/login?abgelaufen=1'; return null; }
                return r.json();
            })
            .then(function (d) {
                if (!d) return;
                titel.textContent = 'Hilfe – ' + (d.ueberschrift || schluessel);
                inhalt.innerHTML = d.html || '';
            })
            .catch(function () { inhalt.textContent = 'Die Hilfe konnte nicht geladen werden.'; });
    });
    document.getElementById('hilfe-zu').addEventListener('click', zu);
    if (overlay) overlay.addEventListener('click', zu);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') zu(); });
})();

/* Zeitregel anlegen: Auftrag senden, Bestaetigung NUR aus zeiterfassung_auftrag
   abfragen (alle 300 ms, hoechstens 5 s). "Erneut versuchen" sendet dieselbe anfrage_id. */
(function () {
    var form = document.getElementById('zeitregel-neu');
    if (!form) return;
    var ausgabe = document.getElementById('zeitregel-ergebnis');
    var erneut = document.getElementById('zeitregel-erneut');
    var knopf = form.querySelector('button[type=submit]');
    var letzte = null;

    function zeige(text, art) { ausgabe.textContent = text; ausgabe.className = 'ergebnis ' + art; ausgabe.hidden = false; }

    function daten() {
        var f = new FormData(form), d = {};
        f.forEach(function (v, k) { d[k] = v; });
        return d;
    }

    function abfragen(id) {
        var start = Date.now();
        (function frage() {
            fetch('/zeit/auftrag/' + encodeURIComponent(id), { cache: 'no-store', credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : null; })
                .catch(function () { return null; })
                .then(function (a) {
                    if (a && a.ok) {
                        knopf.disabled = false;
                        if (a.ergebnis === 'ausgefuehrt') {
                            zeige('Zeitregel angelegt – ' + a.uhrzeit + ' Uhr – Nr. ' + a.zeitregel_id, 'ok');
                            letzte = null; erneut.hidden = true;
                            setTimeout(function () { window.location.reload(); }, 1500);
                        } else {
                            zeige('Nicht angelegt: ' + (a.text || a.grund), 'fehler');
                            letzte = null; erneut.hidden = true;
                        }
                        return;
                    }
                    if (Date.now() - start >= 5000) {
                        knopf.disabled = false;
                        zeige('Keine Bestätigung erhalten.', 'fehler');
                        erneut.hidden = false;
                        return;
                    }
                    setTimeout(frage, 300);
                });
        })();
    }

    function senden(d) {
        knopf.disabled = true; erneut.hidden = true;
        zeige('Wird angelegt …', 'warten');
        fetch('/zeit/zeitregeln/neu', {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
            headers: { 'Content-Type': 'application/json', 'X-Zeit-Auftrag': '1' },
            body: JSON.stringify(d)
        })
            .then(function (r) {
                if (r.redirected) { window.location.href = '/zeit/login?abgelaufen=1'; return null; }
                return r.json();
            })
            .then(function (a) {
                if (!a) return;
                if (!a.ok) { knopf.disabled = false; zeige(a.fehler || 'Nicht gesendet.', 'fehler'); return; }
                d.anfrage_id = a.anfrage_id; letzte = d;
                abfragen(a.anfrage_id);
            })
            .catch(function () { knopf.disabled = false; zeige('Nicht gesendet.', 'fehler'); });
    }

    form.addEventListener('submit', function (e) { e.preventDefault(); senden(daten()); });
    erneut.addEventListener('click', function () { if (letzte) senden(letzte); });
})();

/* Korrektur (Gehen nachtragen / Stempelzeit aendern): wie Zeitregel -- Auftrag senden,
   Bestaetigung NUR aus zeiterfassung_auftrag (alle 300 ms, hoechstens 5 s),
   "Erneut versuchen" mit derselben anfrage_id. */
(function () {
    var form = document.getElementById('korrektur-neu');
    if (!form) return;
    var ausgabe = document.getElementById('korrektur-ergebnis');
    var erneut = document.getElementById('korrektur-erneut');
    var knopf = form.querySelector('button[type=submit]');
    var url = form.getAttribute('data-url');
    var letzte = null;

    function zeige(text, art) { ausgabe.textContent = text; ausgabe.className = 'ergebnis ' + art; ausgabe.hidden = false; }

    function daten() {
        var f = new FormData(form), d = {};
        f.forEach(function (v, k) { d[k] = v; });
        return d;
    }

    function abfragen(id) {
        var start = Date.now();
        (function frage() {
            fetch('/zeit/auftrag/' + encodeURIComponent(id), { cache: 'no-store', credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.json() : null; })
                .catch(function () { return null; })
                .then(function (a) {
                    if (a && a.ok) {
                        knopf.disabled = false; letzte = null; erneut.hidden = true;
                        if (a.ergebnis === 'ausgefuehrt') {
                            zeige('Korrektur gespeichert – ' + a.uhrzeit + ' Uhr', 'ok');
                            setTimeout(function () { window.location.reload(); }, 1500);
                        } else {
                            zeige('Nicht gespeichert: ' + (a.text || a.grund), 'fehler');
                        }
                        return;
                    }
                    if (Date.now() - start >= 5000) {
                        knopf.disabled = false;
                        zeige('Keine Bestätigung erhalten.', 'fehler');
                        erneut.hidden = false;
                        return;
                    }
                    setTimeout(frage, 300);
                });
        })();
    }

    function senden(d) {
        knopf.disabled = true; erneut.hidden = true;
        zeige('Wird gespeichert …', 'warten');
        fetch(url, {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
            headers: { 'Content-Type': 'application/json', 'X-Zeit-Auftrag': '1' },
            body: JSON.stringify(d)
        })
            .then(function (r) {
                if (r.redirected) { window.location.href = '/zeit/login?abgelaufen=1'; return null; }
                return r.json();
            })
            .then(function (a) {
                if (!a) return;
                if (!a.ok) { knopf.disabled = false; zeige(a.fehler || 'Nicht gesendet.', 'fehler'); return; }
                d.anfrage_id = a.anfrage_id; letzte = d;
                abfragen(a.anfrage_id);
            })
            .catch(function () { knopf.disabled = false; zeige('Nicht gesendet.', 'fehler'); });
    }

    form.addEventListener('submit', function (e) { e.preventDefault(); senden(daten()); });
    erneut.addEventListener('click', function () { if (letzte) senden(letzte); });
})();
