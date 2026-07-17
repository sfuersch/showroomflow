# ShowroomFlow

ShowroomFlow ist eine mandantenfaehige Plattform fuer gefuehrte Fahrzeugfotografie. Die iOS-App fuehrt Fotografen durch konfigurierbare Aufnahmen. Das Backend verarbeitet Aussenaufnahmen, verwaltet Hintergruende und Overlays und exportiert die Ergebnisse als VIN-benanntes ZIP per SFTP.

Produktive Adresse: `https://showroomflow.promotekk.com`

## Projektstruktur

- `ios/ShowroomFlow`: SwiftUI-App fuer iOS 17 oder neuer
- `backend`: FastAPI-API und spaetere Verarbeitungsworker
- `docs`: Architektur und fachliche Entscheidungen
- `compose.yaml`: lokale beziehungsweise VPS-nahe Docker-Umgebung
- `compose.production.yaml`: abgeschottete Produktionskonfiguration fuer Plesk
- `ops`: Plesk-nginx- und Sicherungskonfiguration

## Lokaler Start des Backends

1. `.env.example` nach `.env` kopieren und alle Geheimnisse ersetzen.
2. `docker compose up --build` ausfuehren.
3. API-Dokumentation unter `http://localhost:8000/docs` aufrufen.
4. Statuspruefung unter `http://localhost:8000/api/v1/health` aufrufen.

Beim Start werden Datenbankmigrationen ausgefuehrt und der in `.env` konfigurierte erste Systemadministrator einmalig angelegt. Danach sollte dessen Bootstrap-Passwort aus der Serverkonfiguration entfernt werden.

Die Authentifizierung verwendet kurzlebige Zugriffstoken und rotierende Sitzungstoken. Benutzer werden ueber `/api/v1/admin/users` verwaltet. Autohausadministratoren sehen und bearbeiten dabei ausschliesslich Benutzer ihres eigenen Autohauses.

Die Beispielkonfiguration ist nur fuer die lokale Entwicklung bestimmt. Produktive SFTP-, KI- und Zugangsdaten werden nicht in Git gespeichert.

Die produktionsnahe Installation auf Plesk ist in `docs/deployment-plesk.md` beschrieben.

## iOS-App

`ios/ShowroomFlow/ShowroomFlow.xcodeproj` in Xcode oeffnen. Das Projekt verwendet die Bundle-ID `com.promotekk.fotoapp` und hat iOS 17 als Mindestversion. Debug-Builds sprechen lokal mit `http://localhost:8000`, Release-Builds mit `https://showroomflow.promotekk.com`.

## Naechste Meilensteine

1. Anmeldung, Mandanten und Benutzerverwaltung
2. konfigurierbare Aufnahme- und Exportvorlagen
3. Kamera, VIN-Texterkennung und Aufnahmequalitaet
4. Upload, KI-Anbieteradapter und Nachbearbeitung
5. ZIP-Erstellung, SFTP-Export und Wiederholung von Exporten
6. Admin-Oberflaeche und automatische 90-Tage-Loeschung
