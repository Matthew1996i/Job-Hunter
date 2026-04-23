#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Job Hunter — Reset completo
# Apaga vagas, histórico, cache e decisões do MongoDB.
# Preserva: API key, modelo, currículo, presets  (use --tudo para apagar tudo).
#
# Uso:
#   ./reset.sh          — apaga dados de busca, preserva config e presets
#   ./reset.sh --tudo   — apaga tudo, inclusive config (.env) e presets
# ──────────────────────────────────────────────────────────────────────────────

RED='\033[91m'
YEL='\033[93m'
GRN='\033[92m'
DIM='\033[2m'
BLD='\033[1m'
RST='\033[0m'

CONTAINER="job_hunter_mongo"
DB="job_hunter"
TUDO=false

# Flag --tudo apaga também config (.env) e presets
if [[ "$1" == "--tudo" ]]; then
    TUDO=true
fi

echo ""
echo -e "${BLD}${RED}  ╔══════════════════════════════════════╗"
echo -e "  ║   JOB HUNTER — Reset de Dados        ║"
echo -e "  ╚══════════════════════════════════════╝${RST}"
echo ""

if $TUDO; then
    echo -e "  ${YEL}Modo: TUDO — apaga vagas, config (.env), presets e cache${RST}"
else
    echo -e "  ${YEL}Modo: padrão — preserva API key, modelo, currículo e presets${RST}"
fi

echo ""
echo -e "  ${DIM}Será apagado:${RST}"
echo -e "  ${DIM}  • MongoDB: vagas, fila, histórico, decisões, sessões, erros, seen${RST}"
if $TUDO; then
    echo -e "  ${DIM}  • MongoDB: presets${RST}"
    echo -e "  ${DIM}  • .env: GROQ_API_KEY, MODEL, RESUME_PATH${RST}"
fi
echo ""

read -r -p "  Confirma reset? (s/N): " RESP
RESP="${RESP,,}"
if [[ "$RESP" != "s" ]]; then
    echo -e "\n  ${YEL}Cancelado.${RST}\n"
    exit 0
fi

echo ""

# ── MongoDB ────────────────────────────────────────────────────────────────────
echo -ne "  Limpando MongoDB..."

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo -e " ${RED}container '${CONTAINER}' não está rodando.${RST}"
    echo -e "  ${DIM}Suba com: docker-compose up -d${RST}\n"
    exit 1
fi

# Coleções de dados que sempre são apagadas
COLECOES='["jobs","queue","seen","decisions","sessions","errors"]'

if $TUDO; then
    # Apaga dados + presets + meta (config)
    SCRIPT="
        const cols = ${COLECOES};
        cols.push('presets', 'meta');
        cols.forEach(c => db.getCollection(c).drop());
    "
else
    # Preserva meta (config_*), apaga apenas entradas de sessão
    SCRIPT="
        const cols = ${COLECOES};
        cols.forEach(c => db.getCollection(c).drop());
        db.meta.deleteMany({ _id: { \$in: ['api_status', 'resume_hash'] } });
    "
fi

docker exec "${CONTAINER}" mongosh "${DB}" --quiet --eval "${SCRIPT}" > /dev/null 2>&1
if [[ $? -eq 0 ]]; then
    echo -e " ${GRN}OK${RST}"
else
    echo -e " ${RED}ERRO — verifique se o container está rodando${RST}"
fi

# ── Config no .env ─────────────────────────────────────────────────────────────
if $TUDO; then
    ENV_FILE="$(dirname "$0")/.env"
    if [[ -f "$ENV_FILE" ]]; then
        echo -ne "  Limpando .env (GROQ_API_KEY, MODEL, RESUME_PATH)..."
        # Remove as variáveis de config mas preserva MONGO_* e LINKEDIN_*
        sed -i.bak '/^GROQ_API_KEY=/d; /^MODEL=/d; /^RESUME_PATH=/d' "$ENV_FILE"
        rm -f "${ENV_FILE}.bak"
        echo -e " ${GRN}OK${RST}"
    else
        echo -e "  ${DIM}.env não encontrado — nada a limpar${RST}"
    fi
fi

echo ""
echo -e "  ${GRN}${BLD}✓ Reset concluído.${RST}"
if ! $TUDO; then
    echo -e "  ${DIM}API key, modelo, currículo e presets foram preservados.${RST}"
    echo -e "  ${DIM}Use './reset.sh --tudo' para apagar absolutamente tudo.${RST}"
fi
echo ""
