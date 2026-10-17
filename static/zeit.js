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
