"""zeit_mqtt.py — Verwaltungsauftraege der webapp_zeit an die Engine senden.

Die Webapp schreibt nie selbst in myhome_zeit. Jede Aenderung geht als Befehl
ueber /myhome/interner_mqtt_eingang (Rechtepruefung im Eingang) an den
zeiterfassung_server. Format:
    befehl;sms:<Nummer des Bearbeiters>;<anfrage_id>;<terminal>;<param>
param = base64url(JSON) -- enthaelt nie ';', wird im Eingang unveraendert durchgereicht.
"""
import base64
import json
import time
import uuid

import paho.mqtt.client as mqtt

TERMINAL = "verwaltung"
PARAM_MAX = 2048          # dieselbe Grenze prueft der Eingang


def param_kodieren(daten):
    roh = json.dumps(daten, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    text = base64.urlsafe_b64encode(roh).decode("ascii").rstrip("=")
    if len(text) > PARAM_MAX:
        raise ValueError("Parameter zu lang")
    return text


def _client(name):
    try:   # paho-mqtt >= 2
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=name)
    except AttributeError:   # paho-mqtt 1.x
        return mqtt.Client(client_id=name)


def senden(cfg, befehl, nummer, anfrage_id, param_text, timeout=5.0):
    """Einen Auftrag publizieren (QoS 1). Wirft bei Fehlern -- der Aufrufer meldet
    das dem Browser; eine Bestaetigung gibt es ohnehin nur ueber zeiterfassung_auftrag."""
    if not cfg.get("mqtt_host") or not cfg.get("mqtt_user"):
        raise RuntimeError("MQTT ist fuer webapp_zeit nicht konfiguriert")
    nachricht = f"{befehl};sms:{nummer};{anfrage_id};{TERMINAL};{param_text}"
    c = _client(f"webapp_zeit_{uuid.uuid4().hex[:8]}")
    c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_passwd"])
    c.connect(cfg["mqtt_host"], cfg["mqtt_port"], 30)
    c.loop_start()
    try:
        info = c.publish(cfg["eingang_topic"], nachricht, qos=1)
        info.wait_for_publish(timeout)
        if not info.is_published():
            raise RuntimeError("MQTT-Versand nicht bestaetigt")
    finally:
        time.sleep(0.05)
        c.loop_stop()
        c.disconnect()
