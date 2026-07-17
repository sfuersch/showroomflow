# Cloudflare R2 fuer ShowroomFlow

ShowroomFlow verwendet produktiv einen privaten Cloudflare-R2-Bucket mit EU-Zustaendigkeit. Zugangsschluessel liegen nur in der geschuetzten VPS-Konfiguration. Die iOS-App erhaelt spaeter ausschliesslich kurzlebige, auf einen einzelnen Upload begrenzte URLs.

## 1. Bucket anlegen

1. Im Cloudflare-Dashboard `R2 Object Storage` oeffnen und R2 aktivieren.
2. Einen Bucket namens `showroomflow-production` erstellen.
3. Als Datenstandort beziehungsweise Jurisdiction `European Union (EU)` waehlen. Diese Auswahl kann nach dem Erstellen nicht geaendert werden.
4. Die Standardspeicherklasse `Standard` verwenden.
5. Oeffentlichen Zugriff und eine oeffentliche Entwicklungs-URL deaktiviert lassen.

## 2. Aufbewahrung auf 90 Tage begrenzen

In den Bucket-Einstellungen unter `Object lifecycle rules` eine Regel fuer alle Objekte anlegen:

- Name: `delete-after-90-days`
- Prefix: leer, damit die Regel fuer den gesamten Bucket gilt
- Aktion: Objekte nach 90 Tagen loeschen

Die Anwendung entfernt Datensaetze ebenfalls nach der Aufbewahrungsfrist. Die Bucket-Regel ist eine zusaetzliche technische Absicherung fuer Originale und bearbeitete Bilder.

## 3. Begrenzten API-Token erstellen

1. Unter `R2 Object Storage > Overview > Manage API Tokens` einen Account API Token erstellen.
2. Berechtigung `Object Read & Write` waehlen.
3. Den Zugriff auf den Bucket `showroomflow-production` begrenzen.
4. Access Key ID und Secret Access Key unmittelbar in einem Passwortmanager sichern. Der Secret Access Key wird nur einmal angezeigt.

Keinen Admin-Token und keinen kontoweiten Token verwenden. Die Werte niemals in Git committen, in die iOS-App eintragen oder im Chat teilen.

## 4. VPS-Konfiguration

Auf dem VPS folgende Werte in `/opt/showroomflow/.env.production` setzen:

```dotenv
SHOWROOMFLOW_STORAGE_ENDPOINT=https://CLOUDFLARE_ACCOUNT_ID.eu.r2.cloudflarestorage.com
SHOWROOMFLOW_STORAGE_REGION=auto
SHOWROOMFLOW_STORAGE_ACCESS_KEY=R2_ACCESS_KEY_ID
SHOWROOMFLOW_STORAGE_SECRET_KEY=R2_SECRET_ACCESS_KEY
SHOWROOMFLOW_STORAGE_BUCKET=showroomflow-production
SHOWROOMFLOW_RETENTION_DAYS=90
```

Die Grossbuchstaben sind Platzhalter und muessen auf dem VPS ersetzt werden. Die EU-Endpoint-URL steht im Cloudflare-Dashboard bei den S3-API-Angaben des Buckets.

## 5. Upload-Sicherheit

- Der Bucket bleibt privat.
- R2-Zugangsschluessel werden nur vom FastAPI-Backend und den Verarbeitungsworkern verwendet.
- Die iOS-App laedt Bilder spaeter mit kurzlebigen, signierten PUT-URLs hoch.
- Die signierte URL wird an einen konkreten Objektpfad und den erwarteten `Content-Type` gebunden.
- Objektpfade enthalten interne, nicht erratbare IDs und keine Zugangsdaten.
- CORS ist fuer native iOS-Uploads nicht erforderlich. Fuer die spaetere Browser-Nachbearbeitung wird CORS nur fuer `https://showroomflow.promotekk.com` und die benoetigten Methoden aktiviert.

## 6. Inbetriebnahme pruefen

Nach dem Eintragen der Werte wird zuerst die Docker-Konfiguration geprueft. Anschliessend prueft ein geplanter Backend-Speichertest das Schreiben, Lesen und Loeschen eines kleinen Testobjekts. Erst danach werden echte Fahrzeugbilder hochgeladen.
