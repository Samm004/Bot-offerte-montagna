# Bot offerte montagna — guida all'installazione

Il bot gira gratis su GitHub ogni 15 minuti, anche a PC spento, e vi scrive su Telegram.
Servono circa 15 minuti, da fare una volta sola.

---

## 1. Crea il bot Telegram (3 minuti)

1. Su Telegram cerca **@BotFather** e scrivigli `/newbot`.
2. Dagli un nome (es. `Offerte Montagna`) e uno username che finisca in `bot` (es. `offerte_montagna_sam_bot`).
3. BotFather ti risponde con un **token** tipo `7412345678:AAH...`. Copialo e **non condividerlo** con nessuno.

## 2. Crea il gruppo con il tuo amico

1. Su Telegram crea un **nuovo gruppo** con il tuo amico e aggiungi il bot appena creato.
2. Nel gruppo scrivi `/start`.
3. Apri nel browser questo indirizzo, sostituendo `TOKEN` con il tuo token:
   `https://api.telegram.org/botTOKEN/getUpdates`
4. Cerca `"chat":{"id":-100...` e copia il numero **con il segno meno**. È il **chat id** del gruppo.
   (Se la pagina è vuota, scrivi un altro messaggio nel gruppo e ricarica.)

## 3. Crea il repository su GitHub

1. Registrati su https://github.com e clicca **New repository** (il "+" in alto a destra).
2. Nome: `bot-offerte`. Scegli **Public** e premi **Create repository**.
   > Pubblico perché per i repository pubblici GitHub Actions è gratis senza limiti. Con uno privato
   > i minuti gratuiti (2000/mese) non bastano per un controllo ogni 15 minuti. Nel repository si vede
   > solo la lista dei prodotti: token e chat id restano segreti (punto 4).
3. Nella pagina del repository clicca **uploading an existing file** e trascina questi file:
   `bot.py`, `config.yaml`, `requirements.txt`, `LEGGIMI.md`. Poi premi **Commit changes**.
4. Il file del workflow va creato a mano, perché le cartelle che iniziano col punto a volte non si caricano:
   **Add file → Create new file**, come nome scrivi esattamente
   `.github/workflows/monitor.yml`
   e incolla dentro il contenuto del file `monitor.yml`. Poi premi **Commit changes**.

## 4. Inserisci i segreti

Nel repository: **Settings → Secrets and variables → Actions → New repository secret**, e crea:

| Nome               | Valore                       |
|--------------------|------------------------------|
| `TELEGRAM_TOKEN`   | il token di BotFather        |
| `TELEGRAM_CHAT_ID` | il numero del gruppo (con -) |

## 5. Accendi e prova

1. Vai nella scheda **Actions**. Se te lo chiede, premi **I understand my workflows, go ahead and enable them**.
2. Clicca **Monitor offerte → Run workflow**, scegli `test-telegram` e premi **Run workflow**.
   Nel gruppo deve arrivare "✅ Il bot delle offerte è collegato a questa chat!".
3. Rifallo scegliendo `normale`: il primo giro dura circa 5 minuti e manda il messaggio di avvio,
   le offerte trovate e il riepilogo.

Da qui in poi il bot parte da solo ogni 15 minuti.

---

## Cosa riceverete

- **🔥 Offerta**: un articolo nella vostra taglia, disponibile, sotto la soglia. **🔥🔥** se è sotto il `prezzo_top`.
  Lo stesso articolo viene risegnalato solo se il prezzo scende ancora, o se torna disponibile dopo essere finito.
- **☀️ Riepilogo** ogni mattina alle 9: i prezzi migliori del momento per ogni articolo, anche sopra soglia.
  Se non arriva, il bot è fermo.
- **⚠️ Negozio non funzionante**: un sito blocca il bot o ha cambiato struttura. Il bot non resta zitto:
  vi avvisa che su quel negozio non vede niente, così sapete di doverlo controllare a mano.

## Modificare prodotti, taglie e prezzi

Aprite `config.yaml` su GitHub, cliccate la matita ✏️, modificate e premete **Commit changes**.
Al giro successivo il bot rifà le ricerche. Ogni campo è spiegato nei commenti del file.

## Limiti da sapere

- Il bot **avvisa, non compra**.
- **Sportler** non è incluso perché blocca i bot. **Amazon** usatelo con Keepa o CamelCamelCamel.
- **Alpinstore** non dice quali taglie ha in magazzino: per quel negozio l'avviso dice "taglia da verificare".
- GitHub può ritardare i giri di qualche minuto nelle ore di punta, e alcuni siti potrebbero bloccare
  i server di GitHub. In quel caso arriva l'avviso ⚠️.
- Per i gusci "di qualsiasi marca" il bot controlla una lista di modelli di fascia simile al Crater IV
  (Mammut, Patagonia, Rab, Mountain Equipment, Salewa, Arc'teryx, Norrøna, Ortovox). Se vi interessa
  un altro modello, aggiungetelo in `config.yaml`.
