# SplitEase

> Dividi le spese con chi vuoi 💸

**v1.1.0**

- 🔗 **Link sharing** — crea il gruppo e condividi il link via WhatsApp/Telegram/iMessage
- ⚡ **Zero registrazione** — autenticazione automatica via cookie nickname
- 💰 **Inserimento spese** — descrizione + importo + chi ha pagato
- 📊 **Saldi immediati** — chi deve dare, chi deve ricevere, aggiornati in tempo reale
- 📈 **Totale e per persona** — somma totale e quota ciascuno sotto la lista spese
- 🔄 **Settlement ottimizzato** — algoritmo di minimizzazione delle transazioni
- 📎 **Scontrini** — allega o scatta foto dello scontrino per ogni spesa, rimozione automatica sfondo
- 🌗 **Light/Dark mode** — switch con persistenza via localStorage
- 📱 **Mobile-first** — pensato per l'uso dal telefonino al ristorante

## Stack

- **Backend**: Python 3 + Flask
- **Database**: SQLite (WAL mode) + Pillow/numpy per elaborazione immagini
- **Frontend**: HTML + CSS variables + vanilla JS (Jinja2 template)
- **Deploy**: Cloudflare Tunnel

## Avvio locale

```bash
pip install flask
python3 app.py
# Server su http://localhost:5555
```

## Variabili d'ambiente

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `PORT` | 5555 | Porta del server |

## Struttura

```
app.py              # App Flask (routes, template, logica)
splitwise.db        # SQLite (creato automaticamente)
README.md
```

## Come funziona

1. Apri SplitEase → inserisci nome vacanza + il tuo nome (+ prima spesa opzionale)
2. Il gruppo viene creato, sei dentro automaticamente
3. Condividi il link del gruppo con gli amici
4. Ognuno sceglie un nickname e entra
5. Tutti inseriscono spese, i saldi si calcolano in automatico
6. La sezione "Chi paga chi" ti dice esattamente chi deve dare cosa a chi

## Versione

**v1.1.0** — Aggiunti scontrini con rimozione sfondo, totale per persona, migliorato layout spese