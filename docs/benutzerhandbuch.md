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

### Updates

Die App sucht einmal am Tag selbst nach einer neuen Version. Gibt es eine, erscheint ein Fenster (auf Englisch, wie die App) mit "Install Update", "Remind Me Later" und "Skip This Version". Nach "Install Update" lädt die App die neue Version, startet neu und ist aktuell. Mit dem Häkchen "Automatically download and install updates in the future" kommen künftige Updates ohne Nachfrage.

Jedes Update ist signiert: Die App installiert nur Versionen, die nachweislich von uns stammen. Ohne Internetverbindung passiert einfach nichts, die Suche wird später wiederholt.

## Einmalige Einrichtung

Alles Folgende steht im Bereich **Profiles**. Den brauchst du nur beim Einrichten oder Ändern, im Alltag bleibt er zugeklappt.

### SSH-Profil anlegen

Nötig, wenn die Datenbank nicht direkt von deinem Rechner aus erreichbar ist, was bei uns der Normalfall ist.

1. **Profiles**, dann **SSH profiles** aufklappen.
2. **New SSH profile** drücken. Es öffnet sich ein Fenster mit leeren Feldern.
3. Ausfüllen: Profilname, Host, Port (Vorgabe 22), Benutzername, Pfad zum privaten Schlüssel.
4. Passphrase nur eintragen, wenn dein Schlüssel eine hat. Beim Bearbeiten bleibt das Feld leer, das ist Absicht: Das Geheimnis liegt im Schlüsselbund und wird nicht angezeigt. Leer lassen heißt "beibehalten".
5. **Test SSH connection** drücken. Erst wenn das grün meldet, weiter.
6. **Save**.

### Datenbankprofile anlegen

Du brauchst mindestens zwei: eine Quelle und ein Ziel. **Jedes Profil kann beides sein**, es gibt keine Festlegung mehr beim Anlegen.

1. **Profiles**, dann **Database profiles** aufklappen.
2. **New database profile** drücken.
3. Profilname vergeben und **Database** wählen: MySQL oder PostgreSQL.
4. Host, Port, Datenbankname, Benutzername, Passwort eintragen.
5. Wenn ein Tunnel nötig ist: **Use SSH tunnel** ankreuzen und das SSH-Profil auswählen.
6. **Encryption** festlegen, siehe unten. Für fast alle Profile ist **Automatic** richtig.
7. **Test connection** meldet sich wirklich an und sagt dir, ob die Verbindung verschlüsselt ist. **Test through tunnel** prüft zusätzlich den Weg über den Sprungserver. Ist das Passwortfeld leer, nimmt der Test das gespeicherte aus dem Schlüsselbund.
8. **Save**.

### Profile finden, ändern, löschen

Jede Liste hat ein Suchfeld. Es filtert nach Name, Host, Datenbank und Benutzer, auch mit mehreren Wörtern ("prod shop" zeigt nur, was beides enthält). Ab etwa fünf Profilen scrollt die Liste, statt die Seite zu verlängern.

Jede Zeile trägt Marker: **local** oder **remote** (rot), dazu **SSH** und die Verschlüsselung, falls sie von **Automatic** abweicht. Das Stiftsymbol öffnet das Profil zum Bearbeiten, der Papierkorb löscht es nach einer Rückfrage.

Bearbeiten und Anlegen sind bewusst getrennt: Im Fensterkopf steht entweder "New database profile" oder "Edit database profile '<name>'". Ein neues Profil mit einem schon vergebenen Namen wird abgelehnt, statt das vorhandene stillschweigend zu überschreiben. Umbenennen geht beim Bearbeiten jederzeit, das Passwort im Schlüsselbund bleibt dabei erhalten.

> **Wichtig bei Tunnelbetrieb:** Host und Port sind die Adressen, unter denen die Datenbank **vom SSH-Server aus** erreichbar ist, nicht von deinem Rechner. Das ist dieselbe Logik wie in DataGrip. Häufig ist das `127.0.0.1` oder ein interner Hostname, der von außen gar nicht auflösbar wäre.

### Verschlüsselung der Datenbankverbindung

| Einstellung | Bedeutung |
|---|---|
| **Automatic** | Aus für `localhost` und für Verbindungen durch einen SSH-Tunnel (der verschlüsselt bereits), verschlüsselt und geprüft für jeden anderen Host |
| **Off** | nie verschlüsselt, nur für Datenbanken auf diesem Rechner oder in einem vertrauenswürdigen Netz |
| **Encrypted, certificate not checked** | verschlüsselt, aber ohne Prüfung, mit wem man spricht. Schützt vor Mitlesen, nicht vor einem gefälschten Server. Für Server mit selbst signiertem Zertifikat, bei MySQL der Normalfall |
| **Encrypted and verified** | verschlüsselt und das Zertifikat samt Hostname geprüft |

Eine eigene Zertifizierungsstelle (Feld **CA certificate**) geht nur bei PostgreSQL. Bei MySQL vertraut die Übertragung nur öffentlich anerkannten Zertifizierungsstellen; für interne Server dort **Encrypted, certificate not checked** wählen.

Passwörter und Passphrasen landen im **macOS-Schlüsselbund**, nicht in einer Datei der App. Löschst du ein Profil, wird der zugehörige Schlüsselbund-Eintrag mitgelöscht. Deshalb fragt die App vor dem Löschen nach.

## Eine Übertragung durchführen

### 1. Quelle und Ziel wählen

Oben im Bereich **Transfer** die beiden Auswahlfelder füllen. Links steht die Quelle, rechts das Ziel. Beide Felder bieten alle Profile an, dasselbe Profil auf beiden Seiten lehnt die App ab.

**Ziele außerhalb deines Rechners sind rot markiert.** Sobald du ein Ziel wählst, das nicht direkt auf `127.0.0.1` oder `localhost` liegt, erscheint unter der Auswahl ein roter Hinweis mit Host und Datenbank. Ein Ziel hinter einem SSH-Tunnel gilt immer als auswärts, auch wenn dort `127.0.0.1` steht: Diese Adresse ist dann das andere Ende des Tunnels.

Beim Start kommt für solche Ziele eine Rückfrage, die Host, Datenbank und den Umfang nennt. Erst der rote Knopf **Overwrite on <host>** startet die Übertragung, **Cancel** bricht ab, ohne irgendetwas zu schreiben. Für Ziele auf deinem Rechner fragt die App nicht.

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

**Sonderbehandlung bei MySQL als Ziel.** Die App überträgt jede Tabelle zuerst in eine Zwischentabelle und tauscht sie am Ende in einem Schritt aus, sodass niemand eine halb gefüllte Tabelle sieht. Verweisen andere Tabellen per Fremdschlüssel auf die Zieltabelle, tauscht die App sie nicht aus, sondern ersetzt nur die Daten darin, denn ein Austausch würde diese Verweise brechen. Das taucht im Protokoll als WARN auf. Das ist kein Fehler, sondern der Hinweis, dass ein Umweg genommen wurde.

**Indizes werden von der Quelle übernommen.** Bei MySQL zu MySQL legt die App im Ziel dieselben Indizes an wie in der Quelle, auch UNIQUE-, FULLTEXT- und Präfix-Indizes. Das geschieht vor dem Austausch, die Tabelle ist also vom ersten Moment an vollständig indiziert. Bei großen Tabellen kostet das spürbar Zeit, im Protokoll steht dann "Building … index(es)". Kommt die Quelle aus PostgreSQL, bekommt die Zieltabelle nur ihren Primärschlüssel.

**Fremdschlüssel werden ebenfalls übernommen**, samt ihrer Regeln wie `ON DELETE CASCADE`. Sie werden erst am Ende des Laufs gesetzt, wenn alle Tabellen da sind, im Protokoll als "Restoring foreign keys". Die vorhandenen Zeilen prüft die App dabei nicht, genau wie ein Datenbank-Import: Tabellen aus einer laufenden Quelle werden Minuten auseinander kopiert und passen deshalb nicht immer auf die Zeile genau zusammen. Fremdschlüssel, die in ein anderes Schema zeigen, übernimmt die App nicht und nennt sie als WARN.

**Leere Quelltabellen** führen zu einer leeren Zieltabelle: Fehlt sie, wird sie angelegt, hat sie noch alte Zeilen, werden diese entfernt. Beides steht als WARN im Protokoll. Legt die App eine leere Tabelle an, deren Fremdschlüsselziele noch fehlen, setzt sie die Fremdschlüssel in einem zweiten Durchgang am Ende.

**Abbruch bei anderen Zielen als MySQL.** Dort läuft die Übertragung als ein einziger Vorgang. **Cancel** wird vorgemerkt und im Protokoll bestätigt, wirkt aber erst, wenn der laufende Vorgang von sich aus endet.

## Fehlersuche

| Meldung oder Symptom | Was zu tun ist |
|---|---|
| **Missing: …** unter einem Feld | Pflichtfeld ist leer. Das Feld ist rot markiert. |
| SSH-Test scheitert | Schlüsselpfad prüfen, Passphrase prüfen, Erreichbarkeit des Sprungservers prüfen. |
| Datenbanktest scheitert trotz funktionierendem SSH | Host und Port sind aus Sicht des SSH-Servers einzutragen, nicht aus deiner. |
| `certificate verify failed` oder `UnknownIssuer` | Der Server hat ein selbst signiertes oder internes Zertifikat. Bei PostgreSQL die CA-Datei eintragen, bei MySQL **Encrypted, certificate not checked** wählen. |
| `SSH host key … does not match` | Der Sprungserver meldet sich mit einem anderen Schlüssel als beim ersten Kontakt. Nicht wegklicken, erst mit dem Betreiber klären. Der Befehl zum Entfernen des alten Eintrags steht in der Meldung. |
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
