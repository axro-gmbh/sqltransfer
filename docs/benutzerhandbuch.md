# SQL Transfer: Benutzerhandbuch

SQL Transfer kopiert Tabellen aus einer entfernten Datenbank in eine lokale, wahlweise durch einen SSH-Tunnel. Gedacht ist es für den Alltagsfall "ich brauche produktionsnahe Daten auf meiner Maschine", ohne Dumps, ohne Zwischendateien.

## Voraussetzungen

| | |
|---|---|
| Betriebssystem | macOS 12 oder neuer |
| Prozessor | Apple Silicon (M1 und neuer). Auf Intel-Macs läuft die App nicht. |
| Zugang | SSH-Schlüssel für den Sprungserver, Zugangsdaten für Quell- und Zieldatenbank |
| Datenbanken | MySQL und PostgreSQL, jeweils als Quelle und als Ziel |

## Installation

1. ZIP-Datei entpacken (Doppelklick genügt).
2. `sqltransfer.app` nach `/Programme` ziehen.
3. Doppelklick.

Die App ist signiert und von Apple notarisiert. Es erscheint also **keine** Sicherheitswarnung, und es ist kein Rechtsklick auf "Öffnen" nötig. Kommt trotzdem eine Meldung, stimmt etwas mit der heruntergeladenen Datei nicht: dann bitte neu beziehen statt die Warnung wegzuklicken.

Der erste Start dauert etwa 15 bis 20 Sekunden, danach geht es zügig.

## Einmalige Einrichtung

Alles Folgende steht im Bereich **Profiles**. Den brauchst du nur beim Einrichten oder Ändern, im Alltag bleibt er zugeklappt.

### SSH-Profil anlegen

Nötig, wenn die Datenbank nicht direkt von deinem Rechner aus erreichbar ist, was bei uns der Normalfall ist.

1. **Profiles**, dann **SSH profiles** aufklappen.
2. Ausfüllen: Profilname, Host, Port (Vorgabe 22), Benutzername, Pfad zum privaten Schlüssel.
3. Passphrase nur eintragen, wenn dein Schlüssel eine hat. Das Feld bleibt beim späteren Laden leer, das ist Absicht: Das Geheimnis liegt im Schlüsselbund und wird nicht angezeigt. Leer lassen heißt "beibehalten".
4. **Test SSH connection** drücken. Erst wenn das grün meldet, weiter.
5. **Save SSH profile**.

### Datenbankprofile anlegen

Du brauchst zwei: eine Quelle und ein Ziel.

1. **Profiles**, dann **Database profiles** aufklappen.
2. **Role** wählen:
   - **Source (remote)** für die Datenbank, aus der gelesen wird.
   - **Destination (local)** für die, in die geschrieben wird.
3. **Database** wählen: MySQL oder PostgreSQL.
4. Host, Port, Datenbankname, Benutzername, Passwort eintragen.
5. Wenn ein Tunnel nötig ist: **Use SSH tunnel** ankreuzen und das SSH-Profil auswählen.
6. **Test connection** prüft die Verbindung, **Test through tunnel** prüft zusätzlich den Weg über den Sprungserver.
7. **Save DB profile**.

> **Wichtig bei Tunnelbetrieb:** Host und Port sind die Adressen, unter denen die Datenbank **vom SSH-Server aus** erreichbar ist, nicht von deinem Rechner. Das ist dieselbe Logik wie in DataGrip. Häufig ist das `127.0.0.1` oder ein interner Hostname, der von außen gar nicht auflösbar wäre.

Passwörter und Passphrasen landen im **macOS-Schlüsselbund**, nicht in einer Datei der App. Löschst du ein Profil, wird der zugehörige Schlüsselbund-Eintrag mitgelöscht. Deshalb fragt die App vor dem Löschen nach.

## Eine Übertragung durchführen

### 1. Quelle und Ziel wählen

Oben im Bereich **Transfer** die beiden Auswahlfelder füllen. Links steht die Quelle, rechts das Ziel. Angeboten werden nur Profile mit der jeweils passenden Rolle.

Mit **Test source** und **Test destination** prüfst du beide Verbindungen, bevor es losgeht. Das kostet ein paar Sekunden und erspart abgebrochene Übertragungen.

### 2. Festlegen, was übertragen wird

Die Frage "was soll kopiert werden" hat genau drei Antworten:

| Auswahl | Bedeutung |
|---|---|
| **One table** | Eine einzelne Tabelle. Name eintippen oder aus der Liste wählen. |
| **Selected tables** | Mehrere Tabellen aus einer Liste zum Ankreuzen. |
| **Whole database** | Das ganze Schema beziehungsweise die ganze Datenbank. |

Für die ersten beiden Varianten zuerst **Load tables** drücken, dann liest die App die Tabellenliste aus der Quelle. Das Feld bei **One table** ist durchsuchbar: einfach tippen, die Liste filtert mit. Bei **Selected tables** helfen **Select all** und **Clear**, und darüber steht jederzeit, wie viele von wie vielen angekreuzt sind.

Unter **Advanced** liegen zwei Einstellungen, die man selten braucht:

- **Parallel pipes**: wie viele Übertragungen gleichzeitig laufen. Läuft die Verbindung über einen SSH-Tunnel, setzt die App das automatisch auf 1, weil ein Tunnel sonst an seine Kanalgrenze stößt. Höher setzen nur, wenn du weißt, warum.
- **Source schema hint**: das Schema der Quelle, bei PostgreSQL üblicherweise `public`. Wird beim Laden der Tabellenliste und bei "Whole database" verwendet.

### 3. Vorschau ansehen

**Preview plan** überträgt noch nichts. Es zeigt im Protokoll, was passieren würde: Quelle, Ziel, Umfang, Parallelität, und ob Sonderbehandlungen greifen. Bei größeren Übertragungen lohnt sich der Blick immer.

### 4. Starten und verfolgen

**Run transfer** startet. Während es läuft:

- Der Fortschrittsbalken zeigt, bei welcher Tabelle von wie vielen die App gerade ist.
- Das Protokoll füllt sich fortlaufend und scrollt mit.
- Alle anderen Knöpfe sind gesperrt, damit nichts dazwischenfunkt.
- **Cancel** bricht ab. Der Abbruch greift nach der **aktuell laufenden Tabelle**, nicht mitten in ihr. Bei einer sehr großen Einzeltabelle kann das dauern.

Am Ende steht in der Statuszeile, wie viele Zeilen in welcher Zeit übertragen wurden, und der Lauf erscheint im Verlauf rechts.

## Das Protokoll lesen

Jede Zeile trägt eine Uhrzeit und eine Einstufung, farblich unterschieden:

| Stufe | Bedeutung |
|---|---|
| **INFO** | normaler Ablauf |
| **WARN** | Sonderbehandlung hat gegriffen, der Lauf geht weiter |
| **ERROR** | der Lauf ist gescheitert |

**Copy** legt das gesamte Protokoll in die Zwischenablage, praktisch für Rückfragen im Team. **Clear** leert die Anzeige, die Übertragung selbst bleibt davon unberührt.

## Verlauf

Rechts stehen die letzten 20 Läufe mit Status, Quelle, Ziel, Umfang, Zeilenzahl, Dauer und Zeitpunkt in Ortszeit. Über das Pfeilsymbol an einem Eintrag lädst du dessen Umfang zurück ins Formular, um denselben Lauf zu wiederholen, ohne alles neu zusammenzuklicken.

## Wichtig zu wissen

**Die Zieltabelle wird ersetzt, nicht ergänzt.** Eine Übertragung überschreibt die Tabelle im Ziel. Sie hängt keine Zeilen an und gleicht nichts ab. Deshalb gilt: Ziel ist deine lokale Entwicklungsdatenbank, niemals etwas, dessen Inhalt jemand braucht.

**Sonderbehandlung bei MySQL als Ziel.** MySQL begrenzt Tabellennamen auf 64 Zeichen, und die Zwischentabelle beim Übertragen braucht davon einen Teil. Bei langen Namen weicht die App automatisch auf kurze Zwischennamen aus und benennt am Ende um, sodass der endgültige Name stimmt. Hängen an einer Tabelle Fremdschlüssel anderer Tabellen, tauscht die App nicht die Tabelle aus, sondern ersetzt die Daten darin. Beides taucht im Protokoll als WARN auf. Das ist kein Fehler, sondern der Hinweis, dass ein Umweg genommen wurde.

**Leere Quelltabellen** werden im Ziel als leere Tabelle angelegt. Fehlen dafür noch Fremdschlüsselziele, holt die App das in einem zweiten Durchgang am Ende nach.

**Abbruch bei anderen Zielen als MySQL.** Dort läuft die Übertragung als ein einziger Vorgang. **Cancel** wird vorgemerkt und im Protokoll bestätigt, wirkt aber erst, wenn der laufende Vorgang von sich aus endet.

## Fehlersuche

| Meldung oder Symptom | Was zu tun ist |
|---|---|
| **Missing: …** unter einem Feld | Pflichtfeld ist leer. Das Feld ist rot markiert. |
| SSH-Test scheitert | Schlüsselpfad prüfen, Passphrase prüfen, Erreichbarkeit des Sprungservers prüfen. |
| Datenbanktest scheitert trotz funktionierendem SSH | Host und Port sind aus Sicht des SSH-Servers einzutragen, nicht aus deiner. |
| Übertragung bricht mit Kanalfehler ab | **Parallel pipes** auf 1 setzen. |
| Tabellenliste bleibt leer | Falsches Schema unter **Source schema hint**, bei PostgreSQL meist `public`. |
| App reagiert scheinbar nicht | Während eines Laufs sind die Knöpfe absichtlich gesperrt. Fortschrittsbalken und Protokoll zeigen, dass es weitergeht. |

Bei allem, was hier nicht steht: Protokoll über **Copy** kopieren und mitschicken. Darin steht fast immer die eigentliche Ursache.

## Wo liegen meine Daten

| Was | Wo |
|---|---|
| Profile und Verlauf | `~/Library/Application Support/sqltransfer/profiles.db` |
| Passwörter und Passphrasen | macOS-Schlüsselbund |

Die App sendet nichts nach außen. Sie spricht ausschließlich mit den Datenbanken und dem SSH-Server, die du selbst eingetragen hast.
