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

/* Korrekturen (Gehen nachtragen, Stempelzeit, Zeitregel zuordnen, Abzug/Zuschlag): je Formular
   wie Zeitregel -- Auftrag senden, Bestaetigung NUR aus zeiterfassung_auftrag (alle 300 ms,
   hoechstens 5 s), "Erneut versuchen" mit derselben anfrage_id. Ergebnis im <p> nach dem Formular. */
Array.prototype.forEach.call(document.querySelectorAll('form.korrektur-form'), function (form) {
    var ausgabe = form.nextElementSibling;
    var erneut = form.querySelector('button.erneut');
    var knopf = form.querySelector('button[type=submit]');
    var url = form.getAttribute('data-url');
    var letzte = null;
    /* Gehen-Datum folgt dem Kommen-Datum, solange es noch auf dem bisherigen Kommen-Tag stand
       (oder davor laege); ein bewusst anderer Tag (Nachtschicht) bleibt stehen. */
    var kd = form.querySelector('[name=kommen_datum]'), gd = form.querySelector('[name=gehen_datum]');
    if (kd && gd) {
        var vorher = kd.value;
        kd.addEventListener('change', function () {
            if (!gd.value || gd.value === vorher || gd.value < kd.value) gd.value = kd.value;
            vorher = kd.value;
        });
    }

    function zeige(text, art) { ausgabe.textContent = text; ausgabe.className = 'ergebnis ' + art; ausgabe.hidden = false; }

    function daten() {
        var f = new FormData(form), d = {};
        f.forEach(function (v, k) { d[k] = v; });
        ['kommen', 'gehen'].forEach(function (p) {     /* Datum + Uhrzeit -> YYYY-MM-DDTHH:MM */
            if (d[p + '_datum'] === undefined) return;
            d[p] = d[p + '_datum'] && d[p + '_uhrzeit'] ? d[p + '_datum'] + 'T' + d[p + '_uhrzeit'] : '';
            delete d[p + '_datum']; delete d[p + '_uhrzeit'];
        });
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
                            if (form.hasAttribute('data-neu') && a.zeiterfassung_id) {
                                /* Anlegen: zum neuen Datensatz */
                                zeige('Datensatz Nr. ' + a.zeiterfassung_id + ' angelegt – ' + a.uhrzeit + ' Uhr', 'ok');
                                setTimeout(function () {
                                    window.location.href = '/zeit/datensatz/' + encodeURIComponent(a.zeiterfassung_id);
                                }, 1500);
                            } else {
                                zeige((form.getAttribute('data-ok') || 'Korrektur gespeichert') + ' – ' + a.uhrzeit + ' Uhr', 'ok');
                                setTimeout(function () { window.location.reload(); }, 1500);
                            }
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
});

/* Uhr zum Waehlen der Uhrzeit (Felder input.uhrzeit, Format hh:mm, 24 Stunden).
   Erst die Stunde: aussen 1-12, innen 13-23 und 00. Nach dem Loslassen die Minute,
   beim Ziehen minutengenau. Tippen im Feld geht weiterhin. */
(function () {
    var felder = document.querySelectorAll('input.uhrzeit');
    if (!felder.length) return;
    var NS = 'http://www.w3.org/2000/svg', M = 130, R_AUSSEN = 100, R_INNEN = 64;
    var ZEIT_RE = /^(\d{1,2}):(\d{2})$/;
    var pop = document.createElement('div');
    pop.className = 'uhr'; pop.hidden = true;
    pop.innerHTML = '<div class="uhr-kopf"><span class="uhr-h">--</span>:<span class="uhr-m">--</span></div>'
        + '<svg class="uhr-blatt" viewBox="0 0 260 260" width="260" height="260"></svg>'
        + '<div class="uhr-fuss"><button type="button" class="uhr-ab">Abbrechen</button>'
        + '<button type="button" class="uhr-ok">OK</button></div>';
    document.body.appendChild(pop);
    var svg = pop.querySelector('svg'), kopfH = pop.querySelector('.uhr-h'), kopfM = pop.querySelector('.uhr-m');
    var feld = null, h = 0, m = 0, modus = 'h', ziehen = false;

    function zwei(n) { return (n < 10 ? '0' : '') + n; }
    function el(name, attr, text) {
        var e = document.createElementNS(NS, name);
        for (var k in attr) e.setAttribute(k, attr[k]);
        if (text !== undefined) e.textContent = text;
        svg.appendChild(e);
    }
    function punkt(grad, r) {
        var w = grad * Math.PI / 180;
        return { x: M + r * Math.sin(w), y: M - r * Math.cos(w) };
    }
    function stundenOrt(s) {
        if (s >= 1 && s <= 12) return { grad: (s % 12) * 30, r: R_AUSSEN };
        return { grad: (s === 0 ? 0 : (s - 12) * 30), r: R_INNEN };
    }
    function zahl(x, y, groesse, farbe, text) {
        el('text', { x: x, y: y, 'text-anchor': 'middle', 'dominant-baseline': 'central',
                     'font-size': groesse, fill: farbe }, text);
    }

    function zeichnen() {
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        kopfH.textContent = zwei(h); kopfM.textContent = zwei(m);
        kopfH.className = 'uhr-h' + (modus === 'h' ? ' aktiv' : '');
        kopfM.className = 'uhr-m' + (modus === 'm' ? ' aktiv' : '');
        el('circle', { cx: M, cy: M, r: 124, fill: '#eceff1' });
        var ort = modus === 'h' ? stundenOrt(h) : { grad: m * 6, r: R_AUSSEN };
        var p = punkt(ort.grad, ort.r);
        el('line', { x1: M, y1: M, x2: p.x, y2: p.y, stroke: '#1565c0', 'stroke-width': 2 });
        el('circle', { cx: M, cy: M, r: 4, fill: '#1565c0' });
        el('circle', { cx: p.x, cy: p.y, r: 17, fill: '#1565c0' });
        var i, q, wert;
        for (i = 0; i < 12; i++) {
            q = punkt(i * 30, R_AUSSEN);
            if (modus === 'h') {
                wert = i === 0 ? 12 : i;
                zahl(q.x, q.y, 15, wert === h ? '#fff' : '#263238', String(wert));
                wert = i === 0 ? 0 : i + 12; q = punkt(i * 30, R_INNEN);
                zahl(q.x, q.y, 12, wert === h ? '#fff' : '#607d8b', zwei(wert));
            } else {
                zahl(q.x, q.y, 15, i * 5 === m ? '#fff' : '#263238', zwei(i * 5));
            }
        }
    }

    function waehlen(e) {
        var r = svg.getBoundingClientRect();
        var x = (e.clientX - r.left) * 260 / r.width - M, y = (e.clientY - r.top) * 260 / r.height - M;
        var grad = (Math.atan2(x, -y) * 180 / Math.PI + 360) % 360, abstand = Math.sqrt(x * x + y * y);
        if (modus === 'h') {
            var i = Math.round(grad / 30) % 12;
            h = abstand < (R_AUSSEN + R_INNEN) / 2 ? (i === 0 ? 0 : i + 12) : (i === 0 ? 12 : i);
        } else {
            m = Math.round(grad / 6) % 60;
        }
        zeichnen();
    }

    function lesen(f) {
        var t = ZEIT_RE.exec(f.value.trim());
        return t && +t[1] < 24 && +t[2] < 60 ? [+t[1], +t[2]] : null;
    }
    function oeffnen(f) {
        feld = f; modus = 'h';
        var w = lesen(f);
        if (w) { h = w[0]; m = w[1]; } else { h = new Date().getHours(); m = 0; }
        pop.hidden = false; zeichnen();
        var r = f.getBoundingClientRect();
        var links = window.scrollX + r.left;
        links = Math.max(window.scrollX + 8,
            Math.min(links, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8));
        pop.style.left = links + 'px';
        pop.style.top = (window.scrollY + r.bottom + 4) + 'px';
    }
    function schliessen() { pop.hidden = true; feld = null; ziehen = false; }
    function uebernehmen() {
        if (!feld) return;
        feld.value = zwei(h) + ':' + zwei(m);
        feld.dispatchEvent(new Event('input', { bubbles: true }));
        schliessen();
    }

    svg.addEventListener('pointerdown', function (e) {
        ziehen = true;
        try { svg.setPointerCapture(e.pointerId); } catch (x) { /* ohne Capture geht es auch */ }
        waehlen(e); e.preventDefault();
    });
    svg.addEventListener('pointermove', function (e) { if (ziehen) waehlen(e); });
    svg.addEventListener('pointerup', function () {
        if (!ziehen) return;
        ziehen = false;
        if (modus === 'h') { modus = 'm'; zeichnen(); } else { uebernehmen(); }
    });
    kopfH.addEventListener('click', function () { modus = 'h'; zeichnen(); });
    kopfM.addEventListener('click', function () { modus = 'm'; zeichnen(); });
    pop.querySelector('.uhr-ok').addEventListener('click', uebernehmen);
    pop.querySelector('.uhr-ab').addEventListener('click', schliessen);
    /* Capture-Phase: pruefen, bevor das Zifferblatt neu gezeichnet wird (sonst haengt das Ziel nicht mehr darin) */
    document.addEventListener('pointerdown', function (e) {
        if (!pop.hidden && !pop.contains(e.target) && e.target !== feld) schliessen();
    }, true);
    document.addEventListener('keydown', function (e) {
        if (pop.hidden) return;
        if (e.key === 'Escape') schliessen();
        if (e.key === 'Enter' && e.target === feld) { e.preventDefault(); schliessen(); }
    });
    Array.prototype.forEach.call(felder, function (f) {
        f.addEventListener('click', function () { if (pop.hidden || feld !== f) oeffnen(f); });
        f.addEventListener('input', function () {          /* Tippen: Uhr mitfuehren */
            var w = lesen(f);
            if (feld === f && w) { h = w[0]; m = w[1]; zeichnen(); }
        });
    });
})();
