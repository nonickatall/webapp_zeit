/* Nach Leerlauf zur Anmeldung wechseln. Verbindlich ist der Server: die Sitzung
   laeuft dort nach derselben Zeit ohne Seitenaufruf ab. Deshalb zaehlt auch hier
   nur der Seitenaufruf (nicht Klicks ohne Anfrage) -- das raeumt nur den Bildschirm. */
/* Farben (Tagesplan, Abwesenheit): die CSP erlaubt keine style-Attribute im HTML,
   daher traegt das Template data-farbe und das Skript setzt die Farbe. */
(function () {
    var els = document.querySelectorAll('[data-farbe]');
    for (var i = 0; i < els.length; i++) {
        var f = els[i].getAttribute('data-farbe');
        if (/^#[0-9a-fA-F]{6}$/.test(f)) els[i].style.backgroundColor = f;
    }
})();

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
    var VORLAGE = 'zeitregel_vorlage';
    var FELDER = ['gueltig_ab', 'pause_minuten', 'pause_ab_minuten', 'sollbeginn', 'begruendung'];

    function zeige(text, art) { ausgabe.textContent = text; ausgabe.className = 'ergebnis ' + art; ausgabe.hidden = false; }

    /* Nach dem Anlegen laedt die Seite neu: die Werte (ohne Mitarbeiter) bleiben fuer den naechsten stehen */
    function vorlageMerken(d, meldung) {
        var v = { meldung: meldung };
        FELDER.forEach(function (k) { v[k] = d[k] || ''; });
        try { sessionStorage.setItem(VORLAGE, JSON.stringify(v)); } catch (e) { /* ohne Speicher: leeres Formular */ }
    }
    (function vorlageLaden() {
        var v = null;
        try { v = JSON.parse(sessionStorage.getItem(VORLAGE) || 'null'); sessionStorage.removeItem(VORLAGE); } catch (e) { v = null; }
        if (!v) return;
        FELDER.forEach(function (k) { if (v[k] !== undefined && form.elements[k]) form.elements[k].value = v[k]; });
        form.elements.user_id.value = '';
        zeige((v.meldung ? v.meldung + '. ' : '') + 'Werte bleiben stehen – nächsten Mitarbeiter wählen und „Anlegen“.', 'ok');
        form.elements.user_id.focus();
    })();

    /* "Kopieren" in der Liste: Werte einer Regel als Vorlage ins Formular, gueltig ab = heute */
    Array.prototype.forEach.call(document.querySelectorAll('button.btn-kopieren'), function (b) {
        b.addEventListener('click', function () {
            form.elements.user_id.value = b.dataset.user || '';
            form.elements.gueltig_ab.value = form.elements.gueltig_ab.defaultValue;
            form.elements.pause_minuten.value = b.dataset.pause;
            form.elements.pause_ab_minuten.value = b.dataset.pauseAb;
            form.elements.sollbeginn.value = b.dataset.soll || '';
            letzte = null; erneut.hidden = true;
            zeige('Vorlage übernommen – Mitarbeiter, Werte und Begründung prüfen, dann „Anlegen“.', 'warten');
            form.scrollIntoView({ behavior: 'smooth', block: 'start' });
            (form.elements.begruendung.value ? form.elements.user_id : form.elements.begruendung).focus();
        });
    });

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
                            var meldung = 'Zeitregel angelegt – ' + a.uhrzeit + ' Uhr – Nr. ' + a.zeitregel_id;
                            zeige(meldung, 'ok');
                            if (letzte) vorlageMerken(letzte, meldung);
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
                                zeige((form.getAttribute('data-ok') || 'Korrektur gespeichert') + ' – ' + a.uhrzeit + ' Uhr'
                                     + (a.objekt && /Tage neu/.test(a.objekt) ? ' – ' + a.objekt.replace(/^.*\(|\)$/g, '') : ''), 'ok');
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

/* Filter (GET-Formulare der Listen): Auswahl wirkt sofort, ohne "Filter"-Knopf.
   Nur Auswahllisten und Datumsfelder; Auftragsformulare (.formular) sind nicht betroffen. */
Array.prototype.forEach.call(document.querySelectorAll('form.filter-zeile:not(.formular)'), function (form) {
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'get') return;
    var warte = null;
    Array.prototype.forEach.call(form.querySelectorAll('select, input[type=date], input[type=month]'), function (feld) {
        feld.addEventListener('change', function () {
            if (feld.tagName === 'SELECT') { form.submit(); return; }
            /* Datum: beim Tippen meldet der Browser schon "0002", "0020" ... -> erst ein
               vollstaendiges Jahr (ab 2000) und kurz Ruhe abwarten */
            clearTimeout(warte);
            if (!/^(19|20)\d\d-/.test(feld.value)) return;
            warte = setTimeout(function () { form.submit(); }, 700);
        });
    });
});

/* Passwortfelder: Auge-Symbol zum Anzeigen/Verbergen der Eingabe. */
(function () {
    var AUGE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
        '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
    var ZU = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
        '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>' +
        '<line x1="3" y1="3" x2="21" y2="21"/></svg>';
    var felder = document.querySelectorAll('input[type="password"]');
    Array.prototype.forEach.call(felder, function (feld) {
        var huelle = document.createElement('span');
        huelle.className = 'pw-feld';
        feld.parentNode.insertBefore(huelle, feld);
        huelle.appendChild(feld);
        var knopf = document.createElement('button');
        knopf.type = 'button';
        knopf.className = 'pw-auge';
        function setzen(sichtbar) {
            feld.type = sichtbar ? 'text' : 'password';
            knopf.innerHTML = sichtbar ? ZU : AUGE;
            knopf.title = sichtbar ? 'Passwort verbergen' : 'Passwort anzeigen';
            knopf.setAttribute('aria-label', knopf.title);
            knopf.setAttribute('aria-pressed', sichtbar ? 'true' : 'false');
        }
        setzen(false);
        knopf.addEventListener('click', function (e) {
            e.preventDefault();
            setzen(feld.type === 'password');
            feld.focus();
        });
        // Vor dem Absenden wieder verbergen (Browser speichert sonst ggf. als Text)
        if (feld.form) feld.form.addEventListener('submit', function () { feld.type = 'password'; });
        huelle.appendChild(knopf);
    });
})();

/* Stammdaten: "Bearbeiten"/"Aufheben" fuellt das Formular data-form mit den Werten aus data-fuellen
   (JSON, Feldname -> Wert; Haken: 1/0). "Neu" leert das Formular wieder (auch die versteckte id). */
(function () {
    function setzen(form, werte) {
        Object.keys(werte).forEach(function (name) {
            var v = werte[name];
            Array.prototype.forEach.call(form.querySelectorAll('[name="' + name + '"]'), function (el) {
                if (el.type === 'checkbox') el.checked = v === 1 || v === true || v === '1';
                else if (el.type !== 'hidden' || name === 'id' || name === 'bundesland') el.value = v === null ? '' : v;
            });
        });
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-fuellen]'), function (knopf) {
        knopf.addEventListener('click', function () {
            var form = document.getElementById(knopf.getAttribute('data-form'));
            if (!form) return;
            form.hidden = false;
            setzen(form, JSON.parse(knopf.getAttribute('data-fuellen')));
            form.classList.add('bearbeiten');
            form.scrollIntoView({ behavior: 'smooth', block: 'center' });
            var erstes = form.querySelector('input:not([type=hidden]):not([readonly]), select');
            if (erstes) erstes.focus({ preventScroll: true });
        });
    });
    Array.prototype.forEach.call(document.querySelectorAll('form.stammdaten-form button.btn-neu'), function (knopf) {
        knopf.addEventListener('click', function () {
            var form = knopf.form;
            form.reset();
            var id = form.querySelector('input[type=hidden][name=id]');
            if (id) id.value = '';
            form.classList.remove('bearbeiten');
            var aus = form.nextElementSibling;
            if (aus && aus.classList.contains('ergebnis')) aus.hidden = true;
        });
    });
})();

/* Tabellenzeilen mit data-href sind klickbar (Monatsuebersicht: Tag -> Datensaetze, Mitarbeiter -> Monat). */
Array.prototype.forEach.call(document.querySelectorAll('tr[data-href]'), function (tr) {
    tr.addEventListener('click', function (e) {
        if (e.target.closest('a, button, input, select')) return;
        window.location.href = tr.getAttribute('data-href');
    });
});

/* Uhrzeit schnell tippen: "715" -> 07:15, "7" -> 07:00, "7.15"/"7,15" -> 07:15. Beim Verlassen des Feldes und
   VOR jedem Absenden (Capture-Phase, laeuft vor den Formular-Handlern). */
(function () {
    function normal(v) {
        var t = (v || '').trim().replace(/[.,]/, ':');
        var m = /^(\d{1,2}):?(\d{2})$/.exec(t) || /^(\d{1,2})()$/.exec(t);
        if (!m) return v;
        var h = parseInt(m[1], 10), min = m[2] ? parseInt(m[2], 10) : 0;
        if (h > 23 || min > 59) return v;
        return (h < 10 ? '0' : '') + h + ':' + (min < 10 ? '0' : '') + min;
    }
    /* Dauer (Soll, Saldo, Stunden): "5,5"/"5.5" = 5:30 (Dezimalstunden), "8" -> 8:00, "-26,75" -> -26:45 */
    function dauer(v) {
        var t = (v || '').trim(), m = /^([+-]?)(\d{1,4})(?:[.,](\d{1,2}))?$/.exec(t);
        if (!m) return v;
        var min = Math.round(parseFloat(m[2] + '.' + (m[3] || '0')) * 60);
        return (m[1] === '-' && min ? '-' : '') + Math.floor(min / 60) + ':' + ('0' + min % 60).slice(-2);
    }
    function eines(el) {
        if (!el.matches) return;
        if (el.matches('input.uhrzeit')) el.value = normal(el.value);
        else if (el.matches('input.dauer')) el.value = dauer(el.value);
    }
    function alle(form) {
        Array.prototype.forEach.call(form.querySelectorAll('input.uhrzeit, input.dauer'), eines);
    }
    document.addEventListener('blur', function (e) { eines(e.target); }, true);
    /* Enter im Feld: vor der Formatpruefung des Browsers umwandeln (blur kommt dabei nicht) */
    document.addEventListener('keydown', function (e) { if (e.key === 'Enter') eines(e.target); }, true);
    document.addEventListener('submit', function (e) { alle(e.target); }, true);
})();

/* Tagesfenster, Abwesenheit: Stunden-Feld nur bei Umfang "Stunden", "bis" nur bei ganzem Tag;
   Warnung, wenn ein Urlaubszeitraum den Rest uebersteigt (nur Hinweis, gespeichert wird trotzdem). */
Array.prototype.forEach.call(document.querySelectorAll('form.abw-form'), function (form) {
    var umfang = form.querySelector('[name=umfang]'), stunden = form.querySelector('.abw-stunden'),
        bisFeld = form.querySelector('.abw-bis'), bis = form.querySelector('[name=bis]'),
        von = form.querySelector('[name=von]'), art = form.querySelector('[name=art_id]');
    function anpassen() {
        stunden.hidden = umfang.value !== 'stunden';
        stunden.querySelector('input').required = umfang.value === 'stunden';
        bisFeld.hidden = umfang.value !== 'ganz';
        if (umfang.value !== 'ganz') bis.value = von.value;
    }
    umfang.addEventListener('change', anpassen);
    anpassen();
    form.addEventListener('submit', function (e) {
        var rest = form.getAttribute('data-urlaub-rest');
        var opt = art.options[art.selectedIndex];
        if (rest === '' || !opt || opt.getAttribute('data-urlaub') !== '1') return;
        var tage = umfang.value === 'halb' ? 0.5 : umfang.value === 'stunden' ? 0 :
            Math.round((new Date(bis.value) - new Date(von.value)) / 86400000) + 1;
        if (tage > parseFloat(rest) && !confirm('Resturlaub ' + rest + ' Tage, eingetragen werden bis zu ' + tage +
                ' Kalendertage (nur Arbeitstage zählen). Trotzdem eintragen?')) {
            e.preventDefault(); e.stopImmediatePropagation();
        }
    }, true);
});
