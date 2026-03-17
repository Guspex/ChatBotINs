# ORACULO/UDESC — Configuração do Repositório Git

## 1. Inicializar o repositório

```bash
# No diretório raiz do projeto
cd ORACULO_udesc

git init
git branch -M main
```

---

## 2. Estrutura de pastas antes do primeiro commit

Crie os diretórios que o .gitignore precisa encontrar vazios
(Git não versiona pastas vazias — use `.gitkeep`):

```bash
mkdir -p ingestion/pdfs
mkdir -p ingestion/logs
mkdir -p database
mkdir -p ingestion
mkdir -p retrieval
mkdir -p generation

touch ingestion/pdfs/.gitkeep
touch ingestion/logs/.gitkeep
```

---

## 3. Criar o banco PostgreSQL antes de rodar

```sql
-- Execute como superusuário (ex: psql -U postgres)
CREATE DATABASE oraculo_udesc;
CREATE USER oraculo WITH PASSWORD 'oraculo_pass';
GRANT ALL PRIVILEGES ON DATABASE oraculo_udesc TO oraculo;

-- Conectar ao banco e instalar pgvector:
\c oraculo_udesc
CREATE EXTENSION vector;
CREATE EXTENSION unaccent;
```

---

## 4. Configurar o ambiente

```bash
# Copiar e editar variáveis de ambiente
cp .env.example .env
# Edite .env com sua senha real

# Criar ambiente virtual
python -m venv .venv
source .venv/bin/activate     # Linux/Mac
# .venv\Scripts\activate      # Windows

# Instalar dependências
pip install -r requirements.txt
```

---

## 5. Baixar os modelos Ollama

```bash
# Em outro terminal
ollama serve

# Baixar os dois modelos necessários
ollama pull nomic-embed-text   # ~274 MB — embeddings
ollama pull aya-expanse:8b     # ~5.1 GB — geração de respostas
```

---

## 6. Inicializar o banco e ingerir as INs

```bash
# Recria schema do zero
python resetabanco.py

# Baixa os PDFs das INs do site da UDESC
python scraper_udesc.py

# Vetoriza os PDFs e indexa no pgvector
python pipeline_vetorizacao.py

# Opcional: força reprocessamento de tudo
python pipeline_vetorizacao.py --forcar
```

---

## 7. Testar o sistema

```bash
# Suite completa de testes
python test.py

# Testes individuais
python test.py --banco       # testa PostgreSQL + pgvector
python test.py --busca       # testa busca semântica
python test.py --aya         # testa geração com AYA

# Chat interativo no terminal
python test.py --interativo

# Ou direto pelo chatbot
python chatbot.py
```

---

## 8. Primeiro commit

```bash
git add .
git status    # confirme que .env NÃO aparece na lista

git commit -m "feat: ORACULO/UDESC — setup inicial

- Pipeline RAG: pgvector + nomic-embed-text + aya-expanse:8b
- Scraper das INs da UDESC (scraper_udesc.py)
- Pipeline de vetorização com chunking por artigo/seção
- Chatbot com histórico de sessão persistido no PostgreSQL
- Suite de testes com modo interativo"
```

---

## 9. Conectar a um repositório remoto (GitHub/GitLab)

```bash
# Crie o repositório vazio no GitHub/GitLab, depois:
git remote add origin https://github.com/SEU_USUARIO/oraculo-udesc.git
git push -u origin main
```

---

## Fluxo de trabalho diário

```bash
# Atualizar INs novas
python scraper_udesc.py --sem-banco   # só baixa PDFs
python pipeline_vetorizacao.py        # vetoriza os novos

# Recriar banco do zero (se necessário)
python resetabanco.py
python pipeline_vetorizacao.py --forcar

# Commitar mudanças
git add -p
git commit -m "feat/fix/docs: descrição"
git push
```

---

## Estrutura esperada do repositório

```
oraculo-udesc/
├── .env.example            # variáveis de ambiente (versionar)
├── .gitignore
├── requirements.txt
├── GIT_SETUP.md            # este arquivo
├── README.md
│
├── chatbot.py              # motor do chatbot + CLI
├── config.py               # configurações centrais
├── resetabanco.py          # recria schema do banco
├── pipeline_vetorizacao.py # PDF → chunks → embeddings → pgvector
├── scraper_udesc.py        # scraper do site da UDESC
├── test.py                 # suite de testes
│
├── database/
│   ├── connection.py       # pool de conexões PostgreSQL
│   └── schema.sql          # DDL das tabelas
│
├── ingestion/
│   ├── embedder.py         # embeddings via Ollama
│   ├── ingestor.py         # pipeline de ingestão de PDFs
│   ├── pdfs/               # PDFs das INs (não versionados)
│   └── logs/               # relatórios de scraping/vetorização (não versionados)
│
├── retrieval/
│   └── retriever.py        # busca semântica cosine
│
└── generation/
    └── generator.py        # RAG + AYA-Expanse 8B
```
