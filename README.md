# SplitEase

> Dividi le spese con chi vuoi 💸

**v1.0.0**

Mobile-first web app per dividere le spese durante vacanze e weekend con amici. Zero login, zero friction — crei il gruppo, condividi il link, tutti inseriscono le spese.

## Features

- 🔗 **Link sharing** — crea il gruppo e condividi il link via WhatsApp/Telegram/iMessage
- ⚡ **Zero registrazione** — autenticazione automatica via cookie nickname
- 💰 **Inserimento spese** — descrizione + importo + chi ha pagato
- 📊 **Saldi immediati** — chi deve dare, chi deve ricevere, aggiornati in tempo reale
- 🔄 **Settlement ottimizzato** — algoritmo di minimizzazione delle transazioni
- 🌗 **Light/Dark mode** — switch con persistenza via localStorage
- 📱 **Mobile-first** — pensato per l'uso dal telefonino al ristorante
- ✕ **Cancellazione spese** — chi ha pagato può cancellare le proprie
- 📤 **Web Share API** — condivisione Instagram-style su smartphone

## Stack

- **Backend**: Python 3 + Flask
- **Database**: SQLite (WAL mode)
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

**v1.0.0** — Prima release stabile