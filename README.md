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
| `linkedin` | `python3 -m app.mcp_servers.linkedin_server` | `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_ORG_ID` + autorizzazione OAuth (vedi sotto) | pubblica/gestisce post sulla Pagina aziendale |
| `meta` | `python3 -m app.mcp_servers.meta_server` | `META_APP_ID`, `META_APP_SECRET` + autorizzazione OAuth (vedi sotto) | pubblica/gestisce post su Pagine Facebook e account Instagram Business collegati |
| `google_ads` | `python3 -m app.mcp_servers.google_ads_server` | `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_CUSTOMER_ID` + autorizzazione OAuth (vedi sotto) | report e gestione campagne (pausa/attiva, budget) su un account Google Ads |
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

### LinkedIn (pubblica e gestisci post)

Server MCP custom incluso nel repo ([app/mcp_servers/linkedin_server.py](app/mcp_servers/linkedin_server.py))
per creare/elencare/eliminare post sulla Pagina aziendale LinkedIn tramite la **Community
Management API**. A differenza degli altri tipi, l'autenticazione non è una credenziale
statica ma **OAuth 2.0**: serve un'app LinkedIn Developer approvata e un consenso una tantum
dal pannello admin.

**Setup lato LinkedIn** (esterno all'hub, richiede tempo):
1. Crea un'app su [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps),
   collegata alla Pagina aziendale (devi esserne amministratore).
2. Nella scheda **Settings**, completa la **verifica dell'app** (pulsante "Verify" — genera
   un link che un admin della Pagina deve confermare): sblocca la richiesta di accesso ai
   prodotti.
3. Nella scheda **Products**, richiedi l'accesso a **Community Management API**. L'approvazione
   di LinkedIn non è immediata (giorni, non ore).
4. Nella scheda **Auth**, prendi **Client ID** e **Client Secret**, e aggiungi tra gli
   **Authorized redirect URLs** l'URL `<PUBLIC_BASE_URL>/oauth/linkedin/callback`
   (es. `https://servermcp.cascinanet.it/oauth/linkedin/callback`).

**Setup lato hub**:
1. Crea il server, tipo **LinkedIn** → in Env imposta `LINKEDIN_CLIENT_ID`,
   `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_ORG_ID` (solo il numero dell'URN
   `urn:li:organization:NUMERO` della Pagina; per più Pagine `cascinanet=123, pixelio=456`) → Salva.
2. Clicca **"Autorizza con LinkedIn"**: reindirizza al consenso OAuth, poi torna
   automaticamente al pannello con l'esito.
3. Un task in background rinnova l'access token (~60 giorni di validità) prima che scada,
   usando il refresh token; se il rinnovo fallisce (es. refresh token scaduto, ~1 anno),
   va rifatto il consenso dal pulsante.

Tool esposti: `crea_post`, `elenco_post`, `elenco_pagine`, `elimina_post`, `statistiche_post`.

- **Testo e caratteri speciali**: il campo del post è in formato *little text* di LinkedIn,
  dove `( ) [ ] { } < > @ # | * _ ~ \` sono riservati. Senza la `\` davanti LinkedIn **tronca
  in silenzio** il testo al primo carattere che non riesce a interpretare. `crea_post` li protegge
  automaticamente e converte `#parola` in hashtag; `elenco_post` restituisce il testo leggibile.
  Le menzioni `@` non sono supportate e vengono pubblicate come testo semplice.
- **Verifica dopo la pubblicazione**: `crea_post` rilegge il post e confronta il testo salvato
  con quello inviato (ignorando i link, che LinkedIn accorcia in `lnkd.in`). Se sono diversi
  risponde `ok: false` con `testo_salvato` e un avviso: il post però è già online, valuta se
  eliminarlo.
- **Più Pagine con un solo server**: il token OAuth è dell'utente, quindi vale per tutte le
  Pagine che amministra. In `LINKEDIN_ORG_ID` elenca le Pagine come `etichetta=ID` separate da
  virgola; `crea_post` ed `elenco_post` accettano il parametro `pagina` (etichetta o ID). Con
  più Pagine configurate `pagina` è **obbligatorio**: senza, il tool non pubblica e chiede su
  quale Pagina operare. `elenco_pagine` mostra le Pagine configurate e quelle che l'utente
  amministra (nome e ID), utile per trovare gli ID. `elimina_post` e `statistiche_post` non
  ne hanno bisogno: l'URN del post identifica già la Pagina.
- Un server separato per Pagina serve solo se persone diverse devono accedere a Pagine
  diverse (ogni server ha il suo token dell'hub).

- **Token OAuth mai in chiaro nei log**: mascherati (`***`) sia negli errori del server sia
  (per `access_token`/`refresh_token`/client secret) in ogni messaggio d'errore restituito.
- **`PUBLIC_BASE_URL`**: imposta questa variabile d'ambiente con l'URL pubblico
  dell'installazione (dominio + schema, senza slash finale) perché il redirect URI OAuth
  corrisponda esattamente a quello registrato in LinkedIn — importante dietro un reverse
  proxy TLS come quello di Plesk.
- **Versione API**: ogni chiamata invia l'header `LinkedIn-Version` (formato `YYYYMM`).
  LinkedIn tiene attiva ogni versione circa un anno; quando la default del codice viene
  dismessa i tool rispondono con un errore 426 esplicito. Per aggiornarla senza rilasciare
  codice imposta `LINKEDIN_API_VERSION` (es. `202609`) nelle Env extra del server.
- **Scope OAuth**: di default `w_organization_social r_organization_social rw_organization_admin`
  (quelli del prodotto Community Management API). Sovrascrivibili con `LINKEDIN_SCOPES`.
- **Primo test dal vivo**: `statistiche_post` usa un endpoint (`socialActions`) storicamente
  meno stabile: verifica i nomi dei campi al primo utilizzo reale.

### Facebook + Instagram (Meta, Graph API)

Server MCP custom incluso nel repo ([app/mcp_servers/meta_server.py](app/mcp_servers/meta_server.py))
per creare/elencare/eliminare post su Pagine Facebook e sui relativi account Instagram
Business/Creator collegati, tramite la **Graph API** di Meta. Come LinkedIn, l'autenticazione
è **OAuth 2.0**: serve un'app Meta for Developers approvata e un consenso una tantum dal
pannello admin. Un solo consenso vale per **entrambe** le piattaforme e per tutte le Pagine
che l'utente amministra: gli account Instagram collegati si scoprono da soli, non vanno
configurati a mano.

**Vincolo di Instagram**: non esiste un endpoint per pubblicare post di solo testo. Ogni post
richiede un'immagine o un video con URL pubblico raggiungibile da Meta (`crea_post_instagram`
rifiuta la chiamata senza uno dei due).

**Setup lato Meta** (esterno all'hub, richiede tempo):
1. Crea un'app su [Meta for Developers](https://developers.facebook.com/apps), tipo
   **Azienda**, poi aggiungi il prodotto **Facebook Login per Business** (non la variante
   consumer: serve quella business per i permessi sulle Pagine).
2. In **Impostazioni → Di base**: imposta **Dominio app** e **Privacy Policy URL**
   (obbligatorio per la review), prendi **ID app** e **Chiave segreta app**.
3. In **Facebook Login per Business → Impostazioni**, aggiungi tra i **Valid OAuth Redirect
   URIs** l'URL `<PUBLIC_BASE_URL>/oauth/meta/callback`
   (es. `https://servermcp.cascinanet.it/oauth/meta/callback`).
4. Finché l'app è in modalità **Sviluppo**, il consenso funziona solo con gli account
   aggiunti in **Ruoli app → Tester/Amministratori** — utile per collaudare prima della review.
5. Per la pubblicazione in produzione serve la **App Review** sui permessi
   `pages_manage_posts`, `instagram_basic`, `instagram_content_publish` (più lenta di quella
   di LinkedIn: Meta chiede una demo del flusso d'uso).

**Setup lato hub**:
1. Crea il server, tipo **Facebook + Instagram** → in Env imposta `META_APP_ID`,
   `META_APP_SECRET` → Salva.
2. Clicca **"Autorizza con Meta"**: reindirizza al consenso OAuth, poi torna automaticamente
   al pannello. A differenza di LinkedIn non c'è un ID Pagina da inserire prima: il consenso
   legge da solo le Pagine amministrate (`/me/accounts`) e, per ciascuna, l'eventuale account
   Instagram collegato (campo `instagram_business_account`).
3. (Opzionale) Per usare un'etichetta comoda al posto dell'ID nei tool, imposta `META_PAGES`
   come `cascinanet=<page_id>, pixelio=<page_id>` (gli ID si trovano con `elenco_pagine`).
4. Meta non usa un refresh token separato come LinkedIn: un task in background ri-estende
   periodicamente il token utente (valido ~60 giorni, rinnovabile finché non è scaduto) e
   ri-legge Pagine/Instagram; se il rinnovo fallisce (token scaduto oltre la finestra
   rinnovabile), va rifatto il consenso dal pulsante.

Tool esposti: `crea_post_facebook`, `elenco_post_facebook`, `crea_post_instagram`,
`elenco_post_instagram`, `elenco_pagine`, `elimina_post`, `statistiche_post`.

- **Più Pagine con un solo server**: come per LinkedIn, il token è dell'utente e vale per
  tutte le Pagine che amministra. Con più Pagine autorizzate il parametro `pagina` (etichetta
  o ID) è **obbligatorio** su `crea_post_facebook`, `elenco_post_facebook`,
  `crea_post_instagram` ed `elenco_post_instagram`: senza, il tool non pubblica e chiede su
  quale Pagina operare. `elimina_post` e `statistiche_post` richiedono comunque `pagina` per
  sapere quale token usare (a differenza di LinkedIn, dove basta l'URN del post).
- **Non ancora verificato dal vivo** (nessuna app Meta approvata al momento di scrivere questo
  codice): tutti i tool sono scritti secondo la documentazione ufficiale della Graph API, ma
  vanno controllati al primo uso reale. In particolare:
  - la pubblicazione video su Instagram, che Meta elabora in modo asincrono (il tool attende
    fino a ~30 secondi che sia pronta, poi restituisce un errore invece di pubblicare un video
    non pronto — per video grandi potrebbe non bastare);
  - `elimina_post` su un media Instagram: confermato dal vivo che il permesso attuale
    (`instagram_content_publish`) non basta — l'errore preciso restituito da Meta non è
    ancora stato letto (vedi nota sotto), verificalo al primo uso reale dopo il deploy di
    questa correzione;
  - `statistiche_post`, che prova prima i campi di un post Facebook e ripiega su quelli
    Instagram se il primo tentativo fallisce.
- **Un token "scaduto" non è sempre davvero scaduto**: i tool distinguono un token
  effettivamente non valido/scaduto (Graph API, errore `OAuthException` codice `190`, o HTTP
  401) da qualunque altro errore `OAuthException` — tipicamente un permesso mancante per
  quella singola azione (es. `elimina_post` su Instagram). Solo il primo caso propone di
  rifare il consenso OAuth; il secondo restituisce il messaggio originale di Meta. Prima di
  questa correzione i due casi venivano confusi: un errore di permessi appariva come "token
  revocato, rifai il consenso" anche quando il token era valido (riprodotto dal vivo:
  `crea_post_instagram` riusciva, `elimina_post` sullo stesso post/stesso token falliva con
  quel messaggio fuorviante).
- **`expires_in` mancante nella risposta di Meta non vuol dire "scade subito"**: riautorizzando
  un utente che ha già un token valido, la richiesta di estensione (`fb_exchange_token`) può
  riuscire senza restituire `expires_in` (o con `0`) — riprodotto dal vivo: il pannello segnava
  "token scaduto" pochi secondi dopo un'autorizzazione appena completata, mentre i tool
  continuavano a funzionare. In questo caso l'hub assume la durata standard di 60 giorni invece
  di trattarlo come già scaduto; un valore negativo (davvero scaduto) resta segnalato come tale.
- **Token mai in chiaro nei log**: mascherati (`***`) sia negli errori del server sia (token
  utente, token di Pagina, client secret) in ogni messaggio d'errore restituito.
- **`PUBLIC_BASE_URL`**: stessa variabile usata da LinkedIn — deve corrispondere esattamente
  al redirect URI registrato nell'app Meta.
- **Versione API**: ogni chiamata usa la Graph API `v23.0` di default. Meta ritira le versioni
  vecchie dopo circa 2 anni; per aggiornarla senza rilasciare codice imposta
  `META_API_VERSION` (es. `v25.0`) nelle Env extra del server.
- **Scope OAuth**: di default `pages_show_list pages_read_engagement pages_manage_posts
  business_management instagram_basic instagram_content_publish`. Sovrascrivibili con
  `META_SCOPES` nelle Env extra.

### Google Ads (report e gestione campagne)

Server MCP custom incluso nel repo ([app/mcp_servers/google_ads_server.py](app/mcp_servers/google_ads_server.py))
per leggere report di rendimento e gestire campagne (pausa/attiva, cambio budget) su **un
singolo account** Google Ads, via **Google Ads API**. Come LinkedIn e Meta, l'autenticazione è
**OAuth 2.0**; l'account deve essere singolo: questo tipo **non supporta un manager account
(MCC)** con più account clienti sotto.

**Il vecchio Developer Token non esiste più dal 9 settembre 2026.** Fino ad allora l'accesso
era legato a un Developer Token generato da Google Ads → Centro API (livello Basic, solo
account di test; Standard, approvato da Google, per l'account reale). Da quella data Google ha
ritirato il Centro API: l'accesso appartiene ora al **progetto Google Cloud** dello stesso
Client OAuth usato qui, con livelli **Test → Explorer → Basic → Standard** gestiti da Google
Cloud Console → API e servizi → Google Ads API → **Livelli di accesso**. Il livello
**Explorer**, assegnato di norma in automatico non appena si abilita l'API, dà già accesso
all'account reale (migliaia di operazioni al giorno, per account): per un uso conversazionale
basta, non serve rincorrere Basic/Standard a meno di avvicinarsi davvero al tetto giornaliero.
L'header `developer-token` resta accettato per compatibilità ma Google lo ignora: nell'hub è
facoltativo (vedi sotto).

**Setup lato Google** (esterno all'hub, richiede tempo):
1. Su [Google Cloud Console](https://console.cloud.google.com/), crea (o riusa) un progetto,
   abilita la **Google Ads API**, poi crea un **Client ID OAuth** di tipo *Applicazione web*
   (API e servizi → Credenziali). Aggiungi tra gli **URI di reindirizzamento autorizzati**
   l'URL `<PUBLIC_BASE_URL>/oauth/google_ads/callback`
   (es. `https://servermcp.cascinanet.it/oauth/google_ads/callback`).
2. Nella stessa pagina della Google Ads API, verifica/richiedi il **livello di accesso**
   (sezione "Livelli di accesso" → Gestisci): **Explorer** di norma arriva in automatico e
   basta già per l'account reale (vedi sopra).
3. Nella **schermata di consenso OAuth** (Google Auth Platform) dello stesso progetto, porta lo
   stato da *Testing* a **In produzione**. Lo scope richiesto (`.../auth/adwords`) è
   "sensibile": in stato *Testing* Google invalida il refresh token dopo **7 giorni**, rompendo
   l'autorizzazione a ripetizione. Passare a "In produzione" con uno scope sensibile può
   richiedere una verifica di Google (più veloce se l'app resta a uso interno con pochi
   utenti, ma non istantanea) — nel frattempo, con il tuo account aggiunto come utente di test
   nella scheda Audience, l'autorizzazione funziona comunque, solo con il limite dei 7 giorni.
4. Prendi l'**ID cliente** dell'account Google Ads (in alto a destra nell'interfaccia, formato
   `123-456-7890`): è il `GOOGLE_ADS_CUSTOMER_ID`.

**Setup lato hub**:
1. Crea il server, tipo **Google Ads** → in Env imposta `GOOGLE_ADS_CLIENT_ID`,
   `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_CUSTOMER_ID` → Salva.
2. Clicca **"Autorizza con Google"**: reindirizza al consenso OAuth, poi torna automaticamente
   al pannello con l'esito.
3. A differenza di Meta, Google usa un refresh token classico che non va ri-esteso
   periodicamente: un task in background rinnova solo l'access token (dura ~1 ora) ogni 15
   minuti. Se il refresh token stesso non è più valido (revocato, o scaduto per il limite dei 7
   giorni spiegato sopra), va rifatto il consenso dal pulsante.

Tool esposti: `elenco_campagne`, `report_performance`, `metti_in_pausa_campagna`,
`attiva_campagna`, `modifica_budget_campagna`.

- **Azioni che spendono soldi veri**: `metti_in_pausa_campagna`, `attiva_campagna` e
  `modifica_budget_campagna` agiscono subito sull'account reale, senza una doppia conferma
  applicativa nel tool stesso — la cautela va esercitata da chi li invoca (conferma esplicita
  dall'utente prima di eseguirli), come per qualunque azione che muove denaro reale.
- **Campagna indicata per ID o per nome**: tutti i tool che operano su una campagna accettano
  l'ID numerico o il nome (anche parziale, case-insensitive). Più campagne corrispondono allo
  stesso nome parziale → errore con l'elenco, invece di agire su quella sbagliata: usa l'ID per
  essere univoco.
- **Account singolo, non MCC**: `GOOGLE_ADS_CUSTOMER_ID` è un solo account. Per gestire più
  account clienti sotto un manager account servirebbe l'header `login-customer-id` e una
  selezione tra più account, non ancora implementati (stesso tipo di estensione già fatta per
  le più Pagine di LinkedIn/Meta — se serve, si aggiunge).
- **Metriche senza periodo esplicito**: `elenco_campagne` mostra sempre gli ultimi 7 giorni
  (comportamento di default della Google Ads API quando la query non specifica un intervallo).
  Per un periodo diverso usa `report_performance` con `data_da`/`data_a`.
- **Non pagina oltre la prima pagina dei risultati**: adeguato per un uso conversazionale su
  un account singolo; non pensato per estrarre l'intero storico di un account con migliaia di
  campagne/parole chiave.
- **Versione API**: ogni chiamata usa la Google Ads API `v25` di default. Dal 2026 Google
  rilascia una versione al mese (presto due) e ne ritira una dopo pochi mesi, non più dopo un
  anno come in passato — riprodotto dal vivo: `v22` era già dismessa un mese dopo il suo
  rilascio. Quando la default smette di funzionare, l'errore lo dice esplicitamente
  ("Version vNN is deprecated"): aggiorna `GOOGLE_ADS_API_VERSION` nelle Env extra del server
  con la versione indicata nel messaggio o in una più recente, senza bisogno di un rilascio di
  codice né di riavviare il container.
- **`GOOGLE_ADS_DEVELOPER_TOKEN` è facoltativa**: retaggio del sistema ritirato il 9/2026 (vedi
  sopra), nessun campo dedicato nel form — si imposta, se proprio serve per un'integrazione più
  vecchia, in "Env extra (JSON)". Se assente, l'header `developer-token` non viene inviato
  affatto, invece di bloccare ogni chiamata come faceva il codice prima di questa scoperta.
- **Token mai in chiaro nei log**: mascherati (`***`) sia negli errori del server sia (access
  token, refresh token, client secret, developer token) in ogni messaggio d'errore restituito.
- **Non ancora verificato dal vivo**: nessun account Google Ads disponibile al momento di
  scrivere questo codice. Scritto secondo la documentazione ufficiale della Google Ads API
  (GAQL via `googleAds:search`, mutate via `campaigns:mutate`/`campaignBudgets:mutate`), da
  controllare al primo uso reale — in particolare i nomi dei campi nella risposta JSON REST
  (camelCase, es. `advertisingChannelType`).

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
