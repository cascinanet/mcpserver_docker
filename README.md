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
| `custom` | manuale | — | qualsiasi server MCP stdio |

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

> Prerequisiti Google: abilitare le API nel progetto Cloud e dare al service account accesso
> alle proprietà (GA4: Visualizzatore; Search Console: utente). Per GSC multi-sito, tutte le
> proprietà accessibili compaiono in `list_properties` senza riconfigurare nulla.

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
