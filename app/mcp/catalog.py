"""Catalogo dei tipi di server MCP disponibili nell'hub.

Ogni tipo è un "template" che pre-compila i campi tecnici del form (comando, argomenti)
e indica che tipo di credenziali servono. Per aggiungere un nuovo server MCP in futuro
basta aggiungere una voce qui.
"""

from __future__ import annotations

from pydantic import BaseModel


class ServerType(BaseModel):
    key: str                      # identificativo del tipo (salvato sul server)
    label: str                    # nome mostrato nel menu
    description: str = ""
    command: str = ""             # comando di avvio pre-compilato
    args: list[str] = []          # argomenti pre-compilati
    env: dict[str, str] = {}      # env pre-compilate (placeholder da completare)
    # Tipo di credenziali richieste:
    #   "google_service_account" -> mostra il campo "incolla JSON service account"
    #   "none"                   -> nessuna credenziale gestita dal form
    credential_kind: str = "none"
    # Nome della variabile d'ambiente in cui scrivere il percorso del file credenziali.
    credentials_env: str = "GOOGLE_APPLICATION_CREDENTIALS"
    # Suggerimento mostrato nel form per questo tipo.
    hint: str = ""


SERVER_TYPES: list[ServerType] = [
    ServerType(
        key="google_analytics",
        label="Google Analytics",
        description="Server MCP ufficiale di Google Analytics (Admin + Data API).",
        command="analytics-mcp",
        args=[],
        credential_kind="google_service_account",
        hint="Incolla il JSON del service account. L'email del service account deve avere "
             "accesso (Visualizzatore) alla proprietà GA4 in Google Analytics.",
    ),
    ServerType(
        key="google_search_console",
        label="Google Search Console",
        description="Server MCP per Google Search Console (search analytics, URL inspection).",
        command="mcp-search-console",
        args=[],
        env={"GSC_SKIP_OAUTH": "true"},
        credential_kind="google_service_account",
        credentials_env="GSC_CREDENTIALS_PATH",
        hint="Incolla il JSON del service account. Multi-proprietà: un solo server interroga TUTTE le "
             "proprietà a cui il service account ha accesso (il sito si passa come parametro nei tool). "
             "Aggiungi il service account come utente in Search Console e abilita l'API Google Search Console.",
    ),
    ServerType(
        key="sqlite",
        label="SQLite (database)",
        description="Piccolo database SQLite gestito da Claude (query, tabelle, insert).",
        command="mcp-server-sqlite",
        # Il percorso reale (sotto DATA_DIR, per restare sul disco/volume persistente qualunque
        # sia il deployment) viene calcolato a runtime in admin/routes.py::_form_context.
        args=["--db-path", "<DATA_DIR>/db/database.db"],
        credential_kind="none",
        hint="Il percorso del file DB è pre-compilato sotto la cartella dati di questo "
             "deployment (persistente su qualunque piattaforma: Lightsail, Azure, Docker). "
             "Cambia solo il nome del file se vuoi un database diverso da 'database.db'. "
             "Il file viene creato se non esiste. "
             "Tool: read_query, write_query, create_table, list_tables, describe_table.",
    ),
    ServerType(
        key="sqlite_encrypted",
        label="SQLite cifrato (SQLCipher)",
        description="Database SQLite cifrato con SQLCipher. La passphrase si passa a ogni tool "
                    "call (parametro 'key') e non viene mai memorizzata dall'hub.",
        command="python3",
        args=["-m", "app.mcp_servers.sqlcipher_server", "--db-path", "<DATA_DIR>/db/encrypted.db"],
        credential_kind="none",
        hint="Database cifrato a riposo. Ogni tool (read_query, write_query, create_table, "
             "list_tables, describe_table) richiede il parametro 'key' con la passphrase: "
             "non è salvata da nessuna parte e va fornita a ogni chiamata. Cambia solo il nome "
             "del file negli argomenti se vuoi un database diverso da 'encrypted.db'. "
             "Nota: 'Testa connessione' verifica solo che il processo parta, non la passphrase "
             "(che per design non è nota all'hub).",
    ),
    ServerType(
        key="woocommerce",
        label="WooCommerce (report e-commerce)",
        description="Reporting WooCommerce in tempo reale: vendite, top prodotti, coupon, andamento, clienti, ordini.",
        command="python3",
        args=["-m", "app.mcp_servers.woocommerce_server"],
        credential_kind="none",
        hint="Imposta in Env: WC_SITE_URL (es. https://tuosito.it), WC_CONSUMER_KEY, "
             "WC_CONSUMER_SECRET (WooCommerce → Impostazioni → Avanzate → REST API). "
             "Opzionali WC_APP_USER e WC_APP_PASSWORD (WordPress → Utenti → Profilo → "
             "\"Password per le applicazioni\", utente Amministratore): senza queste due, "
             "fatturato per prodotto, dettaglio coupon e report clienti tornano dati ridotti "
             "o non disponibili invece di un errore.",
    ),
    ServerType(
        key="linkedin",
        label="LinkedIn (pubblica e gestisci post)",
        description="Crea, elenca ed elimina post sulla Pagina aziendale LinkedIn (Community Management API).",
        command="python3",
        args=["-m", "app.mcp_servers.linkedin_server", "--server-id", "<SERVER_ID>"],
        credential_kind="none",
        hint="Compila Client ID, Client Secret (dall'app LinkedIn Developer, scheda Auth) e "
             "Pagine qui sotto (una sola: l'ID; più pagine: 'etichetta=ID, etichetta2=ID2'). Salva, poi usa il pulsante 'Autorizza con LinkedIn' più "
             "sotto per completare il consenso OAuth. Richiede che LinkedIn abbia approvato "
             "per l'app l'accesso al prodotto 'Community Management API'.",
    ),
    ServerType(
        key="meta",
        label="Facebook + Instagram (pubblica e gestisci post)",
        description="Crea, elenca ed elimina post/media su Pagine Facebook e sugli account Instagram "
                    "Business collegati (Graph API).",
        command="python3",
        args=["-m", "app.mcp_servers.meta_server", "--server-id", "<SERVER_ID>"],
        credential_kind="none",
        hint="Compila App ID e Chiave segreta (da Meta for Developers, scheda Impostazioni di base) "
             "e salva. Poi usa il pulsante 'Autorizza con Meta' più sotto: il consenso scopre da "
             "solo le Pagine Facebook amministrate e gli account Instagram collegati (non vanno "
             "inseriti a mano). Etichette opzionali per le Pagine in 'Pagine (etichette)' qui sotto. "
             "Richiede che Meta abbia approvato per l'app i permessi di pubblicazione (App Review).",
    ),
    ServerType(
        key="custom",
        label="Personalizzato (comando manuale)",
        description="Definisci manualmente comando, argomenti ed env per un qualsiasi server MCP stdio.",
        command="",
        args=[],
        credential_kind="none",
        hint="Specifica comando e argomenti del server MCP da avviare. Usa Env per le variabili "
             "d'ambiente (es. chiavi API).",
    ),
]

_BY_KEY = {t.key: t for t in SERVER_TYPES}


def get_type(key: str) -> ServerType | None:
    return _BY_KEY.get(key)


def default_type() -> ServerType:
    return SERVER_TYPES[0]
