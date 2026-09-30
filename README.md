# 🤖 FioGora (Integração Fiorilli-Ahgora)

[![Python](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-05998b.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**FioGora** é um serviço de integração RPA de alto desempenho que automatiza a sincronização de dados de funcionários entre o sistema de gestão **Fiorilli** e a plataforma de controle de ponto **Ahgora**. Ele utiliza **FastAPI** para gerenciamento, **PostgreSQL** para persistência e **Selenium** para automação.

---

## ✨ Recursos

- **Sincronização Automatizada**: Conexão contínua entre o Fiorilli e o Ahgora.
- **Arquitetura Assíncrona**: Gerenciamento de tarefas em segundo plano usando FastAPI Lifespan e agendadores.
- **Docker-First**: Ambiente totalmente em contêineres para implantação consistente.
- **Gerenciamento de Tarefas**: API REST para monitorar, listar e revisar logs detalhados de cada tarefa de sincronização.
- **Sistema de Repetição de Tarefas (Retry)**: Agendador integrado para lidar com falhas de automação transitórias.
- **Logs Detalhados**: Logs de execução granulares disponíveis via API REST.

---

## 🚀 Como Começar

### 1. Requisitos

- **Docker & Docker Compose** (Recomendado)
- **OR** Python 3.13 (`uv`), PostgreSQL e Firefox (para desenvolvimento local).

### 2. Configuração

**1. Variáveis de Ambiente:**

Crie um arquivo `.env` a partir do exemplo (`cp .env.example .env`) e preencha as variáveis:

| Variável            | Descrição                                          | Padrão |
| ------------------- | -------------------------------------------------- | ------ |
| `FIORILLI_URL`      | URL base do sistema Fiorilli                       | -      |
| `AHGORA_URL`        | URL de login da plataforma Ahgora                  | -      |
| `HEADLESS_MODE`     | Executar navegador de forma invisível (True/False) | `True` |
| `ADMIN_USERNAME`    | Nome de usuário do Administrador para painel     | `admin` |
| `ADMIN_PASSWORD`    | Senha do Administrador para o painel             | `changeme123` |
| `SECRET_KEY`        | Chave criptográfica para tokens (JWT)              | `b39dc...` |

**2. Configuração no Fiorilli:**

Configure as colunas da tabela no painel *Fiorilli 2.1 - Cadastro de Trabalhadores* e defina como padrão para seu usuário:

- Registro
- Nome
- CPF
- Sexo
- Dt. Nascimento
- PIS/PASEP/NIT
- Nome Cargo Atual
- Nome Local Trabalho
- Nome Unidade Orçamentária
- Nome Vinculo
- Dt. Admissão
- Dt. Desligamento

**3. Mapeamentos Estáticos (`data/mappings/`):**

Para realizar ajustes finos nas regras de negócio sem precisar alterar o código base, o **FioGora** utiliza diretórios de recursos externos.

Edite os arquivos localizados na pasta raiz `data/mappings/`:
- `constants.json`: Definição e padrão das nomenclaturas de colunas e propriedades (ex: `ignore_location_change_ids` para previnir trocas de localizações do servidor).
- `exceptions_and_typos.json`: De/Para para correção de erros de digitação e nomenclatura diferentes de órgãos no Fiorilli.
- `department_to_location.csv`: Mapeamento de `Local/Departamento` (Fiorilli) associados a suas localidades da catraca eletrônica (Ahgora).
- `leave_codes.csv`: Dicionário contendo os códigos numéricos de afastamentos / férias.

### 3. Executando

**1. Docker:**

```bash
docker-compose up -d --build
```

**2. Localmente:**

```bash
docker-compose up -d db
uv run uvicorn app.main:app --reload
```

- **API**: `http://localhost:8000`
- **Docs (Swagger)**: `http://localhost:8000/docs`

### 4. Deploy automático

O servidor busca as atualizações sozinho (não precisa expor portas nem guardar senhas no GitHub). A cada 30 minutos, [`deploy/auto-deploy.sh`](deploy/auto-deploy.sh) verifica se a `main` tem um commit novo e, se o workflow **Tests** passou para ele e nenhuma sincronização/tarefa está rodando, faz `git pull` e `docker compose up -d --build` (as migrações rodam na inicialização do container).

Requisitos no servidor: o projeto clonado com `git` na branch `main`, `.env` configurado e Docker Compose v2 (`docker compose`).

**Instalação** (no servidor, dentro da pasta do projeto):

```bash
(crontab -l 2>/dev/null | grep -v auto-deploy.sh; echo "*/30 * * * * $PWD/deploy/auto-deploy.sh --cron") | crontab -
```

**Deploy manual** (no servidor, ou de outra máquina com `ssh <servidor> fiogora/deploy/auto-deploy.sh`):

| Comando | O que faz |
|---|---|
| `deploy/auto-deploy.sh` | Faz o deploy agora se houver commit novo, com as mesmas verificações, e mostra o status |
| `deploy/auto-deploy.sh --force` | Reconstrói e reinicia mesmo que o commit já esteja no ar (ex.: depois de alterar o `.env`) |
| `deploy/auto-deploy.sh --dry-run` | Faz todas as verificações sem fazer deploy |

- **Log**: `~/.local/state/fiogora-deploy/deploy.log` (inclui os deploys manuais)
- **Desativar o automático**: `crontab -l | grep -v auto-deploy.sh | crontab -`

---

## 📂 Estrutura do Projeto

```text
fiogora/
├── app/
│   ├── api/            # REST API Layer
│   ├── core/           # Config, DB, and Scheduler logic
│   ├── domain/         # Business entities and schemas
│   ├── infrastructure/ # RPA (Selenium) and Repositories
│   ├── services/       # Core business logic
│   └── main.py         # Application Entrypoint
├── tests/              # Test suite
├── Dockerfile          # Container definition
└── docker-compose.yml  # Service orchestration
```

---

## 📜 Licença

Distribuído sob a licença **MIT**. Veja [LICENSE](./LICENSE) para mais informações.
