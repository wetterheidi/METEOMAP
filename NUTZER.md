# MeteoMap – Hinweise für Nutzer

## Woher kommen die Daten?

MeteoMap zeigt zwei Arten von Stationsdaten gleichzeitig: **METAR** und **SYNOP**.
Beide können einzeln oder gemeinsam aktiviert werden.

---

### METAR

METARs sind kodierte Wetterberichte, die primär für die Luftfahrt erstellt werden.
Sie stammen von Flughäfen und größeren Landeplätzen.

| | |
|---|---|
| **Quelle** | Aviation Weather Center (AWC), USA |
| **Aktualisierung** | Stündlich (reguläre METARs), bei Bedarf auch häufiger (SPECI) |
| **Abdeckung** | Weltweit, Schwerpunkt zivile und militärische Flugplätze |
| **Abruf** | Beim Öffnen der Karte direkt aus dem Internet, immer aktuell |

---

### SYNOP

SYNOPs (FM-12) sind das klassische synoptische Meldungsformat des WMO-Netzwerks.
MeteoMap verwendet drei Quellen, die automatisch zusammengeführt werden:

#### OGIMET (Europa, Nordafrika)

| | |
|---|---|
| **Quelle** | OGIMET – internationales SYNOP-Archiv |
| **Aktualisierung** | Alle 30 Minuten vom Server neu abgerufen |
| **Abdeckung** | Europa und Nordafrika (ca. 20 WMO-Blöcke) |
| **Meldungsrhythmus** | Stündlich (Haupttermine), manche Stationen 3-stündlich |

#### DWD BUFR (international)

| | |
|---|---|
| **Quelle** | Deutscher Wetterdienst, OpenData-Portal (BUFR-Format) |
| **Aktualisierung** | Alle 30 Minuten vom Server neu abgerufen |
| **Abdeckung** | International, Schwerpunkt Europa |
| **Besonderheit** | Höhere Datenqualität als OGIMET, da originale BUFR-Rohdaten |
| **Priorität** | Bei doppelter Erfassung wird BUFR gegenüber OGIMET bevorzugt |

#### DMI (Grönland)

| | |
|---|---|
| **Quelle** | Danish Meteorological Institute, Open Data API |
| **Aktualisierung** | Alle 15 Minuten vom Server neu abgerufen |
| **Abdeckung** | Grönland |
| **Priorität** | Höchste Priorität – überschreibt OGIMET und BUFR bei gleicher Station |

---

## Wie aktuell sind die Daten?

### METAR
Beim Aufruf der Karte werden die METARs direkt beim AWC abgerufen – die Daten
sind so aktuell wie möglich, typischerweise < 30 Minuten alt.

### SYNOP
Die Daten werden serverseitig alle 30 Minuten aktualisiert. Zwischen Messzeitpunkt
und Darstellung auf der Karte vergehen typischerweise:

```
Messzeit → SYNOP-Meldung → Eingang bei OGIMET/DWD → Server-Abruf → Anzeige
                  ~10 min              ~5–20 min         ~30 min
```

**Faustregel:** SYNOP-Daten in MeteoMap sind in der Regel **30–60 Minuten** nach
dem Beobachtungszeitpunkt verfügbar.

---

## Der Zeitschieber

Mit dem Zeitschieber (links unten) kann bis zu **24 Stunden** in die Vergangenheit
navigiert werden.

**Wie er funktioniert:**

Für jeden Zeitpunkt im Schieber wird die nächstgelegene verfügbare Meldung
(innerhalb ±60 Minuten) pro Station angezeigt. Bei SYNOPs bedeutet das: Du siehst
immer den Termin, der dem gewählten Zeitpunkt am nächsten liegt – also typischerweise
den Stundentermin unmittelbar vor oder nach dem eingestellten Zeitpunkt.

**Wichtig:**
- Der Verlauf reicht nur so weit zurück, wie der Server Daten gespeichert hat
  (aktuell **12 Stunden**). Ältere Zeitpunkte zeigen keine oder deutlich weniger Stationen.
- Die ersten 12 Stunden nach Inbetriebnahme füllen sich schrittweise.
- METAR-Daten sind nur für die letzten **24 Stunden** über die AWC-API verfügbar.

---

## Quellen und Stationsdichte – was zu erwarten ist

| Region | METAR | SYNOP |
|--------|-------|-------|
| Mitteleuropa | dicht (jeder Flugplatz) | sehr dicht (stündlich) |
| Nordafrika | mittel | mittel |
| Skandinavien | mittel | gut |
| Grönland | wenige | gut (DMI) |
| Russland | wenige | vorhanden (WMO-Blöcke 20, 26) |
| Übersee | nur wo METAR-Abdeckung | nicht in MeteoMap |

---

## Qualitätshinweise

- **SYNOP-Druckangaben:** MeteoMap zeigt, soweit möglich, den QNH (auf MSL
  reduzierten Druck). Bei Stationen mit bekannter Höhe und Temperatur wird er
  aus dem Stationsdruck (QFE) berechnet. Die Güte der Reduktion hängt von der
  Stationshöhe ab – bei Gebirgsstationen mit > 500 m Höhe ist Vorsicht geboten.

- **Wolkendaten SYNOP vs. METAR:** SYNOPs kodieren Bewölkung in der WMO-Okta-Skala
  und werden in METAR-äquivalente Bedeckungsgrade (FEW/SCT/BKN/OVC) umgerechnet.
  METAR-Wolkenangaben sind direkte Beobachtungen und i.d.R. präziser.

- **Böen:** Böendaten stammen aus Sektion 3 oder 5 des SYNOP, nicht alle Stationen
  melden sie. Fehlende Böenangabe bedeutet nicht zwingend windstilles Wetter.

- **Sichtweite:** In SYNOP als Code (VV-Gruppe) gemeldet, stufenweise Auflösung –
  nicht mit METAR-Präzision vergleichbar.

---

## Was bedeutet die Stationsfarbe?

Die Stationsmarkierungen sind entsprechend des ausgewählten Parameters eingefärbt
(z.B. Temperatur, Luftdruck, Wind). Die Legende ist links unten eingeblendet.
Symbole folgen dem WMO-Standard für synoptische Stationsmuster.
