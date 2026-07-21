# MCP Hub (Docker)

> Fork containerizzato di [cascinanet/mcpserver](https://github.com/cascinanet/mcpserver),
> pensato per il deploy tramite Docker su **Plesk**. Per le istruzioni di deploy vedi
> **[PLESK.md](PLESK.md)**.

Piattaforma **FastAPI** che fa da hub per più server **MCP**: admin UI protetta da login,
endpoint **Streamable HTTP** (compatibile con i connettori web di claude.ai), e ogni server MCP
avviato come subprocess (transport stdio) e ponte verso il client.

Server integrati:
- **Google Analytics** — [`analytics-mcp`](https://github.com/googleanalytics/google-analytics-mcp) (GA4 Admin + Data API)
- **Google Search Console** — [`mcp-search-console`](https://github.com/AminForou/mcp-gsc) (multi-proprietà)
- **WooCommerce** — server custom incluso nel repo, reporting e-commerce in tempo reale (vendite, prodotti, coupon, clienti, ordini)

## Architettura

```
Client MCP (claude.ai, Claude Code, …)
   │  HTTPS
   ▼
nginx (TLS, reverse proxy, SSE-friendly)
   │  127.0.0.1:8000
   ▼
MCP Hub (FastAPI + gunicorn/uvicorn, 1 worker)
   ├─ Streamable HTTP:  POST/GET/DELETE /mcp/{id}   (Mcp-Session-Id)   ← usato da claude.ai
   ├─ HTTP+SSE legacy:  /mcp/{id}/sse + /messages    (deprecato)
   ├─ Admin UI:         / (dashboard, CRUD server, cambia password, toggle log)
   └─ pool di processi "caldi" pre-avviati per server
            │  stdio (JSON-RPC newline-delimited)
            ▼
      subprocess MCP (analytics-mcp, mcp-search-console, …)
```

- **Transport**: claude.ai usa **Streamable HTTP** (singolo endpoint `/mcp/{id}`, no `/sse`). La
  risposta è negoziata (SSE se il client lo accetta) con `Content-Type: text/event-stream` (senza
  charset) e `Mcp-Session-Id`. Mantenuto anche il vecchio HTTP+SSE per retrocompatibilità.
- **CORS**: abilitato (`Access-Control-Allow-Origin: *`, preflight OPTIONS, `Mcp-Session-Id` esposto)
  perché i connettori web girano nel browser.
- **Pool caldo**: i subprocess MCP sono lenti ad avviarsi (import Python/gRPC). Si pre-avviano N
  processi per server (`POOL_SIZE`) così l'`initialize` dei client è immediato e non scatta il
  timeout di claude.ai.
- **Auth admin**: login locale, password `scrypt` (stdlib), sessione su cookie firmato. Pagina
  **Cambia password** in `/account/password`.
- **Storage**: file JSON in `DATA_DIR` (nessun database).
- **Credenziali per-server**: incollate dall'admin UI, salvate in `DATA_DIR/creds/<id>.json`
  (chmod 600, fuori da git), collegate alla variabile d'ambiente del tipo (es.
  `GOOGLE_APPLICATION_CREDENTIALS` per Analytics, `GSC_CREDENTIALS_PATH` per Search Console).

## Tipi di server (catalogo)

Definiti in [app/mcp/catalog.py](app/mcp/catalog.py). Aggiungere un tipo = aggiungere una voce.

| Tipo | Comando | Credenziali (env) | Note |
|------|---------|-------------------|------|
| `google_analytics` | `analytics-mcp` | `GOOGLE_APPLICATION_CREDENTIALS` | una proprietà via `property_id` nei tool |
| `google_search_console` | `mcp-search-console` | `GSC_CREDENTIALS_PATH` (+ `GSC_SKIP_OAUTH=true`) | **multi-proprietà**: `site_url` per chiamata |
| `sqlite` | `mcp-server-sqlite` | — | DB SQLite in chiaro sotto `DATA_DIR/db/` |
| `sqlite_encrypted` | `python3 -m app.mcp_servers.sqlcipher_server` | — | DB cifrato **SQLCipher**: passphrase `key` per tool call (vedi sotto) |
| `woocommerce` | `python3 -m app.mcp_servers.woocommerce_server` | `WC_SITE_URL`, `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET` (+ opzionali `WC_APP_USER`/`WC_APP_PASSWORD`) | vedi sotto |
| `custom` | manuale | — | qualsiasi server MCP stdio |

### SQLite cifrato (SQLCipher)

Server MCP custom incluso nel repo ([app/mcp_servers/sqlcipher_server.py](app/mcp_servers/sqlcipher_server.py))
per database SQLite **cifrati a riposo** con SQLCipher. Espone gli stessi tool del tipo `sqlite`
(`read_query`, `write_query`, `create_table`, `list_tables`, `describe_table`) ma ognuno richiede
un parametro obbligatorio **`key`** (la passphrase).

- **La chiave non è mai memorizzata**: niente env, niente file, niente cache. Va passata a ogni
  tool call, è usata solo per la durata della singola richiesta (connect → `PRAGMA key` → verifica
  su `sqlite_master` → operazione → close) e poi scartata.
- **Mai nei log**: il campo `key` è mascherato (`***`) nel logging diagnostico dei body dell'hub
  e nei messaggi d'errore del server.
- **Errori generici**: chiave assente/errata → *"chiave mancante o non valida"*, senza dettagli
  sul file o sulla struttura. Un file esistente in chiaro (non cifrato) viene rifiutato.
- **Modello di minaccia**: protegge il file DB **a riposo** (su disco e nei backup scaricati). La
  chiave transita comunque in chiaro da client→hub (protetta dal TLS) e l'hub la vede in memoria
  per l'istante della richiesta: non protegge dall'operatore dell'hub.
- I pulsanti Scarica/Ripristina/Gestisci backup funzionano anche qui (il file scaricato è cifrato).
- *"Testa connessione"* verifica solo che il processo parta, non la passphrase (per design non nota).

### WooCommerce (report e-commerce)

Server MCP custom incluso nel repo ([app/mcp_servers/woocommerce_server.py](app/mcp_servers/woocommerce_server.py)),
alternativa stabile al connettore MCP nativo di WooCommerce (10.9+): quest'ultimo copre solo
CRUD prodotti/ordini, zero report aggregati. Questo server avvolge invece le due API di
reporting già stabili di WooCommerce, non in preview:

| Namespace | Auth | Copre |
|-----------|------|-------|
| `wc/v3` | consumer key/secret | vendite totali, top seller per quantità, elenco ordini |
| `wc-analytics` | Application Password WordPress (utente Amministratore) | fatturato per prodotto, coupon dettagliati, spesa per cliente — la stessa fonte dati del pannello WooCommerce → Analytics |

Tool esposti: `panoramica_vendite`, `top_prodotti`, `report_coupon`, `andamento_temporale`,
`report_clienti`, `elenco_ordini`, `dettaglio_prodotto`, `ricerca_prodotti`, `catalogo_prodotti`,
`report_categorie`, `dettaglio_ordine`, `clienti_nuovi_vs_ricorrenti`, `confronto_periodi`.

- **Due credenziali separate**: `WC_CONSUMER_KEY`/`WC_CONSUMER_SECRET` bastano per
  `panoramica_vendite`, `elenco_ordini`, `dettaglio_prodotto`, `ricerca_prodotti`,
  `catalogo_prodotti`, `dettaglio_ordine` e `confronto_periodi`. Gli altri tool, senza anche
  `WC_APP_USER`/`WC_APP_PASSWORD`, tornano un dato ridotto (es. solo quantità venduta, non
  fatturato) con scritto chiaramente cosa manca, invece di un errore criptico.
- **Nessun feature flag da abilitare** sul sito: entrambe le API sono attive di default,
  indipendentemente dal supporto MCP nativo di WooCommerce.
- **Privacy**: `report_clienti` espone nome ed email dei clienti. Usa una Application Password
  con permessi minimi e valuta se serve davvero il dettaglio cliente o solo gli aggregati.
- **Campi ACF**: `catalogo_prodotti` include un campo `acf` per prodotto, ma `wc/v3/products`
  non lo espone di default. Se il sito non ha già un filtro che lo aggiunge, il tool torna
  `acf_disponibile: false` e una `nota_acf` con lo snippet PHP da aggiungere (stesso pattern
  del Code Snippets già usato per il tracking Matomo).
- Prima di fidarsi dei numeri, testa su un intervallo già noto e confronta con un export CSV:
  `wc-analytics` è un'API che WooCommerce cambia spesso.

## Struttura

```
app/
  main.py            # app factory, middleware (CORS, log opzionale), lifespan (prewarm)
  config.py          # Settings (env / .env)
  runtime.py         # flag runtime (toggle logging) persistito
  models.py          # MCPServer, User
  templating.py      # Jinja2
  storage/store.py   # persistenza JSON + credenziali per-server
  auth/              # security (scrypt), routes (login/logout), dependencies
  mcp/
    catalog.py       # tipi di server (template)
    session.py       # subprocess stdio (limit 32MB), correlazione richiesta/risposta
    manager.py       # sessioni attive + pool di processi caldi (POOL_SIZE)
    routes.py        # Streamable HTTP + SSE legacy
  admin/routes.py    # dashboard, CRUD server, cambia password, toggle/visualizza log
  templates/         # base, login, dashboard, server_form, change_password, logs
DATA_DIR/
  servers.json       # config server
  users.json         # utenti admin (gitignored)
  creds/<id>.json    # credenziali service account per server (gitignored)
  runtime.json       # flag runtime
```

## Sviluppo locale

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # imposta almeno SECRET_KEY
uvicorn app.main:app --reload
```

Apri http://localhost:8000 → login con le credenziali bootstrap (`BOOTSTRAP_ADMIN_*`).

## Aggiungere un server dall'admin UI

Dashboard → **+ Nuovo server** → scegli il **Tipo** (i campi tecnici si pre-compilano) →
incolla il **JSON del service account** → imposta un **Auth token** (URL-safe!) → Salva.
Endpoint risultante: `/mcp/<id>` (Streamable HTTP).

### Connettere claude.ai
Connettore personalizzato con URL (**senza `/sse`**, token URL-safe):
```
https://mcp.cascinanet.it/mcp/<id>?token=<TOKEN>
```

Prima di collegare un client esterno, usa il pulsante **Testa connessione** nella pagina di
modifica del server: esegue l'handshake MCP (`initialize`) internamente e mostra subito se il
comando parte, senza dover scoprire un problema di configurazione tramite Claude. Lo stato del
processo (in esecuzione / crash / mai avviato, con ultimo errore) è visibile anche nella colonna
**Processo** della dashboard.

> Prerequisiti Google: abilitare le API nel progetto Cloud e dare al service account accesso
> alle proprietà (GA4: Visualizzatore; Search Console: utente). Per GSC multi-sito, tutte le
> proprietà accessibili compaiono in `list_properties` senza riconfigurare nulla.

### Checklist per un nuovo servizio Google (GA4 / Search Console)

1. **Abilita l'API specifica** nel progetto Google Cloud del service account (Analytics Data
   API / Analytics Admin API per GA4, Search Console API per GSC — sono API distinte, vanno
   abilitate entrambe se servono entrambi i servizi).
2. **Concedi l'accesso al service account nel prodotto Google**, non solo su Cloud Console:
   la procedura è diversa tra i due —
   - **GA4**: aggiungi l'email del service account come utente **Visualizzatore** nella
     proprietà, da Google Analytics → Amministrazione → Accesso alla proprietà.
   - **Search Console**: aggiungi l'email del service account come **utente** della proprietà
     in Search Console → Impostazioni → Utenti e autorizzazioni.
3. **Genera il JSON della chiave** del service account e incollalo nel form (sezione
   Credenziali) — l'hub lo salva in `DATA_DIR/creds/<id>.json` e collega automaticamente la
   variabile d'ambiente giusta (`GOOGLE_APPLICATION_CREDENTIALS` o `GSC_CREDENTIALS_PATH`).
4. **Verifica comando/env** nella sezione "Impostazioni tecniche": per i tipi da catalogo sono
   già precompilati, controllali solo se hai personalizzato qualcosa.
5. **Testa la connessione** (pulsante nel form) prima di collegare un client esterno.

### Nota sulla discovery OAuth lato client

I client MCP (incluso Claude) tentano automaticamente la sequenza di discovery OAuth
(`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`,
`POST /register`) dopo qualunque errore di connessione — anche quando l'errore non ha nulla a
che fare con l'autenticazione. mcphub non implementa OAuth (l'auth è un token statico via
`?token=` o header `Authorization: Bearer`), quindi questi tentativi falliscono sempre con 404:
è un comportamento imposto dal client, atteso e innocuo, non un sintomo di un problema di
configurazione dell'hub.

## Deploy in produzione (AWS Lightsail, Ubuntu 22.04)

Istanza Lightsail (consigliato ≥2 GB RAM se più server), IP statico, firewall porte 22/80/443.

```
/opt/mcphub/
  venv/        # virtualenv principale (Python 3.10): hub + analytics-mcp
  venv311/     # virtualenv Python 3.11: mcp-search-console (richiede >=3.11)
  app/  .env  data/
```

- **Servizio**: systemd `mcphub` → `gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 1
  -b 127.0.0.1:8000 --timeout 600`. `Environment=PATH` include `venv311/bin` e `venv/bin`.
- **Reverse proxy**: nginx → `127.0.0.1:8000` con `proxy_buffering off`, `proxy_read_timeout 3600s`
  (necessario per SSE/streaming).
- **TLS**: `certbot --nginx -d mcp.cascinanet.it` (rinnovo automatico).
- **Swap**: file di swap (es. 2 GB) per assorbire i picchi di memoria dei subprocess.
- **DNS**: record A `mcp` → IP statico (gestito su WIDhost per cascinanet.it).

### Aggiornare il codice
Caricare `app/` via scp ed estrarre in `/opt/mcphub`, poi `sudo systemctl restart mcphub`.
(`data/` e `.env` non vanno sovrascritti.)

## Note operative / lezioni apprese

- **1 worker**: le sessioni SSE e il pool sono in memoria → un solo worker gunicorn. Per scalare
  orizzontalmente servirebbe uno store di sessioni condiviso (Redis) — TODO.
- **claude.ai e il timeout**: il connettore web va in errore se l'`initialize` è lento. Serve CPU
  adeguata + pool caldo (vedi `POOL_SIZE` in `manager.py`).
- **Risposte grandi**: i messaggi MCP sono JSON su una riga; il lettore stdio asyncio ha un limite
  default di 64 KB → alzato a 32 MB in `session.py` (`create_subprocess_exec(..., limit=...)`),
  altrimenti i report grandi rompono la sessione.
- **Memoria**: ogni subprocess MCP pesa ~100–150 MB. Su istanze piccole tenere `POOL_SIZE` basso
  (1) e usare swap, o salire di taglia.
- **Token**: solo URL-safe (niente `%`, `£`, `^`). Generare con
  `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
```
