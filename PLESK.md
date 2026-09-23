# Deploy su Plesk (Docker)

Questa repo è la versione containerizzata di [mcpserver](https://github.com/cascinanet/mcpserver),
pensata per essere pubblicata come dominio/sottodominio Plesk con il container gestito
dall'estensione **Docker**.

## Prerequisiti

- Estensione **Docker** installata in Plesk.
- Un dominio o sottodominio dedicato (es. `mcp.tuodominio.it`).
- Server con almeno 2 GB di RAM (ogni subprocess MCP pesa ~100-150 MB; con più server
  configurati contemporaneamente conviene avere margine, eventualmente swap).

## 1. Build & avvio del container

Dall'estensione Docker di Plesk (o via SSH nella cartella del progetto):

```bash
git clone https://github.com/cascinanet/mcpserver_docker.git
cd mcpserver_docker
cp .env.example .env   # imposta almeno SECRET_KEY (vedi sotto)
docker compose up -d --build
```

Variabili minime da impostare in `.env` (lette da `docker-compose.yml`):

- `SECRET_KEY` — genera con `python3 -c "import secrets;print(secrets.token_hex(32))"`
- `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` — credenziali del primo admin
  (cambiale subito dopo il primo login da **Cambia password**)

Il container si chiama `mcphub` ed è esposto **solo su localhost**, porta `127.0.0.1:8070`
(sovrascrivibile con `MCPHUB_PORT` in `.env`); il traffico esterno passa sempre dal reverse
proxy nginx di Plesk davanti al dominio.

## 2. Persistenza dati

`DATA_DIR=/data` dentro il container è montato sulla cartella dell'host
`/var/lib/mcphub-data` (sovrascrivibile con `MCPHUB_DATA_DIR` in `.env`). Contiene
`servers.json`, `users.json`, le credenziali per-server (`creds/<id>.json`) e `runtime.json`.
Essendo una cartella dell'host, sopravvive a rebuild e redeploy e non dipende dal percorso
da cui lanci `docker compose`. Backup:

```bash
tar czf /root/mcphub-data-backup-$(date +%F).tgz -C /var/lib mcphub-data
```

## 3. Collegare il dominio Plesk (Docker Proxy Rules)

In Plesk: **Domini → mcp.tuodominio.it → Docker (o "Proxy Rules" nell'estensione Docker)**:

- Container target: `mcphub` sulla porta `8070` (lato host)
- Abilita **SSL/TLS** con Let's Encrypt dal pannello dominio (niente certbot manuale:
  lo gestisce Plesk).

### Direttive nginx aggiuntive (fondamentali per SSE/Streamable HTTP)

Il connettore MCP di claude.ai usa Streamable HTTP/SSE: senza queste direttive il
proxy fa buffering e taglia le connessioni lunghe. In Plesk vai su
**Domini → mcp.tuodominio.it → Impostazioni Apache & nginx → Direttive nginx aggiuntive**
e incolla:

```
proxy_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

## 4. Primo accesso

Apri `https://mcp.tuodominio.it` → login con le credenziali bootstrap → **cambia
subito la password** da `/account/password`.

Da qui in poi la configurazione (aggiunta server MCP, credenziali service account,
token) si fa dall'admin UI come descritto nel [README](README.md).

## Aggiornare il codice

Se il repository è collegato da **Plesk → Git**: clicca **Estrai ora**, poi via SSH (root)
nella cartella del deploy:

```bash
docker compose up -d --build
```

Altrimenti `git pull` al posto di "Estrai ora". La cartella dati non viene toccata dal rebuild.

**Prima volta con compose** su un'installazione avviata a mano con `docker run`: compose non
può riusare un container che non ha creato. Rimuovilo prima (i dati restano sull'host):

```bash
docker rm -f mcphub
docker compose up -d --build
```

## Note

- **1 solo container/worker**: sessioni SSE e pool di processi MCP sono in memoria,
  non scalare orizzontalmente senza prima introdurre uno store condiviso (Redis).
- Per aggiungere nuovi server MCP via pip, modifica `requirements.txt` e rifai la build.
