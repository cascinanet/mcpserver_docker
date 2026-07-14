"""Modelli di dominio (pydantic)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MCPServer(BaseModel):
    """Definizione di un server MCP gestito dall'hub (lanciato come subprocess stdio)."""

    id: str = Field(..., description="Identificativo univoco, usato negli URL (es. 'analytics').")
    name: str = Field(..., description="Nome leggibile mostrato nella admin UI.")
    type: str = Field("custom", description="Chiave del tipo dal catalogo (es. 'google_analytics').")
    description: str = ""
    enabled: bool = True

    # Comando di avvio del server MCP (stdio).
    command: str = Field(..., description="Eseguibile, es. 'pipx'.")
    args: list[str] = Field(default_factory=list, description="Argomenti, es. ['run', 'analytics-mcp'].")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Env extra passate al processo. I valori 'env:NOME' vengono risolti dall'ambiente dell'hub.",
    )

    # Token opzionale richiesto ai client per connettersi all'endpoint SSE di questo server.
    auth_token: str | None = None

    # True se per questo server è stato caricato un file di credenziali (service account).
    # Il JSON vero NON è salvato qui: vive in data/creds/<id>.json (fuori da git).
    has_credentials: bool = False


class User(BaseModel):
    """Utente della admin UI."""

    username: str
    password_hash: str
    is_admin: bool = True
