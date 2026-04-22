function testAllSymbols() {
  // Eine Liste der wichtigsten WMO-Strings, die wir testen wollen
  const testWx = [
    // 00
    //'FU', 'VA', 'DU', 'SA', 'BLSA', 'VCBLSA', 'BLDU', 'VCBLDU', 'BLPY', 'SS', 'PO', 'VCPO', 'DS', 'VCSS','VCDS'
    // 10
    //'BR', 'MIFG', 'VCTS', 'VIRGA', 'VCSH', 'TS', 'SQ', 'FC', '+FC'
    //30
    //'SS', 'DRSA', 'DS', 'DU', 'DRDU', '+SS', '+DS', 'BLSN', 'VCBLSN', 'DRSN'
    //40
    //'VCFG', 'BCFG', 'PRFG', 'FG', 'FZFG'
    //50
    //'-DZ', 'DZ', '+DZ', '-FZDZ', 'FZDZ', '+FZDZ', '-DZRA', 'DZRA', '+DZRA'
    //60
    //'-RA', 'RA', '+RA', '-RASN', 'RASN', '+RASN'
    //70
    //'UP', 'SG', 'IC', 'PL'
    //80
    //'-SHRA', 'SHRA', '+SHRA', '-SHSN', 'SHSN', '+SHSN', '-GS', 'GS', '+GS', '-GR', '-SHGS', 'SHGS', '+SHGS', '-SHGR'
    //90
    'SHGR', 'TSRA', '+TSRA', 'TSSN', 'TSGS', 'TSGR', '+TSSN', '+TSGS', '+TSGR'
  ];

  // Hol dir den aktuellen Mittelpunkt der Karte
  const center = map.getCenter();
  let lat = center.lat + 2.5; // Start etwas weiter nördlich
  let lonStart = center.lng - 4; // Start etwas weiter westlich
  let lon = lonStart;

  const mockObs = [];

  // Für jeden String eine Fake-Wetterstation bauen
  testWx.forEach((wx, i) => {
    mockObs.push({
      icaoId: 'TST' + i,
      name: 'Test-Symbol: ' + wx,
      lat: lat,
      lon: lon,
      elev: 100,
      obsTime: Math.floor(Date.now() / 1000),
      metarType: 'TEST',
      visib: "5000",
      skyCondition: [],
      wxString: wx,
      rawOb: 'FAKE METAR TEST DATA FOR ' + wx
    });

    // Raster-Logik: 6 Symbole pro Zeile
    lon += 1.5;
    if ((i + 1) % 6 === 0) {
      lat -= 1.0;
      lon = lonStart;
    }
  });

  // Layer leeren
  if (markerLayer) { map.removeLayer(markerLayer); markerLayer = null; }
  markerLayer = L.layerGroup();

  // Damit wir die Symbole sehen, erzwingen wir den WW-Parameter
  selectedParams = ['ww'];
  refreshParamUI(); // Sidebar updaten

  // Marker zeichnen
  mockObs.forEach(obs => {
    try {
      markerLayer.addLayer(createMarker(obs, selectedParams));
    } catch(e) {
      console.error("Fehler bei Symbol " + obs.wxString, e);
    }
  });

  markerLayer.addTo(map);
  document.getElementById('status-text').textContent = 'TEST-MODUS: ' + testWx.length + ' Symbole generiert';
}

// Skript direkt ausführen:
testAllSymbols();