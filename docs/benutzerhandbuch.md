# SQL Transfer: Benutzerhandbuch

*Also available in English: [user-guide.md](user-guide.md)*

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

Die App sucht alle vier Stunden selbst nach einer neuen Version. Gibt es eine, erscheint ein Fenster (auf Englisch, wie die App) mit "Install Update", "Remind Me Later" und "Skip This Version". Nach "Install Update" lädt die App die neue Version, startet neu und ist aktuell. Mit dem Häkchen "Automatically download and install updates in the future" kommen künftige Updates ohne Nachfrage.

Sofort nachsehen kannst du jederzeit: im Menü **SQL Transfer** der Eintrag **Check for Updates…**, direkt unter "About SQL Transfer". Gibt es nichts Neues, sagt die App das auch.

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

Jede Zeile trägt Marker: **local** in Grün für Datenbanken auf deinem Rechner, **remote** in Rot für alle anderen, dazu **SSH** und die Verschlüsselung, falls sie von **Automatic** abweicht. Das Stiftsymbol öffnet das Profil zum Bearbeiten, der Papierkorb löscht es nach einer Rückfrage.

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

Die Einträge sind gruppiert: erst **On this machine** mit Laptop-Symbol, darunter **Elsewhere** mit Wolkensymbol. Die Überschriften lassen sich nicht auswählen. In beide Felder kannst du tippen, die Liste filtert dann mit, was bei vielen Profilen schneller ist als scrollen. Tippst du etwas, das auf kein Profil passt, und wählst nichts aus, bleibt die vorherige Auswahl aktiv.

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

## Personendaten anonymisieren

Im Bereich **Transfer** steht der Schalter **Anonymize personal data**. Er ist an, solange es aktive Regeln gibt. Beim Übertragen ersetzt die App dann in Spalten mit Personendaten die echten Werte durch erfundene.

**Was ersetzt wird, entscheiden Regeln über Spaltennamen.** Sie stehen unter **Profiles → Anonymization rules**, mit Vorgaben für Deutsch und Englisch (E-Mail, Vor- und Nachname, Telefon, Straße, Ort, PLZ). Ein Muster wie `*mail*` trifft `email`, `Email` und `kunde_email`. Über **New rule** legst du eigene an, der Stift ändert, der Papierkorb löscht, und eine Regel lässt sich abschalten, ohne sie zu verlieren.

**Gleicher Wert ergibt immer denselben Ersatz.** Eine Adresse, die in zwei Tabellen steht, wird in beiden gleich ersetzt, Verknüpfungen bleiben also heil. Eindeutig bleiben allerdings nur die Arten, deren Ersatz den Hash enthält: **E-Mail** und **Generic text**. Namen, Orte und Straßen stammen aus einer Liste und wiederholen sich; auf einer UNIQUE-Spalte nimm deshalb E-Mail oder Generic text. Dafür sorgt ein Zufallswert im Schlüsselbund, der deinen Rechner nie verlässt. `NULL` bleibt `NULL`, Leeres bleibt leer.

**Werte, die ihre Form behalten müssen: das Feld „Keep values matching".** Manche Anwendungen lesen die Bedeutung eines Werts aus seiner Form. Bei Axro erkennt das Plugin AxroCustomer einen Debitor daran, dass seine E-Mail die Form `121550927552002@axro.de` hat, also ERP-Nummer an der Firmendomain. Wird diese Adresse ersetzt, verschwinden in der Administration der Reiter „Kontakte", die Debitoren-Kopfzeile und „Login as customer", ohne jede Fehlermeldung. Trage in der Regel deshalb einen regulären Ausdruck über den **Wert** ein, hier `^[0-9]+@axro\.`: Was darauf passt, bleibt unverändert, alles andere wird ersetzt. Eine Mitarbeiteradresse wie `vorname.nachname@axro.de` passt nicht darauf und wird weiter anonymisiert, denn darin steckt eine Person.

**Auch in JSON wird ersetzt.** Spalten vom Typ `json` werden automatisch durchsucht. Liegt das JSON in einer Textspalte (bei Shopware der Normalfall, etwa `custom_fields`), legst du dafür eine Regel mit der Art **Look inside the JSON** an. Darin greifen deine übrigen Regeln auf die **Schlüsselnamen**: `*mail*` trifft dann den Schlüssel `email` genauso wie eine Spalte `email`. Die Struktur bleibt, nur die Werte ändern sich, und Schlüssel ohne Regel bleiben unberührt.

Drei Grenzen dabei: Werte in **Listen** (`{"positionen":[{"email":...}]}`) werden gemeldet, aber nicht ersetzt. Verschachtelung wird bis zur **vierten Ebene** verfolgt. Und Werte, die **keine Zeichenkette** sind (eine Telefonnummer als Zahl), bleiben stehen, damit die Anwendung keinen anderen Typ zurückbekommt. Alle drei Fälle stehen als WARN im Protokoll.

**Nach dem Einspielen: dein Administrator-Konto.** Kopierst du die Tabelle `user` mit, sind danach die Konten der Quelle im Ziel und dein lokales fehlt. Lege es neu an, sonst kommst du nicht mehr in die Administration:

```
bin/console user:create --admin dein-name
```

**Vorher sehen, was passiert:** **Preview plan** listet die betroffenen Spalten, bevor eine Zeile kopiert wird. Nach dem Lauf steht im Protokoll, was ersetzt wurde. Spalten, die nach Personendaten aussehen, aber keine Regel haben, erscheinen als WARN mit Begründung.

**Grenzen, die du kennen solltest:**

- Nur Textspalten. Eine Spalte `telefon` vom Typ `BIGINT` wird gemeldet, nicht geändert.
- Ist die Spalte zu kurz für den Ersatzwert, wird er abgeschnitten, statt die Übertragung abzubrechen.
- Freitext bleibt, wie er ist. Steht eine Adresse in einem Feld `kommentar`, erkennt das kein Namensabgleich.
- Die echten Daten liegen kurz auf deiner Platte: bei MySQL nur in der Zwischentabelle (die fertige Tabelle sieht sie nie), bei PostgreSQL in der Zieltabelle selbst, bis der Schritt durchgelaufen ist.
- Scheitert das Ersetzen, bricht der Lauf ab. Bei MySQL wird dann **nicht** getauscht, die Zieltabelle behält ihren alten Inhalt.

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

**Die Tabellendefinition wird wiederhergestellt.** Legt die App eine Tabelle neu an oder tauscht sie aus, bringt apitap nur Spalten, Typen und Primärschlüssel mit. Die App setzt danach wieder, was zum Schreiben gehört: **berechnete Spalten** samt ihrer Formel, **Standardwerte**, **CHECK-Bedingungen** und **AUTO_INCREMENT** samt Zählerstand, dazu wie bisher Indizes und Fremdschlüssel. Eine Kopie verhält sich beim Schreiben also wie das Original: `order_date` rechnet wieder mit, ein Einfügen ohne Spalte nimmt den Standardwert, und eine verletzte Bedingung wird abgelehnt.

Scheitert dabei ein Schritt, bricht der Lauf ab und es wird **nicht** getauscht. Lieber die alte Tabelle als eine halb hergestellte.

Grundlage ist das `SHOW CREATE TABLE` der Quelle, also das, was der Server selbst als Definition ausgibt. Prüfbedingungen werden nach dem Tausch gesetzt, weil ihre Namen in MySQL der Datenbank gehören und die alte Tabelle sie bis dahin hält. Eine Bedingung, die sich nicht anwenden lässt, wird gemeldet und kostet nicht die ganze Tabelle. Das gilt für jedes Detail: Was der Server nicht zurücknimmt (etwa einen binären Standardwert, den er selbst anders ausgibt, als er ihn annimmt), steht mit Klausel und Grund im Protokoll, alles andere an der Tabelle wird trotzdem gesetzt.

Nicht eingeholt werden: Trigger, Views, Partitionierung und Spaltenkommentare. **Das Ganze gilt nur für MySQL nach MySQL.** Bei einer PostgreSQL-Quelle oder einem PostgreSQL-Ziel bleibt es beim bisherigen Verhalten, die Kopie verliert diese Eigenschaften und das Protokoll sagt es.

**Sonderbehandlung bei MySQL als Ziel.** Die App überträgt jede Tabelle zuerst in eine Zwischentabelle und tauscht sie am Ende in einem Schritt aus, sodass niemand eine halb gefüllte Tabelle sieht. Verweisen andere Tabellen per Fremdschlüssel auf die Zieltabelle, tauscht die App sie nicht aus, sondern ersetzt nur die Daten darin, denn ein Austausch würde diese Verweise brechen. Das taucht im Protokoll als WARN auf. Das ist kein Fehler, sondern der Hinweis, dass ein Umweg genommen wurde.

**Indizes werden von der Quelle übernommen.** Bei MySQL zu MySQL legt die App im Ziel dieselben Indizes an wie in der Quelle, auch UNIQUE-, FULLTEXT- und Präfix-Indizes. Das geschieht vor dem Austausch, die Tabelle ist also vom ersten Moment an vollständig indiziert. Bei großen Tabellen kostet das spürbar Zeit, im Protokoll steht dann "Building … index(es)". Kommt die Quelle aus PostgreSQL, bekommt die Zieltabelle nur ihren Primärschlüssel.

**Wenn eine Spalte nicht mitkommt.** Tabellen, auf die andere zeigen, werden nicht getauscht, sondern an ihrer Stelle neu gefüllt. Bringt apitap dabei eine Spalte nicht mit, wird die Zeile ohne sie eingefügt und die Spalte behält ihren Standardwert, bei einem nullbaren Fremdschlüssel also NULL in jeder Zeile. Das Protokoll nennt diesen Fall jetzt mit Tabelle und Spalte als WARN. Steht so eine Zeile da, vergleiche die Spalte mit der Quelle, bevor du mit der Kopie arbeitest.

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
