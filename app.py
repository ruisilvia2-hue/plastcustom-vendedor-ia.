import os, re, json, math, logging
import anthropic
import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor, Json
import requests
from flask import Flask, request, jsonify

# Log estruturado (nível + hora + mensagem) em vez de print() solto.
# Continua indo pro mesmo lugar (stdout, visível nos logs do EasyPanel), mas agora
# dá pra saber a hora exata e filtrar por gravidade (INFO/WARNING/ERROR).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("vendedor_ia")

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
DATABASE_URL = os.environ["DATABASE_URL"]
EVOLUTION_URL = os.environ["EVOLUTION_API_URL"]
EVOLUTION_KEY = os.environ["EVOLUTION_API_KEY"]
PROPRIETARIO = os.environ["PROPRIETARIO_TELEFONE"]
CONSULTOR_TELEFONE = os.environ["CONSULTOR_TELEFONE"]  # recebe o resumo automático quando um pedido é fechado
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # segredo compartilhado com o n8n para autenticar o /webhook

# ============================================================
# TABELA DE PREÇOS OFICIAL — portada da calculadora HTML da Plastcustom
# (Plastcustom_Orcamento.html — atualizada em 23/06/2026)
# Faixas: v1 = 150-200kg | v2 = 210-400kg | v3 = 410kg ou +
# ============================================================
# TABELA_PADRAO / PRECOS_PP_PADRAO servem de rede de segurança: são usadas SOMENTE se o
# arquivo Plastcustom_Orcamento.html não for encontrado ou não puder ser lido (ver mais
# abaixo, função carregar_tabela_precos_do_html). Na operação normal, os valores de verdade
# vêm direto do arquivo HTML - assim, pra atualizar preços, basta substituir esse arquivo
# no GitHub e clicar em "Implement", sem precisar editar nenhum código Python.
TABELA_PADRAO = [
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 36.66, "v2": 36.01, "v3": 34.71},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 37.83, "v2": 37.31, "v3": 36.01},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 39.13, "v2": 38.48, "v3": 37.31},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 40.95, "v2": 40.30, "v3": 39.78},

    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 39.78, "v2": 39.13, "v3": 37.96},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 40.95, "v2": 40.43, "v3": 39.13},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 42.25, "v2": 41.60, "v3": 40.43},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 44.07, "v2": 43.55, "v3": 42.90},

    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 33.15, "v2": 32.50, "v3": 31.20},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 34.58, "v2": 33.80, "v3": 32.50},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 35.23, "v2": 34.58, "v3": 33.15},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 37.18, "v2": 36.53, "v3": 35.88},

    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 26.13, "v2": 26.13, "v3": 26.13},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 27.43, "v2": 27.43, "v3": 27.43},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 28.08, "v2": 28.08, "v3": 28.08},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 30.03, "v2": 30.03, "v3": 30.03},
]

PRECOS_PP_PADRAO = {
    "com_nf": [
        {"ate": 200,   "frente2": 30.00, "frente3": 31.50, "verso2": 32.00, "verso3": 33.50},
        {"ate": 400,   "frente2": 29.50, "frente3": 31.00, "verso2": 31.50, "verso3": 33.00},
        {"ate": 99999, "frente2": 28.50, "frente3": 30.00, "verso2": 30.50, "verso3": 32.00},
    ],
    "sem_nf": [
        {"ate": 200,   "frente2": 27.30, "frente3": 28.70, "verso2": 29.12, "verso3": 30.49},
        {"ate": 400,   "frente2": 26.85, "frente3": 28.21, "verso2": 28.67, "verso3": 30.03},
        {"ate": 99999, "frente2": 25.94, "frente3": 27.30, "verso2": 27.76, "verso3": 29.12},
    ]
}

def carregar_tabela_precos_do_html(caminho):
    """Lê a tabela de fator/kg (TABELA) e a tabela de PP (PRECOS_PP) diretamente do arquivo
    HTML da calculadora oficial da Plastcustom. Assim, quando o arquivo é substituído por uma
    versão nova (com preços atualizados), o robô passa a usar os valores novos automaticamente
    na próxima vez que o serviço reiniciar - sem precisar editar nenhum código Python.
    Se o arquivo não existir ou não puder ser lido, devolve (None, None) e quem chamou usa
    a tabela padrão (TABELA_PADRAO / PRECOS_PP_PADRAO) como rede de segurança."""
    try:
        with open(caminho, encoding="utf-8") as f:
            texto = f.read()
    except (FileNotFoundError, OSError):
        return None, None

    tabela = []
    m_tabela = re.search(r'TABELA:\s*\[(.*?)\n\s*\],\s*\n\s*MAP_PRECO', texto, re.DOTALL)
    if m_tabela:
        linha_regex = re.compile(
            r"\{\s*m:\s*'([^']+)'\s*,\s*i:\s*'([^']+)'\s*,\s*c:\s*'([^']+)'\s*,"
            r"\s*v1:\s*([\d.]+)\s*,\s*v2:\s*([\d.]+)\s*,\s*v3:\s*([\d.]+)\s*\}"
        )
        for mm in linha_regex.finditer(m_tabela.group(1)):
            tabela.append({
                "m": mm.group(1), "i": mm.group(2), "c": mm.group(3),
                "v1": float(mm.group(4)), "v2": float(mm.group(5)), "v3": float(mm.group(6)),
            })

    precos_pp = {"com_nf": [], "sem_nf": []}
    m_pp = re.search(r'PRECOS_PP:\s*\{(.*?)\n\s*\},\s*\n\s*TABELA_ESPECIAL_MILHEIRO', texto, re.DOTALL)
    if m_pp:
        for chave in ("com_nf", "sem_nf"):
            m_chave = re.search(rf'{chave}:\s*\[(.*?)\]', m_pp.group(1), re.DOTALL)
            if m_chave:
                linha_regex_pp = re.compile(
                    r"\{\s*ate:\s*(\d+)\s*,\s*frente2:\s*([\d.]+)\s*,\s*frente3:\s*([\d.]+)\s*,"
                    r"\s*verso2:\s*([\d.]+)\s*,\s*verso3:\s*([\d.]+)\s*\}"
                )
                for mm in linha_regex_pp.finditer(m_chave.group(1)):
                    precos_pp[chave].append({
                        "ate": int(mm.group(1)), "frente2": float(mm.group(2)), "frente3": float(mm.group(3)),
                        "verso2": float(mm.group(4)), "verso3": float(mm.group(5)),
                    })

    # Só considera válido se extraiu uma quantidade razoável de linhas - evita usar
    # uma tabela vazia/quebrada por causa de um arquivo corrompido ou formatado diferente.
    if len(tabela) < 10 or len(precos_pp["com_nf"]) < 1:
        return None, None
    return tabela, precos_pp


_CAMINHO_CALCULADORA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Plastcustom_Orcamento.html")
_tabela_carregada, _precos_pp_carregados = carregar_tabela_precos_do_html(_CAMINHO_CALCULADORA)
if _tabela_carregada:
    TABELA = _tabela_carregada
    PRECOS_PP = _precos_pp_carregados
    logger.info(f"Tabela de preços carregada de {_CAMINHO_CALCULADORA} ({len(TABELA)} linhas)")
else:
    TABELA = TABELA_PADRAO
    PRECOS_PP = PRECOS_PP_PADRAO
    logger.warning(f"Não encontrou/não conseguiu ler {_CAMINHO_CALCULADORA} - usando tabela de preços padrão embutida no código")

MATERIAIS_VALIDOS = ["Virgem BD", "Virgem AD", "Reciclado Cor", "Reciclado Sem Cor", "Polipropileno (PP)"]
# Cor do PRODUTO (a cor da sacola em si) - diferente de "cores de impressão" (a logomarca).
# Não afeta o preço, é só uma característica visual do pedido.
CORES_PRODUTO_VALIDAS = ["Branca", "Preta", "Azul", "Vermelha", "Verde", "Amarela", "Laranja", "Cinza", "Transparente", "Natural"]
PRODUTOS_VALIDOS = ["Sacola Camiseta", "Sacola Vazada", "Saco Impresso Solda Fundo", "Saco com Aba"]

# ============================================================
# TABELA DE CILINDROS DE IMPRESSÃO — portada da calculadora HTML
# Determina quais larguras/alturas são tecnicamente possíveis de imprimir
# ============================================================
TABELA_CILINDRO_IMPRESSORA = [
    {"impressora": 1, "cilindro": 28, "cores": 3}, {"impressora": 1, "cilindro": 29, "cores": 4},
    {"impressora": 1, "cilindro": 30, "cores": 4}, {"impressora": 1, "cilindro": 32, "cores": 3},
    {"impressora": 1, "cilindro": 34, "cores": 4}, {"impressora": 1, "cilindro": 36, "cores": 4},
    {"impressora": 1, "cilindro": 38, "cores": 4}, {"impressora": 1, "cilindro": 40, "cores": 4},
    {"impressora": 1, "cilindro": 42, "cores": 3}, {"impressora": 1, "cilindro": 46, "cores": 2},
    {"impressora": 1, "cilindro": 50, "cores": 4}, {"impressora": 1, "cilindro": 52, "cores": 4},
    {"impressora": 1, "cilindro": 58, "cores": 4}, {"impressora": 1, "cilindro": 60, "cores": 4},
    {"impressora": 1, "cilindro": 68, "cores": 4}, {"impressora": 1, "cilindro": 70, "cores": 4},
    {"impressora": 1, "cilindro": 72, "cores": 2}, {"impressora": 1, "cilindro": 100, "cores": 2},
    {"impressora": 2, "cilindro": 28, "cores": 3}, {"impressora": 2, "cilindro": 29, "cores": 4},
    {"impressora": 2, "cilindro": 30, "cores": 4}, {"impressora": 2, "cilindro": 32, "cores": 3},
    {"impressora": 2, "cilindro": 34, "cores": 4}, {"impressora": 2, "cilindro": 36, "cores": 4},
    {"impressora": 2, "cilindro": 38, "cores": 4}, {"impressora": 2, "cilindro": 40, "cores": 4},
    {"impressora": 2, "cilindro": 42, "cores": 3}, {"impressora": 2, "cilindro": 46, "cores": 2},
    {"impressora": 2, "cilindro": 50, "cores": 4}, {"impressora": 2, "cilindro": 52, "cores": 4},
    {"impressora": 2, "cilindro": 58, "cores": 4}, {"impressora": 2, "cilindro": 60, "cores": 4},
    {"impressora": 2, "cilindro": 68, "cores": 4}, {"impressora": 2, "cilindro": 70, "cores": 4},
    {"impressora": 2, "cilindro": 72, "cores": 2}, {"impressora": 2, "cilindro": 100, "cores": 2},
    {"impressora": 3, "cilindro": 30, "cores": 6}, {"impressora": 3, "cilindro": 35, "cores": 6},
    {"impressora": 3, "cilindro": 42, "cores": 6}, {"impressora": 3, "cilindro": 50, "cores": 6},
    {"impressora": 3, "cilindro": 55, "cores": 4}, {"impressora": 3, "cilindro": 60, "cores": 6},
    {"impressora": 3, "cilindro": 70, "cores": 6}, {"impressora": 3, "cilindro": 80, "cores": 5},
    {"impressora": 3, "cilindro": 90, "cores": 2}, {"impressora": 3, "cilindro": 100, "cores": 4},
]
CILINDROS_DISPONIVEIS = sorted({c["cilindro"] for c in TABELA_CILINDRO_IMPRESSORA})

LARGURAS_SACOLA_CAMISETA_PERMITIDAS = [30, 35, 38, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90]

# Para cada produto: qual dimensão (largura ou altura) é limitada pelo cilindro de impressão,
# e quantas "repetições" da medida cabem no cilindro (ex.: Sacola Vazada permite até 3x a largura)
PRODUTO_REGRA_CILINDRO = {
    "Sacola Camiseta": {"dimensao": "altura", "max_rep": 1},
    "Sacola Vazada": {"dimensao": "largura", "max_rep": 3},
    "Saco Impresso Solda Fundo": {"dimensao": "altura", "max_rep": 4},
    "Saco com Aba": {"dimensao": "largura", "max_rep": 4},
}

def largura_camiseta_mais_proxima(largura):
    return min(LARGURAS_SACOLA_CAMISETA_PERMITIDAS, key=lambda v: abs(v - largura))

def disponibilidade_cilindro(medida_base, cores_n, max_rep, produto):
    """Para cada repetição possível (1x, 2x, 3x...) verifica se existe cilindro compatível."""
    resultados = []
    for rep in range(1, max_rep + 1):
        alvo = medida_base * rep
        itens = [c for c in TABELA_CILINDRO_IMPRESSORA if abs(c["cilindro"] - alvo) < 1e-6]
        # Caso especial da calculadora: medida 48 aceita o cilindro de 50 para esses 2 produtos
        if not itens and produto in ("Sacola Camiseta", "Saco Impresso Solda Fundo") and abs(alvo - 48) < 1e-6:
            itens = [c for c in TABELA_CILINDRO_IMPRESSORA if c["cilindro"] == 50]
        if itens:
            max_cores = max(c["cores"] for c in itens)
            resultados.append({"disponivel": True, "ok_cores": cores_n == 0 or cores_n <= max_cores})
    return resultados

def medida_cilindro_valida(produto, medida, cores_n, max_rep):
    return any(r["disponivel"] and r["ok_cores"] for r in disponibilidade_cilindro(medida, cores_n, max_rep, produto))

def medida_cilindro_mais_proxima(produto, medida, cores_n, max_rep):
    """Busca, entre todos os cilindros compatíveis, a medida-base mais próxima do que o cliente pediu."""
    melhor, melhor_dist = None, None
    for c in TABELA_CILINDRO_IMPRESSORA:
        if cores_n > 0 and c["cores"] < cores_n:
            continue
        for rep in range(1, max_rep + 1):
            candidato = round(c["cilindro"] / rep, 2)
            if candidato <= 0 or not medida_cilindro_valida(produto, candidato, cores_n, max_rep):
                continue
            dist = abs(candidato - medida)
            if melhor is None or dist < melhor_dist or (dist == melhor_dist and candidato < melhor):
                melhor, melhor_dist = candidato, dist
    return melhor

def ajustar_tamanho(produto, largura, altura, cores_n):
    """Ajusta largura/altura para os valores tecnicamente possíveis (com cilindro de impressão
    disponível), igual a calculadora faz automaticamente. Retorna (largura, altura, lista_de_ajustes)."""
    largura = float(largura); altura = float(altura); cores_n = int(cores_n)
    ajustes = []

    if produto == "Sacola Camiseta":
        nova_largura = largura_camiseta_mais_proxima(largura)
        if abs(nova_largura - largura) > 0.01:
            ajustes.append(f"largura ajustada de {largura:g}cm para {nova_largura:g}cm (medida disponível)")
            largura = nova_largura

    regra = PRODUTO_REGRA_CILINDRO.get(produto)
    if regra:
        dim, max_rep = regra["dimensao"], regra["max_rep"]
        valor_atual = altura if dim == "altura" else largura
        if not medida_cilindro_valida(produto, valor_atual, cores_n, max_rep):
            novo = medida_cilindro_mais_proxima(produto, valor_atual, cores_n, max_rep)
            if novo:
                ajustes.append(f"{dim} ajustada de {valor_atual:g}cm para {novo:g}cm (cilindro de impressão disponível para {cores_n} cores)")
                if dim == "altura":
                    altura = novo
                else:
                    largura = novo

    return largura, altura, ajustes

# Espessuras oficiais por produto (mm) — cada produto tem sua própria faixa
ESPESSURAS_POR_PRODUTO = {
    "Sacola Camiseta": [0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.028, 0.035, 0.045],
    "Sacola Vazada": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.045],
    "Saco Impresso Solda Fundo": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.045],
    "Saco com Aba": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014, 0.045],
}

def espessura_mais_proxima(valor, produto=None):
    """Ajusta qualquer valor informado para a opção oficial mais próxima DENTRO do produto escolhido."""
    opcoes = ESPESSURAS_POR_PRODUTO.get(produto) or sorted({e for lst in ESPESSURAS_POR_PRODUTO.values() for e in lst})
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return opcoes[0]
    return min(opcoes, key=lambda x: abs(x - v))

def lookup_pp(imp, cores_faixa, kg, tipo_nota):
    tabela = PRECOS_PP.get(tipo_nota, PRECOS_PP["com_nf"])
    faixa = next((f for f in tabela if kg <= f["ate"]), tabela[-1])
    frente_verso = "VERSO" in imp
    ate2 = cores_faixa != "3 ou + cores"
    if frente_verso:
        return faixa["verso2"] if ate2 else faixa["verso3"]
    return faixa["frente2"] if ate2 else faixa["frente3"]

def lookup_fator_kg(material, imp, cores_faixa, kg, tipo_nota="com_nf"):
    if material == "Polipropileno (PP)":
        return lookup_pp(imp, cores_faixa, kg, tipo_nota)
    row = next((r for r in TABELA if r["m"] == material and r["i"] == imp and r["c"] == cores_faixa), None)
    if not row:
        return 0
    fator_base = row["v1"] if kg <= 200 else (row["v2"] if kg <= 400 else row["v3"])
    return round(fator_base * 0.91, 2) if tipo_nota == "sem_nf" else fator_base

def calcular_pedido_minimo(largura, altura, espessura, cores_n):
    """Pedido mínimo real: 150kg com impressão / 100kg sem impressão, convertido em milheiros
    de acordo com o peso de CADA combinação de tamanho+espessura (não é um número fixo)."""
    L = float(largura); A = float(altura); E = float(espessura)
    p_mil_kg = L * A * E
    if p_mil_kg <= 0:
        return None
    pedido_min_kg = 100 if int(cores_n) == 0 else 150
    unidades_min = math.ceil((pedido_min_kg / p_mil_kg) * 1000 / 500) * 500
    return {
        "milheiros_min": unidades_min / 1000,
        "unidades_min": unidades_min,
        "kg_min": pedido_min_kg,
    }

def calcular_preco(produto, material, largura, altura, cores_n, imp, milheiros, espessura=0.028, tipo_nota="com_nf"):
    """
    Calcula o preço EXATO seguindo a mesma lógica da calculadora oficial da Plastcustom.
    Não inclui clichê (cobrado à parte, conforme já informado pelo robô ao cliente).
    """
    L = float(largura)
    A = float(altura)
    E = float(espessura)
    MILH = float(milheiros)
    cores_n = int(cores_n)

    area = L * A
    vol = area * E
    p_un_g = vol            # peso por unidade (g) — mesma fórmula da calculadora
    p_mil_kg = p_un_g       # "peso do milheiro" no sentido usado pela calculadora
    total_kg = p_mil_kg * MILH

    cores_faixa = "até 2 cores" if cores_n <= 2 else "3 ou + cores"
    preco_kg = lookup_fator_kg(material, imp, cores_faixa, total_kg, tipo_nota)

    if preco_kg <= 0:
        raise ValueError(f"Combinação sem preço na tabela: {material} / {imp} / {cores_faixa}")

    # Sem impressão: desconto de R$2 no fator kg (regra da calculadora), exceto PP
    if cores_n == 0 and material != "Polipropileno (PP)":
        preco_kg -= 2

    mil_base = preco_kg * p_mil_kg

    # Regra: milheiro < 1,5kg soma R$3,00 no fator kg
    adicional_fator_kg = 3 if 0 < p_mil_kg < 1.5 else 0
    adicional_mil = adicional_fator_kg * p_mil_kg

    mil = mil_base + adicional_mil
    unitario = mil / 1000
    total = mil * MILH

    pedido_min_kg = 100 if cores_n == 0 else 150
    minimo = calcular_pedido_minimo(L, A, E, cores_n)

    return {
        "preco_kg": round(preco_kg, 2),
        "unitario": round(unitario, 4),
        "milheiro": round(mil, 2),
        "total": round(total, 2),
        "peso_total_kg": round(total_kg, 2),
        "espessura_usada": round(E, 3),
        "pedido_minimo_kg": pedido_min_kg,
        "pedido_minimo_milheiros": minimo["milheiros_min"] if minimo else None,
        "atende_minimo": total_kg >= pedido_min_kg,
    }

CAMPOS_OBRIGATORIOS_ITEM = ["produto", "largura", "altura", "espessura", "cores_n", "impressao", "milheiros"]

def processar_item_pedido(item):
    """Valida e ajusta UM item do pedido (tamanho/espessura), calcula o que falta, e
    gera uma prévia de preço se já estiver completo. Função pura (sem banco de dados),
    o que a deixa fácil de testar isoladamente."""
    ajustes = []
    produto = item.get("produto")
    material = item.get("material")
    if material is not None and material not in MATERIAIS_VALIDOS:
        material = None
    cor_produto = item.get("cor_produto")
    if cor_produto is not None and cor_produto not in CORES_PRODUTO_VALIDAS:
        cor_produto = None
    largura = item.get("largura")
    altura = item.get("altura")
    espessura = item.get("espessura")
    cores_n = item.get("cores_n")
    impressao = item.get("impressao")
    if impressao is not None and impressao not in ("FRENTE", "FRENTE_VERSO"):
        impressao = None
    milheiros = item.get("milheiros")

    if produto in PRODUTOS_VALIDOS and largura is not None and altura is not None and cores_n is not None:
        try:
            largura, altura, ajustes_tam = ajustar_tamanho(produto, largura, altura, int(cores_n))
            ajustes.extend(ajustes_tam)
        except (TypeError, ValueError):
            pass

    if produto in PRODUTOS_VALIDOS and espessura is not None:
        try:
            espessura_antiga = float(espessura)
            espessura = espessura_mais_proxima(espessura, produto)
            if abs(espessura - espessura_antiga) > 1e-6:
                ajustes.append(f"espessura ajustada de {espessura_antiga:g}mm para {espessura:g}mm (opção disponível para este produto)")
        except (TypeError, ValueError):
            pass

    item_normalizado = {
        "produto": produto, "material": material, "cor_produto": cor_produto,
        "largura": largura, "altura": altura, "espessura": espessura,
        "cores_n": cores_n, "impressao": impressao, "milheiros": milheiros,
    }
    faltando = [c for c in CAMPOS_OBRIGATORIOS_ITEM if item_normalizado.get(c) is None]
    completo = not faltando

    preco_preview = None
    if completo:
        try:
            imp_map = "IMPRESSÃO FRENTE / VERSO" if impressao == "FRENTE_VERSO" else "IMPRESSÃO FRENTE"
            calc = calcular_preco(produto, material or "Virgem BD", largura, altura, int(cores_n), imp_map, milheiros, espessura=espessura)
            preco_preview = {
                "preco_por_milheiro": calc["milheiro"], "preco_total": calc["total"],
                "atende_minimo": calc["atende_minimo"], "pedido_minimo_milheiros": calc["pedido_minimo_milheiros"],
            }
        except Exception as e:
            logger.warning(f"Não foi possível gerar prévia de preço para item: {e}")

    return {"item": item_normalizado, "ajustes": ajustes, "faltando": faltando, "completo": completo, "preco_preview": preco_preview}

def executar_atualizar_pedido(conversa_id, entrada):
    """Executa a ferramenta 'atualizar_pedido': é o coração da memória estruturada da
    conversa. Recebe TODOS os itens que a IA já sabe sobre o pedido (um ou vários
    tamanhos/produtos), valida/ajusta cada um, salva como estado da conversa, e
    devolve o que falta em cada item - assim a IA nunca precisa perguntar de novo
    algo que já foi respondido."""
    try:
        itens_entrada = entrada.get("itens") or []
        resultados = [processar_item_pedido(it) for it in itens_entrada]
        estado = {"itens": [r["item"] for r in resultados], "observacoes": entrada.get("observacoes", "")}
        salvar_estado_pedido(conversa_id, estado)
        return {
            "itens": [
                {**r["item"], "ajustes_feitos": r["ajustes"], "faltando": r["faltando"],
                 "completo": r["completo"], "preco_preview": r["preco_preview"]}
                for r in resultados
            ],
            "observacoes": estado["observacoes"],
            "total_itens": len(resultados),
            "itens_completos": sum(1 for r in resultados if r["completo"]),
        }
    except Exception as e:
        logger.error(f"Erro na ferramenta atualizar_pedido: {e}")
        return {"erro": "Não foi possível atualizar o pedido agora. Continue a conversa normalmente e tente de novo em seguida."}

def executar_consultar_pedido_minimo(entrada):
    """Executa a ferramenta 'consultar_pedido_minimo': ajusta tamanho/espessura para valores
    tecnicamente válidos e calcula o pedido mínimo real dessa combinação."""
    try:
        produto = entrada.get("produto")
        largura = float(entrada["largura"])
        altura = float(entrada["altura"])
        espessura_pedida = float(entrada["espessura"])
        cores_n = int(entrada["cores_n"])

        largura, altura, ajustes = ajustar_tamanho(produto, largura, altura, cores_n)
        espessura = espessura_mais_proxima(espessura_pedida, produto)
        if abs(espessura - espessura_pedida) > 1e-6:
            ajustes.append(f"espessura ajustada de {espessura_pedida:g}mm para {espessura:g}mm (opção disponível para este produto)")

        minimo = calcular_pedido_minimo(largura, altura, espessura, cores_n)
        return {
            "largura_usada": largura, "altura_usada": altura, "espessura_usada": espessura,
            "ajustes_feitos": ajustes,
            "pedido_minimo_milheiros": minimo["milheiros_min"] if minimo else None,
            "pedido_minimo_kg": minimo["kg_min"] if minimo else None,
        }
    except Exception as e:
        logger.error(f"Erro na ferramenta consultar_pedido_minimo: {e}")
        return {"erro": "Não foi possível calcular o mínimo para esses dados. Peça para o cliente confirmar produto, tamanho e espessura novamente."}

def executar_calcular_orcamento(entrada):
    """Executa a ferramenta 'calcular_orcamento': é a ÚNICA forma pela qual um preço final
    chega até o cliente. A IA nunca calcula preço sozinha - só usa o que esta função devolve."""
    try:
        produto = entrada.get("produto")
        material = entrada.get("material") or "Virgem BD"
        if material not in MATERIAIS_VALIDOS:
            material = "Virgem BD"
        cor_produto = entrada.get("cor_produto") or "Transparente"
        if cor_produto not in CORES_PRODUTO_VALIDAS:
            cor_produto = "Transparente"
        largura = float(entrada["largura"])
        altura = float(entrada["altura"])
        espessura_pedida = float(entrada["espessura"])
        cores_n = int(entrada["cores_n"])
        impressao = entrada.get("impressao") or "FRENTE"
        imp_map = "IMPRESSÃO FRENTE / VERSO" if impressao == "FRENTE_VERSO" else "IMPRESSÃO FRENTE"
        milheiros = float(entrada["milheiros"])

        largura, altura, ajustes = ajustar_tamanho(produto, largura, altura, cores_n)
        espessura = espessura_mais_proxima(espessura_pedida, produto)
        if abs(espessura - espessura_pedida) > 1e-6:
            ajustes.append(f"espessura ajustada de {espessura_pedida:g}mm para {espessura:g}mm (opção disponível para este produto)")

        calc = calcular_preco(produto, material, largura, altura, cores_n, imp_map, milheiros, espessura=espessura)

        if not calc["atende_minimo"]:
            return {
                "erro": "peso abaixo do mínimo exigido para esta combinação",
                "pedido_minimo_milheiros": calc["pedido_minimo_milheiros"],
                "peso_calculado_kg": calc["peso_total_kg"],
                "peso_minimo_kg": calc["pedido_minimo_kg"],
                "instrucao": "Explique ao cliente que não é possível fechar nesse peso e peça para aumentar a quantidade para o mínimo informado.",
            }

        return {
            "produto": produto, "material": material, "cor_produto": cor_produto,
            "largura_usada": largura, "altura_usada": altura, "espessura_usada": espessura,
            "cores_n": cores_n, "impressao": impressao, "milheiros": milheiros,
            "ajustes_feitos": ajustes,
            "preco_por_milheiro": calc["milheiro"],
            "preco_total": calc["total"],
            "peso_total_kg": calc["peso_total_kg"],
        }
    except Exception as e:
        logger.error(f"Erro na ferramenta calcular_orcamento: {e}")
        return {"erro": "Não foi possível calcular o preço para esta combinação agora. Diga ao cliente que vai confirmar com a equipe e retornar em breve. NÃO informe nenhum valor."}

TOOLS = [
    {
        "name": "atualizar_pedido",
        "description": "Chame esta ferramenta SEMPRE que o cliente informar qualquer dado novo sobre o pedido - mesmo que venha tudo numa mensagem só, ou aos poucos. Pode incluir MAIS DE UM item se o cliente mencionar vários tamanhos ou produtos na mesma mensagem (ex: '30x40, 40x50 e 50x50'). Envie a lista COMPLETA de itens que você já conhece desta conversa (os de antes + os novos) - esta ferramenta substitui o estado anterior pelo que você enviar. O resultado te diz o que falta em cada item, então você nunca precisa perguntar de novo o que já foi informado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "description": "Lista de TODOS os itens do pedido que você já sabe (um item = um produto+tamanho). Inclua os já conhecidos de mensagens anteriores MAIS os novos desta mensagem.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "produto": {"type": "string", "enum": PRODUTOS_VALIDOS},
                            "material": {"type": "string", "enum": MATERIAIS_VALIDOS},
                            "cor_produto": {"type": "string", "enum": CORES_PRODUTO_VALIDAS},
                            "largura": {"type": "number", "description": "largura em cm"},
                            "altura": {"type": "number", "description": "altura em cm"},
                            "espessura": {"type": "number", "description": "espessura em mm"},
                            "cores_n": {"type": "integer", "description": "número de cores de impressão (0 se sem impressão)"},
                            "impressao": {"type": "string", "enum": ["FRENTE", "FRENTE_VERSO"]},
                            "milheiros": {"type": "number", "description": "quantidade em milheiros (mil unidades)"},
                        },
                    },
                },
                "observacoes": {"type": "string", "description": "Qualquer observação extra relevante sobre o pedido (opcional)."},
            },
            "required": ["itens"],
        },
    },
    {
        "name": "consultar_pedido_minimo",
        "description": "Consulta o pedido mínimo (em mil unidades) para uma combinação de produto+tamanho+espessura+cores, ANTES de perguntar a quantidade ao cliente. Também ajusta tamanho/espessura para os valores tecnicamente disponíveis, se necessário. Use assim que tiver produto, largura, altura, espessura e número de cores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "produto": {"type": "string", "enum": PRODUTOS_VALIDOS},
                "largura": {"type": "number", "description": "largura em cm"},
                "altura": {"type": "number", "description": "altura em cm"},
                "espessura": {"type": "number", "description": "espessura em mm"},
                "cores_n": {"type": "integer", "description": "número de cores de impressão (0 se sem impressão)"},
            },
            "required": ["produto", "largura", "altura", "espessura", "cores_n"],
        },
    },
    {
        "name": "calcular_orcamento",
        "description": "Calcula o preço OFICIAL e final do pedido. É a única forma válida de informar preço ao cliente - NUNCA calcule ou estime um valor por conta própria. Use somente quando já tiver TODAS as informações: produto, material, tamanho, espessura, cores e quantidade em milheiros.",
        "input_schema": {
            "type": "object",
            "properties": {
                "produto": {"type": "string", "enum": PRODUTOS_VALIDOS},
                "material": {"type": "string", "enum": MATERIAIS_VALIDOS, "description": "usa 'Virgem BD' se o cliente não especificou"},
                "cor_produto": {"type": "string", "enum": CORES_PRODUTO_VALIDAS, "description": "cor da sacola em si (não afeta o preço, é só informativo). Use 'Transparente' se o cliente não especificou."},
                "largura": {"type": "number", "description": "largura em cm"},
                "altura": {"type": "number", "description": "altura em cm"},
                "espessura": {"type": "number", "description": "espessura em mm"},
                "cores_n": {"type": "integer", "description": "número de cores de impressão (0 se sem impressão)"},
                "impressao": {"type": "string", "enum": ["FRENTE", "FRENTE_VERSO"]},
                "milheiros": {"type": "number", "description": "quantidade pedida, em milheiros (mil unidades)"},
            },
            "required": ["produto", "material", "largura", "altura", "espessura", "cores_n", "impressao", "milheiros"],
        },
    },
    {
        "name": "fechar_pedido",
        "description": "Chame esta ferramenta assim que o cliente confirmar claramente que quer fechar/prosseguir com o pedido (ex.: respondeu 'sim' depois de você perguntar 'Posso gerar a proposta?'). Isso avisa o consultor humano para finalizar a venda. Só chame depois de já ter apresentado um preço calculado (via calcular_orcamento) nesta conversa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resumo": {
                    "type": "string",
                    "description": "Resumo em texto corrido do pedido fechado: produto, tamanho, material, espessura, cores, quantidade em mil unidades e o preço total combinado.",
                },
            },
            "required": ["resumo"],
        },
    },
    {
        "name": "transferir_para_consultor",
        "description": "Chame esta ferramenta quando você não souber responder algo importante ao cliente, quando a pergunta estiver fora do que você sabe (fora de vendas de sacolas/sacos plásticos), ou quando o cliente pedir claramente para falar com uma pessoa/atendente humano. Isso avisa um consultor humano para assumir a conversa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Resumo breve do que o cliente perguntou ou precisa, que você não conseguiu resolver sozinho.",
                },
            },
            "required": ["motivo"],
        },
    },
    {
        "name": "solicitar_privacidade",
        "description": "Chame esta ferramenta sempre que o cliente perguntar sobre os dados pessoais dele (o que guardamos, por quê), ou pedir para ver, corrigir, ou APAGAR/EXCLUIR os dados dele, ou se opuser ao uso dos dados (direitos da LGPD). NÃO apague nada sozinho - isso avisa um humano que vai executar o pedido com segurança.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["acesso", "correcao", "exclusao", "duvida"], "description": "acesso = quer saber o que temos; correcao = quer corrigir algo; exclusao = quer apagar os dados; duvida = pergunta geral sobre privacidade"},
                "detalhe": {"type": "string", "description": "O que exatamente o cliente pediu, em texto corrido."},
            },
            "required": ["tipo", "detalhe"],
        },
    },
]

SYSTEM_PROMPT = """Você é o Rui, vendedor de alta performance da Plastcustom. Conhece cada detalhe dos produtos e fecha vendas com naturalidade, como um vendedor humano experiente — não como um formulário sequencial. Nunca mencione catálogo, sistema ou virtual.

PRODUTOS:
1. Sacola Camiseta - alça integrada no corpo
2. Sacola Vazada - alça recortada no plástico
3. Saco Impresso Solda Fundo - saco liso com solda no fundo
4. Saco com Aba - saco com dobra superior

TAMANHOS: largura e altura são flexíveis dentro do que a produção consegue imprimir (não é uma lista curta fixa!).
  A ferramenta atualizar_pedido valida automaticamente se o tamanho pedido tem cilindro de impressão disponível;
  se não tiver, ela já ajusta para o tamanho tecnicamente mais próximo (aparece em "ajustes_feitos" no resultado).
  Quando isso acontecer, informe ao cliente de forma transparente. NUNCA diga que um tamanho "não existe" por conta própria.
MATERIAIS: Virgem BD (padrão) / Virgem AD (resistente) / PP (transparente) / Reciclado
CORES DO PRODUTO (a cor da sacola em si - diferente da cor de impressão da logomarca!):
  Branca / Preta / Azul / Vermelha / Verde / Amarela / Laranja / Cinza / Transparente / Natural
  Não afeta o preço. "Transparente" é o padrão se o cliente não escolher.
ESPESSURAS DISPONÍVEIS (mm) — cada produto tem sua própria faixa:
  - Sacola Camiseta: 0,003 / 0,004 / 0,005 / 0,006 / 0,007 / 0,008 / 0,009 / 0,028 / 0,035 / 0,045
  - Sacola Vazada, Saco Impresso Solda Fundo, Saco com Aba: 0,004 / 0,005 / 0,006 / 0,007 / 0,008 / 0,009 / 0,010 / 0,011 / 0,012 / 0,013 / 0,014 / 0,045
IMPRESSÃO: até 6 cores, frente e/ou verso. Clichê cobrado à parte na primeira compra.

COMO CONVERSAR — O NÚCLEO DE COMO VOCÊ DEVE SE COMPORTAR:
- Você é um vendedor de verdade tendo uma conversa, não um formulário lendo perguntas em ordem fixa.
- SEMPRE extraia TODAS as informações que o cliente já deu numa mensagem, mesmo vindo várias juntas
  (ex: "quero sacola vazada 30x40, 40x50 e 50x50, reciclado, 3 cores frente, 30 mil cada" já te dá
  produto, 3 tamanhos diferentes, material, impressão e quantidade de uma vez - capture tudo já).
- Depois de capturar o que puder (chamando atualizar_pedido), pergunte SÓ o que realmente falta. Pode
  perguntar mais de uma coisa junto quando fizer sentido (ex: "e qual material e cor você prefere?"),
  mas evite jogar muitas perguntas de uma vez - agrupe no máximo 2-3 relacionadas por resposta.
- NUNCA pergunte de novo algo que já está no ESTADO ATUAL DO PEDIDO (fornecido no contexto desta mensagem).
  Se ele já mostra produto="Sacola Vazada", não pergunte de novo qual produto.
- Exceção: se o cliente disser algo que contradiz o que já foi informado, pergunte pra esclarecer em vez
  de simplesmente substituir sem avisar.

MÚLTIPLOS TAMANHOS OU PRODUTOS NO MESMO PEDIDO:
- Trate cada combinação de produto+tamanho como um ITEM separado na lista "itens" de atualizar_pedido.
- Sempre mande a lista COMPLETA de itens que você já conhece (os de antes + os novos) - a ferramenta
  substitui o estado anterior, não soma automaticamente.
- Ao apresentar o orçamento final, mostre o preço de CADA item e depois o total geral.
- Se um item ficar incompleto, continue perguntando só sobre ele - os outros itens já completos não
  precisam esperar para serem calculados.

TROCA DE PRODUTO NO MEIO DA CONVERSA:
- Se o cliente trocar de produto (ex: de "Saco Impresso Solda Fundo" para "Sacola Vazada"), mantenha
  tudo que ainda faz sentido (material, cor, quantidade, número de cores) e só pergunte de novo o que
  realmente muda entre os produtos (espessura e tamanho têm regras próprias por produto e são
  revalidadas automaticamente pela ferramenta).

TENTE RESPONDER ANTES DE TRANSFERIR:
- Você sabe bastante sobre produtos, preços, prazos, condições e processo - perguntas técnicas sobre
  isso (diferença entre produtos, o que é clichê, como funciona o pedido mínimo, prazo, pagamento,
  diferença entre materiais) você responde DIRETAMENTE, sem transferir.
- Só use transferir_para_consultor quando a pergunta for GENUINAMENTE fora do que você sabe: reclamação,
  status de pedido já entregue, assunto não relacionado à compra, ou pedido explícito de falar com uma
  pessoa. Tentar responder primeiro é sempre melhor que transferir cedo demais.

COMO APRESENTAR OPÇÕES DE MENU (produto, material, cor, espessura, número de cores):
- Formato numerado:
  1. Primeira opção
  2. Segunda opção
  Depois, uma linha curta tipo "Pode responder só com o número 😊".
- Não use bullets (•) nem travessões soltos - sempre números.
- Aceite tanto o número quanto o nome quando o cliente responder.
- Tamanho (largura x altura) é pergunta aberta, não vira menu numerado.

PRIVACIDADE E DADOS PESSOAIS (LGPD):
- Guardamos telefone, nome e histórico da conversa, só para atender bem e gerar orçamento.
- Na PRIMEIRA mensagem desta conversa (indicado no contexto), inclua no final da resposta, de forma
  curta e natural: "Ah, e só pra constar: guardo nossa conversa aqui pra te atender melhor - se quiser
  saber mais sobre isso ou pedir pra apagar em algum momento, é só falar 😊"
- Pedido de ver/corrigir/apagar dados → chame solicitar_privacidade (nunca prometa que já apagou nada).

FERRAMENTAS:
- atualizar_pedido: chame toda vez que aprender QUALQUER dado novo (mesmo parcial, mesmo vários de
  uma vez). É o que mantém sua memória estruturada - sempre mande a lista completa de itens conhecidos.
- consultar_pedido_minimo: opcional, útil pra confirmar o mínimo de um item antes dele estar completo
  (atualizar_pedido já mostra isso no preview de cada item quando aplicável).
- calcular_orcamento: chame para obter o PREÇO OFICIAL FINAL de um item completo, antes de apresentar
  qualquer valor ao cliente como definitivo. Nunca invente ou estime preço por conta própria.
- fechar_pedido: chame quando o cliente confirmar que quer fechar (depois de já ver o preço oficial).
- transferir_para_consultor: só depois de tentar responder você mesmo.
- solicitar_privacidade: pedidos relacionados a dados pessoais (LGPD).
- Se uma ferramenta devolver "erro", NÃO informe nenhum valor - siga a instrução que vier junto do erro.

CONDIÇÕES:
- Pedido mínimo: NÃO é fixo — sempre calculado pelas ferramentas, varia por peso de cada item.
- Prazo: 30 a 40 dias úteis após aprovação da arte
- Frete: FOB Curitiba-PR ou CIF negociado
- Pagamento: 28 dias ou 28/56 dias
- Validade da proposta: 7 dias
- Clichê: cobrado à parte na primeira compra (valor confirmado pela equipe, não calculado automaticamente)

FORMATAÇÃO DE MENSAGENS:
- O WhatsApp NÃO entende tabelas em Markdown (símbolos | e ---). NUNCA use esse formato.
- O WhatsApp entende: *negrito* (um asterisco de cada lado) e quebras de linha normais.
- Ao apresentar o orçamento final de UM item, use:

*Orçamento Plastcustom* 🎉

*Produto:* [produto] [largura]x[altura]cm
*Material:* [material]
*Cor:* [cor do produto]
*Espessura:* [espessura]mm
*Impressão:* [cores] cores, [frente/frente e verso]
*Quantidade:* [milheiros] mil unidades

*Preço por milheiro:* R$ [valor]
*Preço total:* R$ [valor]

Prazo de 30 a 40 dias úteis após aprovação da arte. Pagamento em 28 dias ou 28/56 dias. Proposta válida por 7 dias.

Posso gerar a proposta para você?

- Se houver MAIS DE UM item, liste cada um nesse formato (de forma compacta) e feche com *Total geral:* R$ [soma].

OBJEÇÕES:
- Tá caro: mostre custo por unidade e sugira quantidade maior
- Vou pensar: Posso segurar esse preço por 7 dias
- Pouco: explique o pedido mínimo real daquele item (calculado pela ferramenta)

REGRAS GERAIS:
- Máximo 3-4 parágrafos por resposta
- Tom confiante, direto, natural - como um vendedor experiente, não um script"""

SINAIS = {
    "perguntou_preco": (["preço","valor","custa","quanto","tabela"], 20),
    "perguntou_prazo": (["prazo","entrega","quando","dias"], 15),
    "escolheu_modelo": (["camiseta","vazada","impresso","aba","sacola","saco"], 25),
    "escolheu_tamanho": (["30x40","40x50","50x60","60x80","80x100","tamanho","medida"], 20),
    "escolheu_quantidade": (["mil","unidades","quantidade"], 30),
    "pediu_orcamento": (["orçamento","proposta","cotação","calcul"], 35),
    "tem_empresa": (["empresa","loja","mercado","farmácia","padaria","cnpj","supermercado"], 15),
    "mandou_logo": (["logo","logomarca","arquivo","arte"], 40),
    "confirmou_pedido": (["confirmo","quero fechar","fechado","pode gerar","sim pode","fecha pedido","fecha o pedido"], 50),
    "vou_pensar": (["pensar","depois","talvez","não sei"], -10),
    "ta_caro": (["caro","salgado","muito caro"], -15),
}

def limpar_telefone(telefone):
    return re.sub(r'[^0-9]', '', telefone)[:20]

_db_pool = None

def get_pool():
    """Cria o pool de conexões só na primeira vez que for realmente necessário
    (não ao importar o arquivo). Isso também é mais seguro com o Gunicorn:
    cada processo worker cria o seu próprio pool depois de nascer."""
    global _db_pool
    if _db_pool is None:
        _db_pool = pg_pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _db_pool

def get_db():
    return get_pool().getconn()

def release_db(db):
    get_pool().putconn(db)

def buscar_ou_criar_cliente(telefone):
    telefone = limpar_telefone(telefone)
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        # Tenta o jeito seguro contra corrida: só funciona se existir uma restrição
        # única (UNIQUE) na coluna telefone. Se duas mensagens chegarem ao mesmo tempo
        # do mesmo número, o banco garante que só um registro é criado.
        cur.execute(
            "INSERT INTO clientes (telefone) VALUES (%s) ON CONFLICT (telefone) DO NOTHING RETURNING *",
            (telefone,)
        )
        c = cur.fetchone()
        db.commit()
        if not c:
            cur.execute("SELECT * FROM clientes WHERE telefone=%s", (telefone,))
            c = cur.fetchone()
    except psycopg2.Error:
        # Não existe restrição única na tabela ainda -> volta pro comportamento antigo
        # (funciona, mas sem a proteção total contra corrida). Veja nota no chat sobre
        # como adicionar essa restrição no banco.
        db.rollback()
        cur.execute("SELECT * FROM clientes WHERE telefone=%s", (telefone,))
        c = cur.fetchone()
        if not c:
            cur.execute("INSERT INTO clientes (telefone) VALUES (%s) RETURNING *", (telefone,))
            c = cur.fetchone()
            db.commit()
    cur.close(); release_db(db)
    return dict(c)

def buscar_ou_criar_conversa(cliente_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM conversas WHERE cliente_id=%s AND status='ativa' ORDER BY inicio DESC LIMIT 1", (cliente_id,))
    c = cur.fetchone()
    if not c:
        cur.execute("INSERT INTO conversas (cliente_id) VALUES (%s) RETURNING *", (cliente_id,))
        c = cur.fetchone()
        db.commit()
    cur.close(); release_db(db)
    return dict(c)

def obter_estado_pedido(conversa_id):
    """Lê a memória estruturada do pedido (produto/tamanho/material/etc, podendo ter
    vários itens) salva no banco. Se a coluna ainda não existir (ver instruções de
    migração), volta um estado vazio em vez de quebrar - o robô continua funcionando,
    só sem a memória persistente até a coluna ser criada."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT estado_pedido FROM conversas WHERE id=%s", (conversa_id,))
        row = cur.fetchone()
        estado = row["estado_pedido"] if row else None
        return estado if estado else {"itens": [], "observacoes": ""}
    except psycopg2.Error:
        db.rollback()
        return {"itens": [], "observacoes": ""}
    finally:
        cur.close(); release_db(db)

def salvar_estado_pedido(conversa_id, estado):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE conversas SET estado_pedido=%s WHERE id=%s", (Json(estado), conversa_id))
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível salvar estado_pedido (a coluna pode não existir ainda no banco): {e}")
    finally:
        cur.close(); release_db(db)

def marcar_conversa_fechada(conversa_id):
    """Depois que um pedido fecha, a conversa não fica mais 'ativa' - assim, se o mesmo
    cliente mandar mensagem de novo no futuro (um pedido novo), ele começa com uma
    memória de pedido limpa, em vez de arrastar o pedido antigo já fechado."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE conversas SET status='fechada' WHERE id=%s", (conversa_id,))
        db.commit()
    except psycopg2.Error as e:
        db.rollback()
        logger.warning(f"Não foi possível marcar conversa como fechada: {e}")
    finally:
        cur.close(); release_db(db)

def salvar_mensagem(conversa_id, remetente, conteudo):
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO mensagens (conversa_id, remetente, conteudo) VALUES (%s,%s,%s)", (conversa_id, remetente, conteudo))
    cur.execute("UPDATE conversas SET ultima_mensagem=NOW() WHERE id=%s", (conversa_id,))
    db.commit(); cur.close(); release_db(db)

def obter_historico(conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT remetente, conteudo FROM mensagens WHERE conversa_id=%s ORDER BY timestamp DESC LIMIT 30", (conversa_id,))
    msgs = list(reversed(cur.fetchall()))
    cur.close(); release_db(db)
    return msgs

def calcular_score(conversa_id, cliente_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT conteudo FROM mensagens WHERE conversa_id=%s AND remetente='cliente'", (conversa_id,))
    msgs = cur.fetchall()
    score = 0
    detectados = set()
    for msg in msgs:
        texto = msg["conteudo"].lower()
        for sinal, (keywords, pts) in SINAIS.items():
            if sinal not in detectados and any(k in texto for k in keywords):
                score += pts
                detectados.add(sinal)
    score = max(0, min(100, score))
    categoria = "quente" if score >= 80 else "morno" if score >= 50 else "frio"
    cur.execute("INSERT INTO leads (cliente_id, conversa_id, score, categoria) VALUES (%s,%s,%s,%s)", (cliente_id, conversa_id, score, categoria))
    cur.execute("UPDATE conversas SET lead_score=%s WHERE id=%s", (score, conversa_id))
    db.commit(); cur.close(); release_db(db)
    return {"score": score, "categoria": categoria}

def enviar_whatsapp(telefone, mensagem, instance="automacao"):
    """
    Usado SOMENTE para notificar o proprietário sobre leads quentes.
    A resposta ao cliente é enviada pelo n8n (não duplicar aqui).
    """
    url = f"{EVOLUTION_URL}/message/sendText/{instance}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_KEY}
    # formato correto da Evolution API v2: "number" e "text" no nível raiz
    payload = {"number": telefone, "text": mensagem}
    logger.info(f"Enviando WhatsApp para {telefone} via {instance}")
    logger.info(f"URL de envio: {url}")
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"Resposta da Evolution API: status={r.status_code} corpo={r.text[:200]}")
    except Exception as e:
        logger.error(f"Erro ao enviar WhatsApp: {e}")

def notificar_proprietario(cliente, score, conversa_id):
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM notificacoes WHERE cliente_id=%s AND tipo='lead_quente' AND enviada_em > NOW() - INTERVAL '24 hours'", (cliente["id"],))
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = f"LEAD QUENTE PLASTCUSTOM\n\nCliente: {nome}\nTelefone: +{cliente['telefone']}\nScore: {score}%\n\nCliente pronto para fechar! Entre em contato agora."
    enviar_whatsapp(PROPRIETARIO, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'lead_quente')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)

def notificar_privacidade(cliente, conversa_id, tipo, detalhe):
    """Avisa o responsável (você) sobre um pedido relacionado a dados pessoais (LGPD) -
    acesso, correção, exclusão ou dúvida. NÃO apaga nada automaticamente: pedidos de
    exclusão precisam ser tratados por um humano, com cuidado, já que envolvem dados
    de negócio (histórico de conversa, negociação de preço, etc)."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='privacidade' AND enviada_em > NOW() - INTERVAL '24 hours'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    rotulo = {"acesso": "QUER VER OS DADOS", "correcao": "QUER CORRIGIR DADOS", "exclusao": "QUER EXCLUIR DADOS (LGPD)", "duvida": "DÚVIDA SOBRE PRIVACIDADE"}.get(tipo, tipo.upper())
    msg = (
        f"PEDIDO DE PRIVACIDADE - {rotulo}\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"Detalhe: {detalhe}\n\n"
        "Trate esse pedido diretamente com o cliente (a LGPD pede resposta em prazo razoável)."
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'privacidade')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)

def notificar_transferencia(cliente, conversa_id, motivo):
    """Avisa o consultor que o robô não conseguiu ajudar e precisa de um humano.
    Tem um intervalo de 2h entre avisos pra mesma conversa, pra não virar spam
    se o cliente continuar perguntando coisas fora do que o robô sabe."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='transferencia' AND enviada_em > NOW() - INTERVAL '2 hours'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = (
        "CLIENTE PRECISA DE AJUDA HUMANA - PLASTCUSTOM\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"Motivo: {motivo}\n\n"
        "O robô já avisou o cliente que um consultor vai assumir a conversa."
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'transferencia')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)

def notificar_pedido_fechado(cliente, conversa_id, resumo):
    """Envia o resumo do pedido (escrito pela própria IA, via a ferramenta fechar_pedido)
    para o CONSULTOR_TELEFONE. Tem um intervalo curto (5 min) só pra evitar notificação
    duplicada se a IA chamar a ferramenta duas vezes seguidas pela mesma confirmação -
    mas NÃO bloqueia pedidos novos/diferentes feitos depois, na mesma conversa."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='pedido_fechado' AND enviada_em > NOW() - INTERVAL '5 minutes'",
        (conversa_id,)
    )
    if cur.fetchone():
        cur.close(); release_db(db); return
    nome = cliente.get("nome") or cliente["telefone"]
    msg = (
        "PEDIDO FECHADO - PLASTCUSTOM\n\n"
        f"Cliente: {nome}\n"
        f"Telefone: +{cliente['telefone']}\n\n"
        f"{resumo}\n\n"
        "Entre em contato para finalizar!"
    )
    enviar_whatsapp(CONSULTOR_TELEFONE, msg)
    cur.execute("INSERT INTO notificacoes (cliente_id, conversa_id, tipo) VALUES (%s,%s,'pedido_fechado')", (cliente["id"], conversa_id))
    db.commit(); cur.close(); release_db(db)

@app.route("/webhook", methods=["POST"])
def webhook():
    # Autenticação: só aceita chamadas que tragam o segredo combinado com o n8n.
    # Sem isso, qualquer pessoa na internet que descobrisse esse endereço poderia
    # gastar seus créditos de IA e mandar mensagens em nome do robô.
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401

    data = request.get_json()
    telefone_raw = data.get("telefone","").strip()
    mensagem = data.get("mensagem","").strip()[:2000]  # limite defensivo contra payloads abusivos
    instance = data.get("instance","automacao")
    if not telefone_raw or not mensagem:
        return jsonify({"erro": "dados incompletos"}), 400
    try:
        cliente = buscar_ou_criar_cliente(telefone_raw)
        conversa = buscar_ou_criar_conversa(cliente["id"])
        salvar_mensagem(conversa["id"], "cliente", mensagem)
        historico = obter_historico(conversa["id"])

        # Monta o histórico como mensagens de verdade (user/assistant), não como um texto único.
        # Isso é o formato correto da API de mensagens da Claude, e permite usar tool use.
        messages = []
        for m in historico[:-1]:
            role = "user" if m["remetente"] == "cliente" else "assistant"
            messages.append({"role": role, "content": m["conteudo"]})
        messages.append({"role": "user", "content": mensagem})

        # Se não há nenhuma mensagem anterior nesta conversa, é o primeiro contato -
        # a IA deve incluir a nota curta de privacidade (LGPD) na resposta.
        system_prompt_final = SYSTEM_PROMPT
        if not historico[:-1]:
            system_prompt_final += "\n\nCONTEXTO: esta é a PRIMEIRA mensagem desta conversa - inclua a nota curta de privacidade no final da sua resposta, como instruído acima."

        # Injeta a memória estruturada do pedido (persistida no banco) diretamente no
        # contexto - assim a IA tem uma fonte confiável do que já foi informado, em vez
        # de precisar reler e "adivinhar" a partir do texto cru da conversa toda vez.
        estado_atual = obter_estado_pedido(conversa["id"])
        if estado_atual.get("itens"):
            system_prompt_final += "\n\nESTADO ATUAL DO PEDIDO (já confirmado nesta conversa - NÃO pergunte de novo o que já está aqui):\n" + json.dumps(estado_atual, ensure_ascii=False)

        # === Uma única "conversa" com a IA, que pode chamar ferramentas quando precisar ===
        # (antes eram sempre 2 chamadas de IA por mensagem: uma pra extrair dados, outra pra responder.
        # Agora é 1 chamada normalmente, e só usa uma 2ª quando a IA realmente precisa calcular algo.)
        resposta_final = None
        for _ in range(6):  # limite de segurança contra loop infinito de ferramentas
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=system_prompt_final,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                resposta_final = "".join(b.text for b in response.content if b.type == "text").strip()
                break

            # A IA pediu pra usar uma ou mais ferramentas: executa cada uma e devolve o resultado
            messages.append({"role": "assistant", "content": response.content})
            resultados_tools = []
            for bloco in response.content:
                if bloco.type != "tool_use":
                    continue
                if bloco.name == "atualizar_pedido":
                    resultado = executar_atualizar_pedido(conversa["id"], bloco.input)
                elif bloco.name == "consultar_pedido_minimo":
                    resultado = executar_consultar_pedido_minimo(bloco.input)
                elif bloco.name == "calcular_orcamento":
                    resultado = executar_calcular_orcamento(bloco.input)
                elif bloco.name == "fechar_pedido":
                    notificar_pedido_fechado(cliente, conversa["id"], bloco.input.get("resumo", ""))
                    marcar_conversa_fechada(conversa["id"])
                    resultado = {"ok": True, "mensagem": "Consultor notificado com sucesso. Esta conversa foi concluída - uma próxima mensagem do cliente inicia um pedido novo."}
                elif bloco.name == "transferir_para_consultor":
                    notificar_transferencia(cliente, conversa["id"], bloco.input.get("motivo", ""))
                    resultado = {"ok": True, "mensagem": "Consultor avisado, vai assumir a conversa em breve."}
                elif bloco.name == "solicitar_privacidade":
                    notificar_privacidade(cliente, conversa["id"], bloco.input.get("tipo", "duvida"), bloco.input.get("detalhe", ""))
                    resultado = {"ok": True, "mensagem": "Pedido registrado, a equipe vai tratar diretamente com o cliente."}
                else:
                    resultado = {"erro": f"ferramenta desconhecida: {bloco.name}"}
                resultados_tools.append({
                    "type": "tool_result",
                    "tool_use_id": bloco.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })
            messages.append({"role": "user", "content": resultados_tools})

        if resposta_final is None:
            resposta_final = "Deixa eu confirmar mais alguns detalhes com a equipe e já te retorno, pode ser?"

        resposta = resposta_final
        salvar_mensagem(conversa["id"], "ia", resposta)
        lead = calcular_score(conversa["id"], cliente["id"])
        # NOTA: o envio da mensagem ao cliente é feito pelo n8n (HTTP Request1),
        # por isso NÃO chamamos enviar_whatsapp() aqui para o cliente (evita duplicar).
        if lead["score"] >= 80:
            notificar_proprietario(cliente, lead["score"], conversa["id"])

        return jsonify({"ok": True, "resposta": resposta, "score": lead["score"], "categoria": lead["categoria"]})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

def limpar_dados_antigos(meses=12, modo_teste=True):
    """Remove (ou só relata, se modo_teste=True) dados de clientes cuja conversa está
    inativa há mais de `meses` meses E que NUNCA resultou em pedido fechado. Pedidos
    fechados são preservados (motivo de negócio: histórico de vendas). A ordem de
    exclusão respeita as dependências entre tabelas (mensagens antes de conversas, etc)."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.id AS conversa_id, c.cliente_id, cl.telefone
        FROM conversas c
        JOIN clientes cl ON cl.id = c.cliente_id
        WHERE c.ultima_mensagem < NOW() - (%s || ' months')::INTERVAL
          AND NOT EXISTS (
              SELECT 1 FROM notificacoes n
              WHERE n.conversa_id = c.id AND n.tipo = 'pedido_fechado'
          )
    """, (meses,))
    alvos = cur.fetchall()

    if modo_teste:
        cur.close(); release_db(db)
        return {"modo": "teste", "conversas_que_seriam_removidas": len(alvos),
                "telefones": [a["telefone"] for a in alvos]}

    removidos = 0
    for alvo in alvos:
        cur.execute("DELETE FROM mensagens WHERE conversa_id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM leads WHERE conversa_id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM notificacoes WHERE conversa_id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM conversas WHERE id=%s", (alvo["conversa_id"],))
        cur.execute("DELETE FROM clientes WHERE id=%s", (alvo["cliente_id"],))
        removidos += 1
    db.commit()
    cur.close(); release_db(db)
    logger.info(f"Limpeza de dados antigos (LGPD): {removidos} clientes/conversas removidos (inativos há mais de {meses} meses, sem pedido fechado)")
    return {"modo": "executado", "conversas_removidas": removidos}

@app.route("/manutencao/limpeza", methods=["POST"])
def manutencao_limpeza():
    """Endpoint protegido para a limpeza periódica de dados antigos (LGPD).
    Por padrão roda em modo de TESTE (não apaga nada) - só passa a apagar de
    verdade se o corpo da requisição incluir {"modo": "executar"}."""
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify({"erro": "não autorizado"}), 401
    data = request.get_json(silent=True) or {}
    meses = data.get("meses", 12)
    modo_teste = data.get("modo") != "executar"
    try:
        resultado = limpar_dados_antigos(meses=meses, modo_teste=modo_teste)
        return jsonify(resultado)
    except Exception as e:
        logger.error(f"Erro na limpeza de dados antigos: {e}")
        return jsonify({"erro": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",3000)))
