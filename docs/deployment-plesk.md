# Produktionsnahes Deployment auf Plesk

Diese Anleitung ist fuer Ubuntu 24.04, Plesk Obsidian, Docker Compose v5 und die Domain `showroomflow.promotekk.com` vorbereitet. ShowroomFlow bindet ausschliesslich an `127.0.0.1:18080`; Datenbank und Redis bleiben in einem internen Docker-Netzwerk.

## 1. DNS vorbereiten

Beim DNS-Anbieter einen A-Record fuer `showroomflow.promotekk.com` auf die oeffentliche IPv4-Adresse des VPS setzen. Einen AAAA-Record nur setzen, wenn der VPS korrekt ueber IPv6 erreichbar ist.

## 2. Subdomain in Plesk anlegen

1. In Plesk `showroomflow.promotekk.com` als Subdomain anlegen.
2. Ein Let's-Encrypt-Zertifikat fuer die Subdomain ausstellen.
3. Dauerhafte Weiterleitung von HTTP auf HTTPS aktivieren.
4. Noch keine globalen nginx-Einstellungen aendern.

## 3. Repository im Plesk-Terminal installieren

```bash
mkdir -p /opt/showroomflow
git clone https://github.com/sfuersch/showroomflow.git /opt/showroomflow
cd /opt/showroomflow
cp .env.production.example .env.production
chmod 600 .env.production
```

Alle Platzhalter in `.env.production` ersetzen. Passwoerter mit Sonderzeichen muessen in `SHOWROOMFLOW_DATABASE_URL` und `SHOWROOMFLOW_REDIS_URL` URL-kodiert werden. Geheimnisse niemals committen oder im Chat teilen.

Den produktiven Cloudflare-R2-Speicher vor dem ersten Start gemaess `docs/cloudflare-r2.md` einrichten. Fuer einen EU-Bucket muss die Endpoint-URL auf `.eu.r2.cloudflarestorage.com` enden; als Region wird `auto` verwendet.

Ein sicheres Anwendungsgeheimnis kann auf dem VPS so erzeugt werden:

```bash
openssl rand -hex 48
```

## 4. Konfiguration vor dem Start pruefen

```bash
cd /opt/showroomflow
docker compose --env-file .env.production -f compose.production.yaml config --quiet
docker compose --env-file .env.production -f compose.production.yaml build
```

## 5. Container starten

```bash
cd /opt/showroomflow
docker compose --env-file .env.production -f compose.production.yaml up -d --wait
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 api
curl --fail http://127.0.0.1:18080/api/v1/ready
```

Nach erfolgreicher erster Anmeldung die beiden `SHOWROOMFLOW_BOOTSTRAP_ADMIN_*`-Zeilen aus `.env.production` entfernen und den API-Container neu erstellen.

## 6. Plesk nginx verbinden

Unter `Domains > showroomflow.promotekk.com > Apache & nginx Settings` den Proxy-Modus fuer diese Subdomain deaktivieren und PHP-Unterstuetzung abschalten. Danach den Inhalt aus `ops/plesk-nginx.conf` als zusaetzliche nginx-Direktiven eintragen. Plesk muss die Konfiguration ohne Fehler annehmen.

Anschliessend pruefen:

```bash
curl --fail https://showroomflow.promotekk.com/api/v1/health
curl --fail https://showroomflow.promotekk.com/api/v1/ready
```

## 7. Datenbanksicherung

```bash
chmod 750 /opt/showroomflow/ops/backup-database.sh
mkdir -p /var/backups/showroomflow
/opt/showroomflow/ops/backup-database.sh
ls -lh /var/backups/showroomflow
```

Nach erfolgreichem Test den Befehl ueber Plesk als taegliche geplante Aufgabe ausfuehren. Die lokale Sicherung wird standardmaessig 14 Tage aufbewahrt. Zusaetzlich sollte das Plesk-Serverbackup extern gespeichert werden.

Das Skript legt Sicherungsverzeichnis und neue Dump-Dateien mit ausschliesslichem Zugriff fuer den ausfuehrenden Root-Benutzer an.

## 8. SFTP-Export

Die SFTP-Zugangsdaten werden je Autohaus in der Verwaltungsoberflaeche gepflegt. Das Passwort wird mit dem produktiven `SHOWROOMFLOW_SECRET_KEY` verschluesselt gespeichert; dieser Schluessel darf nach der Einrichtung nicht unkoordiniert geaendert werden.

Fuer die sichere Serverpruefung wird der SHA-256-Fingerabdruck des SFTP-Hostschluessels benoetigt. Er kann vom SFTP-Betreiber bestaetigt und beispielsweise so ausgelesen werden:

```bash
ssh-keyscan -p 22 sftp.example.de 2>/dev/null | ssh-keygen -lf - -E sha256
```

Nach dem Speichern von Server und Port kann der Wert ueber **Hostschluessel abrufen** direkt vom SFTP-Server uebernommen werden. Bei der erstmaligen Einrichtung oder einer unerwarteten Aenderung sollte der angezeigte Wert im Format `SHA256:...` unabhaengig mit dem Betreiber abgeglichen werden. Ueber **Verbindung pruefen** wird das Zielverzeichnis bei Bedarf angelegt. Ist beim Auftrag der automatische Export aktiviert, folgen ZIP-Erstellung und SFTP-Uebertragung automatisch, sobald alle Pflichtfotos hochgeladen und verarbeitet sind. Fehlgeschlagene oder bereits abgeschlossene Uebertragungen koennen in der Auftragsansicht erneut gestartet werden.

## 9. Aktualisierung

Erst nach Datenbanksicherung aktualisieren:

```bash
cd /opt/showroomflow
git pull --ff-only
docker compose --env-file .env.production -f compose.production.yaml build
docker compose --env-file .env.production -f compose.production.yaml up -d --wait
docker compose --env-file .env.production -f compose.production.yaml ps
```

## Rollback

Vor jedem produktiven Update den aktuellen Commit notieren und eine Datenbanksicherung erstellen. Ein Datenbank-Rollback darf nicht allein durch Zuruecksetzen des Git-Commits erfolgen; Migrationen muessen passend zur betroffenen Version behandelt werden.
## Bildverarbeitung aktivieren

Die Verarbeitung bleibt nach dem ersten Deployment bewusst deaktiviert. Dadurch funktionieren
Foto-Uploads weiterhin, ohne versehentlich kostenpflichtige KI-Aufrufe auszulösen. Für remove.bg
werden in `.env.production` folgende Werte gesetzt:

```dotenv
SHOWROOMFLOW_PROCESSING_PROVIDER=remove_bg
SHOWROOMFLOW_REMOVE_BG_API_KEY=<API-Schlüssel>
SHOWROOMFLOW_REMOVE_BG_SIZE=preview
SHOWROOMFLOW_PROCESSING_QUEUE=showroomflow-processing
SHOWROOMFLOW_OUTPUT_WIDTH=1920
SHOWROOMFLOW_OUTPUT_HEIGHT=1440
```

Anschließend API und Worker gemeinsam bauen und starten:

```bash
docker compose --env-file .env.production -f compose.production.yaml build api worker
docker compose --env-file .env.production -f compose.production.yaml up -d api worker
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=80 worker
```

Der API-Container führt beim Start die Migration aus. Der Worker besitzt keinen öffentlichen Port
und ist nur mit PostgreSQL, Redis, R2 und dem konfigurierten KI-Dienst verbunden.

`preview` verwendet die kostengünstige Testauflösung. Für freigegebene Verkaufsbilder wird der
Wert später auf `auto` geändert und der Worker neu erstellt.

### Photoroom als A/B-Vergleich testen

Photoroom ist zunächst nur eine zusätzliche, manuell gestartete Vergleichsvariante. Das reguläre
remove.bg-Ergebnis und der Export werden dadurch nicht ersetzt. Für kostenlose, mit Wasserzeichen
versehene Sandbox-Tests werden diese Werte ergänzt:

```dotenv
SHOWROOMFLOW_PHOTOROOM_SANDBOX_API_KEY=<Sandbox-API-Schlüssel>
SHOWROOMFLOW_PHOTOROOM_LIVE_API_KEY=<Live-API-Schlüssel>
SHOWROOMFLOW_PHOTOROOM_SANDBOX=true
```

ShowroomFlow wählt anhand des im SuperAdmin-Backend gesetzten Sandbox-Schalters automatisch den
passenden Schlüssel. Danach API und Worker neu erstellen. In der Auftragsansicht erscheint für jedes
freizustellende Foto die Schaltfläche `Photoroom testen`. Original, remove.bg und Photoroom werden
nebeneinander angezeigt. In dieser ersten Teststufe nutzt Photoroom einen weichen KI-Schatten,
aber bewusst kein generatives Relighting, damit Fahrzeugfarbe und Details unverändert bleiben.

Zusätzlich kann `Optimiert testen` gestartet werden. Diese getrennt gespeicherte Variante verwendet
dieselbe perspektivabhängige Größenautomatik wie die produktive Verarbeitung. Sie überschreibt
weder das reguläre Ergebnis noch den Export und verändert Fahrzeugfarbe oder Fahrzeugdetails nicht
durch generatives Relighting.

Für Außenaufnahmen führt Photoroom zunächst eine transparente Konturerkennung durch. ShowroomFlow
berechnet daraus den Zielrahmen anhand des sichtbaren Fahrzeugflächenanteils und übergibt die
automatisch bestimmten Ränder an die anschließende Showroom-Verarbeitung. Dadurch werden schmale
Front- und Heckansichten, breite Seitenansichten und diagonale Perspektiven ohne fest zugeordnete
Perspektivprofile optisch angeglichen. Beim zweiten Aufruf erstellt Photoroom die Endkomposition mit
dem gewählten statischen Hintergrund und einem perspektivabhängigen weichen KI-Schatten. Dadurch
werden insbesondere Radaufstandspunkte und der Schatten unter diagonalen Fahrzeugansichten korrekt
aus der Fahrzeuggeometrie abgeleitet. Ziel-Flächenanteil sowie maximale Breite und Höhe werden
ausschließlich vom Systemadministrator unter `Verwaltung > Bilddienstleister` gepflegt. Ein
Schattenwert von `0` deaktiviert den KI-Schatten; jeder positive Wert aktiviert ihn. Dieser Ablauf
benötigt bei Photoroom zwei API-Aufrufe pro verarbeitetem Außenfoto. Bei remove.bg bleibt die lokale
Komposition einschließlich prozentual regelbarem Ersatzschatten aktiv.

Der Systemadministrator kann die zusätzlichen Bildvergleiche unter
`Verwaltung > Bilddienstleister` zentral ein- oder ausschalten. Bei ausgeschaltetem Vergleichsmodus sehen
Autohausbenutzer nur Original, optimiertes Ergebnis und `Verarbeitung starten`; technische
Dienstleisternamen werden in der Auftragsansicht nicht angezeigt. Original und `Optimiert` können
bei aktivem Vergleichsmodus über das Download-Symbol mit einem eindeutigen Dateinamen geladen
werden.

### Semantische KI-Masken erproben

Für Fotos mit Scheiben- oder Öffnungshintergrund kann ShowroomFlow zunächst GPT Image um eine
pixelgleiche magentafarbene Bereichsmarkierung bitten. Das System gewinnt daraus lokal die technische Maske
und verfeinert deren Kante am unveränderten Original. Unplausible, leere oder nicht verfügbare
KI-Ergebnisse fallen automatisch auf die bestehende Photoroom-Maskierung zurück.

Der Pilot wird über VPS-Secrets aktiviert; der API-Schlüssel wird niemals im Backend angezeigt:

```dotenv
SHOWROOMFLOW_OPENAI_MASK_ENABLED=true
SHOWROOMFLOW_OPENAI_API_KEY=<OpenAI-API-Schlüssel>
SHOWROOMFLOW_OPENAI_MASK_MODEL=gpt-image-2
SHOWROOMFLOW_OPENAI_MASK_TIMEOUT_SECONDS=240
SHOWROOMFLOW_OPENAI_MASK_REVIEW_ALL=true
```

Mit `SHOWROOMFLOW_OPENAI_MASK_REVIEW_ALL=true` landet jedes auf dieser Weise erzeugte Ergebnis
vor Export und Freigabe in der Operator-Prüfung. Das sollte während der Qualitätserprobung aktiv
bleiben. Die kostenpflichtigen Maskenaufrufe erscheinen im SuperAdmin-Kostendashboard als
`Semantische KI-Maske`.

Im zentralen Orientierungskatalog kann der Superadmin je Perspektive zusätzlich beschreiben,
welche Bildbereiche maskiert und welche Details besonders geschützt werden müssen. Leere Felder
verwenden weiterhin die Systemvorgaben. Der Schutzprompt ergänzt die festen Sicherheitsregeln; er
ersetzt sie nicht.

## Foto-Thumbnails

Ab Migration `0015_photo_thumbnails` werden für neue Originale, optimierte Ergebnisse und
Vergleichsbilder automatisch JPEG-Vorschauen mit maximal 480 × 360 Pixeln in R2 gespeichert.
Auftragsansicht und App verwenden diese kleinen Dateien; Vollbilder werden erst beim Öffnen oder
Herunterladen angefordert. Bestehende Bilder können nach dem Deployment einmalig ergänzt werden:

```bash
docker compose --env-file .env.production -f compose.production.yaml \
  exec -T api python -m app.thumbnail_backfill
```

Der Backfill ist wiederholbar und überspringt bereits vorhandene Vorschaubilder.

Nach der Migration `0007_image_service_credits` wird der reguläre Bilddienstleister unter
`Verwaltung > Bilddienstleister` durch den Systemadministrator gewählt. Die API-Schlüssel bleiben
weiterhin als VPS-Secrets in `.env.production`; die Oberfläche zeigt nur ihren Status. Ein dort
aktivierter Sandbox-Modus ergänzt bei einem Live-Schlüssel automatisch den Sandbox-Präfix. Ist
bereits ein Sandbox-Schlüssel hinterlegt, muss er für den Produktivbetrieb auf dem VPS durch einen
Live-Schlüssel ersetzt werden.

Jedes Autohaus besitzt ein konfigurierbares Monatskontingent. Ein Credit wird beim ersten
Verarbeitungsauftrag für ein Fahrzeug reserviert und deckt alle Außenbilder und Neuverarbeitungen
dieses Auftrags ab. Nicht genutzte Credits werden nicht übertragen; am ersten Kalendertag gilt
automatisch wieder das vollständige Monatskontingent. Eine neue interne Auftragsversion zählt als
neues Fahrzeug im Sinne der Abrechnung.

Der Systemadministrator kann einem Autohaus zusätzlich dauerhafte Credits gutschreiben. Diese
werden erst verwendet, wenn das Monatskontingent aufgebraucht ist, und verfallen beim Monatswechsel
nicht. Jede Gutschrift wird mit Betrag, Systemadministrator, Zeitpunkt und optionaler Notiz
protokolliert. Der aktuelle Zusatzsaldo wird atomar geführt, damit parallele Bildverarbeitungen
keine Credits doppelt verbrauchen.

### Overlays und Zusatzbilder verwalten

Nach Migration `0010_overlay_supplemental_assets` stehen in der Foto-Konfiguration zwei weitere
Bereiche bereit. Overlays werden als transparente PNG-Dateien gespeichert und können nach Marke,
Standort und Fotoposition eingeschränkt sowie in Position, Breite und Deckkraft konfiguriert
werden. Eine oder mehrere Fotopositionen sind möglich; ohne Auswahl wird automatisch die aktive
Fotoposition mit der kleinsten Export-Nr. verwendet. Zusatzbilder akzeptieren
PNG oder JPG und besitzen eine eigene Export-Nr.; auch sie können optional auf Marke und Standort
begrenzt werden.

Die Konfiguration ist ausschließlich für Systemadministratoren und Autohausadministratoren
erreichbar. Autohausadministratoren bleiben auf das eigene Autohaus begrenzt. Fotografen können
sich nicht an der Verwaltungsoberfläche anmelden und haben weder lesenden noch schreibenden Zugriff
auf diese Medien.

Der Worker legt passende aktive Overlays nach der regulären KI-Verarbeitung auf das optimierte
Ergebnis. Er prüft dabei Autohaus, Marke, Standort und Fotoposition erneut. Breite und Deckkraft
werden begrenzt, die Proportionen bleiben erhalten und zum Bildrand wird ein Sicherheitsabstand
eingehalten. Originale und manuelle Vergleichsvarianten werden nicht verändert. Nach einer
Overlay-Änderung müssen bereits verarbeitete Bilder erneut über `Verarbeitung starten` erzeugt
werden.

### ZIP-Archive erstellen

Nach Migration `0011_zip_exports` kann ein Administrator in der Auftragsansicht ein privates
ZIP-Archiv erstellen. Verarbeitete Außenbilder, unveränderte Detailbilder und die für Marke und
Standort passenden Zusatzbilder werden nach ihrer Export-Nr. sortiert, auf 1920 × 1440 Pixel
vereinheitlicht und als `<VIN>_<Export-Nr.>.jpg` in `<VIN>.zip` gespeichert. Das fertige Archiv
bleibt in R2 und wird über einen zeitlich begrenzten Download-Link bereitgestellt. Jeder erneute
Export wird als eigener Versuch archiviert.

Exportplätze werden bereits beim Speichern der Foto- und Zusatzbildkonfiguration geprüft. Zwei
Zusatzbilder dürfen denselben Platz nur verwenden, wenn sich ihre Marken- oder Standortbereiche
sicher ausschließen. Vor jeder ZIP-Erstellung validiert der Worker die für den konkreten Auftrag
wirksamen Bilder erneut. Doppelte Plätze oder noch nicht verarbeitete Pflichtbilder führen zu einer
verständlichen Fehlermeldung und niemals zum Überschreiben einer Datei.

## Benachrichtigungen zur Qualitätsprüfung

Ab Migration `0027_web_push_notifications` können Systemadministratoren und Operatoren auf der
Seite `Qualitätsprüfung` Browserbenachrichtigungen aktivieren. Für jedes Browserprofil wird ein
eigenes, jederzeit widerrufbares Abonnement gespeichert. Neue Prüfaufträge werden genau einmal
gemeldet; nicht mehr gültige Browser-Abonnements werden bei der Zustellung deaktiviert.

Vor dem ersten Einsatz wird einmalig ein VAPID-Schlüsselpaar erzeugt:

```bash
cd /opt/showroomflow
docker compose --env-file .env.production -f compose.production.yaml \
  run --rm --no-deps api python -m app.generate_web_push_keys
```

Die drei ausgegebenen `SHOWROOMFLOW_WEB_PUSH_*`-Zeilen werden in `.env.production` übernommen.
Danach müssen API und Worker neu erstellt werden. Die Schlüssel dürfen nicht bei jedem Deployment
neu erzeugt werden, da bestehende Browser-Abonnements an das ursprüngliche Schlüsselpaar gebunden
sind.

Auf iPhone und iPad steht Web Push für die über Safari zum Home-Bildschirm hinzugefügte
ShowroomFlow-Web-App zur Verfügung. Die Berechtigung wird anschließend innerhalb dieser Web-App
über `Benachrichtigungen aktivieren` angefordert.
