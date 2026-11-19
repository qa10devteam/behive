Offline recordings for legal_sources adapters.
Status, headers, content type, size, and a truncated heading or an error/metadata body.
No full act text and no PDF of an act. Live readings 2026-11-17:
  CELEX bare work 32022L2555 -> 400, 205 B, no Content-Type
  CELEX 32022L2555.POL -> 303, Location http://.../DOC_1, xhtml
  DOC_1 over https -> 200, application/xhtml+xml, 732726 B, no Content-Length
  ELI DU/2026/901 -> 200 JSON, 1153 B, textHTML=false, textPDF=true
  print term10/2608 -> 200 JSON, 329 B, title from JSON, attachments include 2608.pdf
text.pdf for DU/2026/901 was confirmed application/pdf (Range request); the file was not saved.
Live readings 2026-11-19 (L7-FIX, 4 requests to publications.europa.eu, none to eur-lex.europa.eu):
  CELEX 52023XC0914%2801%29.POL -> 404, 67 B, id taken literally (percent-encoded parentheses are not decoded)
  CELEX 52023XC0914(01).POL     -> 404, 63 B, id read correctly (no POL manifestation on the celex resource)
  CELEX 52023XC0914(01).ENG     -> 404, 63 B, id read correctly
  SPARQL STRSTARTS 52023XC0914  -> 200, 618 B, works for 52023XC0914(01) and 52023XC0914(01)R(01)
