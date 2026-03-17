# ORACULO/UDESC — Assistente Normativo

Chatbot RAG para suporte a servidores da UDESC, baseado em **AYA 8B** (Ollama) + **pgvector** (PostgreSQL).

---

## Pré-requisitos

| Dependência | Versão mínima | Instalação |
|---|---|---|
| Python | 3.11+ | [python.org](https://python.org) |
| PostgreSQL | 15+ | [postgresql.org](https://postgresql.org) |
| pgvector | 0.7+ | `CREATE EXTENSION vector;` |
| Ollama | 0.3+ | [ollama.com](https://ollama.com) |

---

## Instalação

```bash
# 1. Clone o projeto
git clone <repositorio>
cd ORACULO_udesc

# 2. Crie o ambiente virtual
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
# .venv\Scripts\activate    # Windows

# 3. Instale dependências
pip install -r requirements.txt

# 4. Configure variáveis de ambiente (copie e edite)
cp .env.example .env
```

### Arquivo `.env`
```env
PGHOST=localhost
PGPORT=5432
PGDATABASE=orion_udesc
PGUSER=orion
PGPASSWORD=sua_senha_aqui
OLLAMA_URL=http://localhost:11434
ORACULO_INGESTION_DIR=./ingestion/pdfs
ORACULO_LOG_LEVEL=INFO
```

### Criar banco PostgreSQL
```sql
CREATE DATABASE orion_udesc;
CREATE USER orion WITH PASSWORD 'sua_senha_aqui';
GRANT ALL PRIVILEGES ON DATABASE orion_udesc TO orion;
-- Conectar ao banco e instalar pgvector:
\c orion_udesc
CREATE EXTENSION vector;
```

---

## Setup e Ingestão

```bash
# Inicia o Ollama (em outro terminal)
ollama serve

# Executa setup completo: schema + download dos modelos + ingestão dos PDFs
python setup.py
```

### Adicionando PDFs de INs

Deposite os arquivos PDF na pasta `ingestion/pdfs/`. Nomenclatura sugerida:

```
IN_042-2021_Compras-Diretas.pdf
RESOLUCAO_001-2022_Pesquisa.pdf
PORTARIA_015-2023_Afastamentos.pdf
```

Para ingerir novos documentos após o setup inicial:

```python
from ingestion.ingestor import ingerir_pdf, MetadadosDocumento
from pathlib import Path
from datetime import date

ingerir_pdf(
    caminho_pdf=Path("ingestion/pdfs/IN_042-2021.pdf"),
    meta=MetadadosDocumento(
        titulo="Instrução Normativa sobre Compras Diretas",
        numero="042/2021",
        tipo="IN",
        orgao_emissor="UDESC",
        data_publicacao=date(2021, 3, 15)
    )
)
```

---

## Uso

### Interface CLI (desenvolvimento)
```bash
python chatbot.py
```

### Uso programático
```python
from chatbot import SessaoChat

sessao = SessaoChat(servidor_id="12345")

resultado = sessao.perguntar("Como montar um processo de compra por dispensa?")
print(resultado["resposta"])

# Avaliar a resposta
sessao.avaliar_ultima_resposta(positivo=True)

sessao.encerrar()
```

---

## Arquitetura

```
PDF das INs
    ↓
[pdfplumber] Extração de texto
    ↓
[Chunker] Divisão por artigos/seções (512 tokens, 80 overlap)
    ↓
[nomic-embed-text via Ollama] Vetorização (768 dims)
    ↓
[pgvector] Armazenamento + índice HNSW cosine
    ↓
Query do servidor
    ↓
[nomic-embed-text] Embedding da pergunta
    ↓
[pgvector] Busca por similaridade cosine → top-5 chunks
    ↓
[AYA 8B via Ollama] Geração de resposta com contexto normativo
    ↓
Resposta com citação das fontes + persistência no banco
```

---

## Estrutura do projeto

```
ORACULO_udesc/
├── config.py               # Configurações centrais
├── chatbot.py              # Motor do chatbot + CLI
├── setup.py                # Setup inicial e ingestão
├── requirements.txt
├── database/
│   ├── connection.py       # Conexão PostgreSQL
│   └── schema.sql          # Tabelas + índices pgvector
├── ingestion/
│   ├── ingestor.py         # Pipeline PDF → chunks → pgvector
│   └── embedder.py         # Embeddings via Ollama
├── retrieval/
│   └── retriever.py        # Busca semântica cosine
├── generation/
│   └── generator.py        # RAG + AYA 8B
└── ingestion/pdfs/         # Deposite os PDFs aqui
```
