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

    function zu() { fenster.hidden = true; }

    knopf.addEventListener('click', function () {
        var schluessel = knopf.getAttribute('data-hilfe');
        titel.textContent = 'Hilfe';
        inhalt.textContent = 'wird geladen …';
        fenster.hidden = false;
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
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') zu(); });
})();
