"""Queen Memory — cross-mission knowledge persistence and operational context injection."""

import logging
#!/usr/bin/env python3
"""
HIVE 2.0 — Queen Memory (Trwały wektor analityczny)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


log = logging.getLogger(__name__)
Doktryna z dokumentu:
  "idea ta musi stać się beeqeen — strukturą kluczową dla istnienia Ula
   implikującą wektory analityczne"

Beeqeen to NIE jest jednorazowy prompt. To jest PAMIĘĆ OPERACYJNA — struktura
która akumuluje wiedzę między misjami i kieruje wektory kolejnych misji.

Co robi ten moduł:
  1. RECALL — przed planowaniem nowej misji Queen ładuje z DuckDB:
       - FUIR (Follow-Up Intelligence Requirements) z poprzednich ocen
       - Luki badawcze (hive_gaps) z powiązanych misji
       - Kluczowe ustalenia (hive3_mission_memory) z podobnych tematów
       - Oceny Queen (hive_queen_assessments) — co już wiemy z pewnością

  2. CONTEXT INJECTION — kompiluje to w strukturę kontekstu dla planera:
       - "Wiemy to: [confirmed intelligence]"
       - "Nie wiemy tego: [intelligence voids]"
       - "Szukaj tego: [FUIR directives]"
       - "Nie powtarzaj: [already covered queries]"

  3. PERSIST — po misji zapisuje nowe ustalenia do pamięci

  4. CLUSTERING — grupuje misje tematycznie — Queen widzi 
     cały klaster wiedzy, nie tylko ostatnią misję

Używany przez QueenPlanner w hive2.py:
  memory = QueenMemory(topic)
  ctx = memory.recall()
  # ctx injected into Queen prompt → perfect instructions for scouts
"""

import os
import json
