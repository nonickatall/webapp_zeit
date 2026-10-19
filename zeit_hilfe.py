"""zeit_hilfe.py — Hilfetexte (Markdown) sicher in HTML umsetzen.

Bewusst ein kleiner eigener Umsetzer statt einer Bibliothek: Er kann genau das,
was die Hilfetexte brauchen (Ueberschriften #/##/###, Absaetze, Listen - und 1.,
Tabellen mit |, **fett**, *kursiv*, `code`), und escaped ZUERST jedes HTML.
Ein Hilfetext kann damit kein Skript und kein fremdes Markup in die Seite bringen.
"""
import html
import re

_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"<i>\1</i>"),
]


def _inline(text):
    t = html.escape(text, quote=True)
    for muster, ersatz in _INLINE:
        t = muster.sub(ersatz, t)
    return t


def _zellen(zeile):
    z = zeile.strip()
    if z.startswith("|"):
        z = z[1:]
    if z.endswith("|"):
        z = z[:-1]
    return [c.strip() for c in z.split("|")]


def markdown_html(text):
    zeilen = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    aus, absatz, i = [], [], 0

    def absatz_ende():
        if absatz:
            aus.append("<p>" + " ".join(_inline(z) for z in absatz) + "</p>")
            absatz.clear()

    while i < len(zeilen):
        z = zeilen[i].rstrip()
        s = z.strip()
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if not s:
            absatz_ende(); i += 1; continue
        if m:
            absatz_ende()
            n = len(m.group(1)) + 1          # # -> h2, ## -> h3, ### -> h4 (h1 traegt die Seite)
            aus.append(f"<h{n}>{_inline(m.group(2))}</h{n}>"); i += 1; continue
        if s.startswith("|") and i + 1 < len(zeilen) and re.match(r"^\s*\|?\s*:?-{2,}", zeilen[i + 1]):
            absatz_ende()
            kopf = _zellen(s)
            aus.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in kopf) + "</tr></thead><tbody>")
            i += 2
            while i < len(zeilen) and zeilen[i].strip().startswith("|"):
                aus.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _zellen(zeilen[i])) + "</tr>")
                i += 1
            aus.append("</tbody></table>"); continue
        lm = re.match(r"^(-|\*|\d+\.)\s+(.*)$", s)
        if lm:
            absatz_ende()
            tag = "ol" if lm.group(1)[0].isdigit() else "ul"
            aus.append(f"<{tag}>")
            while i < len(zeilen):
                lm = re.match(r"^\s*(-|\*|\d+\.)\s+(.*)$", zeilen[i])
                if not lm or (("ol" if lm.group(1)[0].isdigit() else "ul") != tag):
                    break
                aus.append(f"<li>{_inline(lm.group(2))}</li>"); i += 1
            aus.append(f"</{tag}>"); continue
        absatz.append(s); i += 1
    absatz_ende()
    return "\n".join(aus)
