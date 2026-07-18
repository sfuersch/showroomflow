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

## 8. Aktualisierung

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
SHOWROOMFLOW_PHOTOROOM_API_KEY=<API-Schlüssel>
SHOWROOMFLOW_PHOTOROOM_SANDBOX=true
```

Der Schlüssel kann ohne `sandbox_` eingetragen werden; ShowroomFlow ergänzt den Präfix im
Sandbox-Modus. Danach API und Worker neu erstellen. In der Auftragsansicht erscheint für jedes
freizustellende Foto die Schaltfläche `Photoroom testen`. Original, remove.bg und Photoroom werden
nebeneinander angezeigt. In dieser ersten Teststufe nutzt Photoroom einen weichen KI-Schatten,
aber bewusst kein generatives Relighting, damit Fahrzeugfarbe und Details unverändert bleiben.

Zusätzlich kann `Optimiert testen` gestartet werden. Diese getrennt gespeicherte Variante verwendet
das farbschonende Photoroom-Relighting `ai.preserve-hue-and-saturation` und deaktiviert das Einrasten
angeschnittener Motivseiten, damit Fahrzeuggröße und Position über eine Bildserie konsistenter
bleiben. Sie überschreibt weder das reguläre Ergebnis noch den Export. Da auch das farbschonende
Relighting sichtbare Farb- oder Helligkeitsänderungen erzeugen kann, muss es vor einer produktiven
Aktivierung anhand mehrerer heller, dunkler und farbiger Fahrzeuge geprüft werden.

Der Systemadministrator kann die zusätzlichen Bildvergleiche unter
`Verwaltung > Bilddienstleister` zentral ein- oder ausschalten. Bei ausgeschaltetem Vergleichsmodus sehen
Autohausbenutzer nur Original, reguläres Ergebnis und `Verarbeitung starten`; technische
Dienstleisternamen werden in der Auftragsansicht nicht angezeigt. Original und `Optimiert` können
bei aktivem Vergleichsmodus über das Download-Symbol mit einem eindeutigen Dateinamen geladen
werden.

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
