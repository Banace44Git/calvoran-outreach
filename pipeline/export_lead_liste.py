"""Druckbare Lead-Liste (DIN A4 quer) je Outreach-Welle.

Erzeugt eine HTML-Datei, die per Browser-Druck (Cmd-P → „Als PDF sichern") als
A4-Querformat-PDF gesichert wird — das Querformat ist über `@page { size: A4 landscape }`
fest eingebacken, es muss im Druckdialog nichts umgestellt werden.

Spaltenreihenfolge (wie für die Telefon-Akquise gewünscht):
    GF (Name u. Vorname, alle)  ·  Unternehmen  ·  Branche (kurz)  ·  Stadt  ·  Telefon  ·  Kommentare (leer)

Eine Zeile je Lead (Zellen brechen bei Bedarf um), zwischen den Leads immer eine Leerzeile.

Wiederholbar für andere Abfragen über die Parameter --wave / --status / --channel:
    .venv/bin/python pipeline/export_lead_liste.py                 # Welle 1, alle Brief-Leads
    .venv/bin/python pipeline/export_lead_liste.py --wave 2
    .venv/bin/python pipeline/export_lead_liste.py --status sent,won
    .venv/bin/python pipeline/export_lead_liste.py --sort score    # höchster Score zuerst
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date
from pathlib import Path

import phonenumbers

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from calvoran.db import get_client  # noqa: E402

OUT_DIR = _ROOT / "data"

# Kurz-Label für die groben Makrocluster (Fallback, wenn keine WZ-Bezeichnung vorliegt).
CLUSTER_LABEL = {
    "produzierend": "Produktion",
    "bau_gebaeudetechnik": "Bau/Gebäudetechnik",
    "grosshandel_distribution": "Großhandel",
    "handel_einzelhandel": "Einzelhandel",
    "dienstleistung": "Dienstleistung",
    "logistik_transport": "Logistik/Transport",
    "rest": "",
}


def din_phone(raw: str) -> str:
    """Deutsche Rufnummer DIN-5008-nah: '+49 228648040' -> '+49(0) 228 64 80 40'.
    (identisch zur Formatierung im Nachverfolgungs-Tab des Dashboards)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        x = phonenumbers.parse(raw, "DE")
        if not phonenumbers.is_valid_number(x):
            return raw
        nat = phonenumbers.format_number(x, phonenumbers.PhoneNumberFormat.NATIONAL)
    except phonenumbers.NumberParseException:
        return raw
    head, _, rest = nat.partition(" ")
    area = head.lstrip("0")
    local = "".join(c for c in rest if c.isdigit())
    groups: list = []
    while len(local) > 2:
        groups.insert(0, local[-2:])
        local = local[:-2]
    groups.insert(0, local)
    return f"+49(0) {area} " + " ".join(g for g in groups if g)


def kurz_branche(wz: str, cluster: str, maxlen: int = 44) -> str:
    """WZ-Bezeichnung ohne den führenden WZ-Code, auf Wortgrenze gekürzt. Ohne WZ-Text
    Fallback auf ein Kurz-Label des Makroclusters."""
    txt = re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", (wz or "").strip()).strip()
    if not txt:
        return CLUSTER_LABEL.get(cluster or "", (cluster or "").replace("_", " "))
    if len(txt) <= maxlen:
        return txt
    cut = txt.rfind(" ", 0, maxlen)
    return (txt[:cut] if cut > 0 else txt[:maxlen]).rstrip(" ,-") + "…"


def gf_namen(ges_vertreter) -> list[str]:
    """ges_vertreter (['Nachname, Vorname', ...]) -> Liste, wie gespeichert (Name u. Vorname)."""
    if isinstance(ges_vertreter, list):
        return [str(g).strip() for g in ges_vertreter if str(g).strip()]
    return [str(ges_vertreter).strip()] if ges_vertreter else []


def _chunks(seq: list, n: int = 50):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_leads(wave: int, channel: str, status: list[str] | None) -> list[dict]:
    cl = get_client()

    q = cl.table("outreach").select("company_id,status,sent_at").eq("channel", channel).eq("wave", wave)
    if status:
        q = q.in_("status", status)
    outreach = q.execute().data
    ids = [r["company_id"] for r in outreach]
    if not ids:
        return []
    ostat = {r["company_id"]: r for r in outreach}

    comp: dict = {}
    for batch in _chunks(ids):
        for c in (cl.table("companies")
                  .select("id,name,ort,plz,branche_wz,ges_vertreter,raw")
                  .in_("id", batch).execute().data):
            comp[c["id"]] = c

    scores: dict = {}
    for batch in _chunks(ids):
        for s in (cl.table("scores").select("company_id,cluster_branche,score_total")
                  .in_("company_id", batch).execute().data):
            scores[s["company_id"]] = s

    rows = []
    for cid in ids:
        c = comp.get(cid) or {}
        s = scores.get(cid) or {}
        raw = c.get("raw") or {}
        rows.append({
            "gf": gf_namen(c.get("ges_vertreter")),
            "firma": (c.get("name") or "").strip(),
            "branche": kurz_branche(c.get("branche_wz"), s.get("cluster_branche")),
            "stadt": (c.get("ort") or "").strip(),
            "telefon": din_phone(raw.get("Tel.")),
            "score": s.get("score_total") if s.get("score_total") is not None else -1,
            "status": ostat.get(cid, {}).get("status", ""),
        })
    return rows


def sort_rows(rows: list[dict], key: str) -> list[dict]:
    if key == "score":
        return sorted(rows, key=lambda r: (-(r["score"] or -1), r["firma"].lower()))
    if key == "firma":
        return sorted(rows, key=lambda r: r["firma"].lower())
    # default: stadt, dann firma
    return sorted(rows, key=lambda r: (r["stadt"].lower(), r["firma"].lower()))


HTML_HEAD = """<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4 landscape; margin: 10mm 9mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color: #111;
          font-size: 10.5pt; margin: 0; }}
  h1 {{ font-size: 13pt; margin: 0 0 2mm; }}
  .meta {{ font-size: 9pt; color: #555; margin: 0 0 4mm; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  thead th {{ text-align: left; font-size: 8.5pt; text-transform: uppercase; letter-spacing: .3px;
              color: #333; border-bottom: 1.4pt solid #333; padding: 0 4px 2mm; }}
  tbody td {{ vertical-align: top; padding: 1.5mm 4px; border-bottom: .4pt solid #ccc;
              word-wrap: break-word; overflow-wrap: break-word; }}
  tr.lead {{ page-break-inside: avoid; }}
  tr.gap td {{ border: none; height: 4mm; padding: 0; }}
  td.gf {{ line-height: 1.35; }}
  td.tel {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  td.komm {{ border-bottom: .4pt solid #999; }}
  col.c-gf     {{ width: 17%; }}
  col.c-firma  {{ width: 20%; }}
  col.c-branche{{ width: 19%; }}
  col.c-stadt  {{ width: 9%; }}
  col.c-tel    {{ width: 13%; }}
  col.c-komm   {{ width: 22%; }}
</style>
"""


def render_html(rows: list[dict], title: str, subtitle: str) -> str:
    def esc(s: str) -> str:
        return html.escape(str(s or ""))

    body = [HTML_HEAD.format(title=esc(title)),
            f"<h1>{esc(title)}</h1>",
            f'<div class="meta">{esc(subtitle)}</div>',
            "<table>",
            '<colgroup>'
            '<col class="c-gf"><col class="c-firma"><col class="c-branche">'
            '<col class="c-stadt"><col class="c-tel"><col class="c-komm"></colgroup>',
            "<thead><tr>"
            "<th>Geschäftsführer</th><th>Unternehmen</th><th>Branche</th>"
            "<th>Stadt</th><th>Telefon</th><th>Kommentare</th>"
            "</tr></thead>",
            "<tbody>"]

    for i, r in enumerate(rows):
        gf = "<br>".join(esc(g) for g in r["gf"]) or "&nbsp;"
        body.append(
            '<tr class="lead">'
            f'<td class="gf">{gf}</td>'
            f'<td>{esc(r["firma"])}</td>'
            f'<td>{esc(r["branche"])}</td>'
            f'<td>{esc(r["stadt"])}</td>'
            f'<td class="tel">{esc(r["telefon"])}</td>'
            '<td class="komm">&nbsp;</td>'
            '</tr>')
        if i < len(rows) - 1:
            body.append('<tr class="gap"><td colspan="6"></td></tr>')

    body.append("</tbody></table>")
    return "\n".join(body)


def main() -> None:
    ap = argparse.ArgumentParser(description="Druckbare Lead-Liste (A4 quer) je Outreach-Welle.")
    ap.add_argument("--wave", type=int, default=1)
    ap.add_argument("--channel", default="letter", help="outreach.channel (default: letter)")
    ap.add_argument("--status", default=None,
                    help="Komma-Liste von outreach.status (leer = alle dieser Welle)")
    ap.add_argument("--sort", choices=["stadt", "firma", "score"], default="stadt")
    ap.add_argument("--title", default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    status = [s.strip() for s in args.status.split(",")] if args.status else None
    rows = fetch_leads(args.wave, args.channel, status)
    if not rows:
        print(f"Keine Leads für Welle {args.wave} (channel={args.channel}, status={status or 'alle'}).")
        return
    rows = sort_rows(rows, args.sort)

    title = args.title or f"Lead-Liste Welle {args.wave}"
    stat_txt = ", ".join(status) if status else "alle"
    subtitle = (f"{len(rows)} Leads · Kanal {args.channel} · Status {stat_txt} · "
                f"sortiert nach {args.sort} · Stand {date.today().strftime('%d.%m.%Y')}")

    OUT_DIR.mkdir(exist_ok=True)
    out = args.out or OUT_DIR / f"{date.today().isoformat()}_lead-liste_welle{args.wave}.html"
    out.write_text(render_html(rows, title, subtitle), encoding="utf-8")
    print(f"{len(rows)} Leads → {out}")
    print(f"Drucken/als PDF sichern:  open '{out}'  → Cmd-P → „Als PDF sichern“ (Querformat ist voreingestellt)")


if __name__ == "__main__":
    main()
