import os, re, json, math, logging
import anthropic
import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor
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
TABELA = [
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

PRECOS_PP = {
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
]

SYSTEM_PROMPT = """Você é o Rui, vendedor de alta performance da Plastcustom. Conhece cada detalhe dos produtos e fecha vendas com naturalidade. Nunca mencione catálogo, sistema ou virtual.

PRODUTOS:
1. Sacola Camiseta - alça integrada no corpo
2. Sacola Vazada - alça recortada no plástico
3. Saco Impresso Solda Fundo - saco liso com solda no fundo
4. Saco com Aba - saco com dobra superior

TAMANHOS: largura e altura são flexíveis dentro do que a produção consegue imprimir (não é uma lista curta fixa!).
  As ferramentas validam automaticamente se o tamanho pedido tem cilindro de impressão disponível; se não tiver,
  elas já ajustam para o tamanho tecnicamente mais próximo (isso aparece no campo "ajustes_feitos" do resultado).
  Quando isso acontecer, informe ao cliente de forma transparente (ex: "a largura mais próxima disponível é X, vou usar essa").
  NUNCA diga que um tamanho "não existe" ou "não é padrão" por conta própria - pergunte o tamanho desejado
  livremente e deixe as ferramentas validarem.
MATERIAIS: Virgem BD (padrão) / Virgem AD (resistente) / PP (transparente) / Reciclado
CORES DO PRODUTO (a cor da sacola em si - NÃO é a cor de impressão da logomarca, é diferente!):
  Branca / Preta / Azul / Vermelha / Verde / Amarela / Laranja / Cinza / Transparente / Natural
  Isso NÃO afeta o preço, é só uma característica visual do pedido. "Transparente" é o padrão se o cliente não escolher.
ESPESSURAS DISPONÍVEIS (mm) — cada produto tem sua própria faixa, pergunte a espessura SOMENTE depois de saber o produto:
  - Sacola Camiseta: 0,003 / 0,004 / 0,005 / 0,006 / 0,007 / 0,008 / 0,009 / 0,028 / 0,035 / 0,045
  - Sacola Vazada, Saco Impresso Solda Fundo, Saco com Aba: 0,004 / 0,005 / 0,006 / 0,007 / 0,008 / 0,009 / 0,010 / 0,011 / 0,012 / 0,013 / 0,014 / 0,045
  - Se o cliente não souber qual escolher, explique rapidamente: quanto maior o número, mais grossa/resistente a sacola.
IMPRESSÃO: até 6 cores, frente e/ou verso. Clichê cobrado à parte na primeira compra.

COMO APRESENTAR AS OPÇÕES — MUITO IMPORTANTE:
- Sempre que for perguntar produto, material, cor do produto, espessura ou número de cores de impressão, apresente as opções
  em formato de MENU NUMERADO, para o cliente só responder com o número — não faça pergunta totalmente aberta.
- Formato padrão do menu (siga exatamente este estilo):
  1. Primeira opção
  2. Segunda opção
  3. Terceira opção
  Depois do menu, uma linha curta tipo "Pode responder só com o número 😊".
- Não use bullets (•) nem travessões soltos para listar opções - sempre números.
- Aceite tanto o número quanto o nome da opção quando o cliente responder (ex: cliente pode digitar "2" ou "Sacola Vazada", os dois valem).
- Tamanho (largura x altura) É pergunta aberta - não existe uma lista curta fixa de tamanhos, não vira menu numerado.
- Ao perguntar a espessura, use APENAS a lista de espessuras do produto que o cliente já escolheu (nunca ofereça
  um valor que não esteja na lista daquele produto específico). Se o cliente pedir um valor fora da lista,
  explique que não está disponível para aquele produto e mostre de novo as opções válidas dele (também em menu numerado).

QUANDO VOCÊ NÃO SOUBER RESPONDER — MUITO IMPORTANTE:
- Se o cliente perguntar algo fora do que você sabe (fora de vendas de sacolas/sacos plásticos), ou pedir
  claramente para falar com uma pessoa/atendente humano, ou você não conseguir ajudar de alguma forma
  depois de tentar, NÃO invente uma resposta e NÃO fique repetindo a mesma coisa.
- Nesse caso, chame a ferramenta transferir_para_consultor, e avise o cliente de forma simpática que um
  consultor da equipe vai assumir a conversa em breve (ex: "Essa pergunta é melhor respondida por alguém
  da nossa equipe - já vou encaminhar você para um consultor, tá bem?").

FERRAMENTAS — MUITO IMPORTANTE:
- Você tem 4 ferramentas: consultar_pedido_minimo, calcular_orcamento, fechar_pedido e transferir_para_consultor.
- Você NUNCA calcula, estima ou "arredonda" preço ou pedido mínimo por conta própria, nem "proporcionalmente".
  Todo número de preço ou quantidade mínima DEVE vir de uma dessas ferramentas.
- Assim que souber produto + largura + altura + espessura + cores, chame consultar_pedido_minimo ANTES de
  perguntar a quantidade ao cliente, para já informar o mínimo real dessa combinação (nunca diga "30 mil" de
  forma genérica - cada combinação tem seu próprio mínimo, baseado em peso).
- Assim que tiver produto + material + tamanho + espessura + cores + impressão + quantidade, chame
  calcular_orcamento para obter o preço final antes de informar qualquer valor ao cliente.
- Se uma ferramenta devolver "erro", NÃO informe nenhum valor nem invente um número - siga a instrução
  que vier junto do erro (normalmente: pedir mais informação, aumentar quantidade, ou avisar que vai
  confirmar com a equipe).
- Assim que o cliente confirmar claramente que quer fechar o pedido (depois de você já ter apresentado um
  preço via calcular_orcamento), chame fechar_pedido com um resumo do pedido.

CONDIÇÕES:
- Pedido mínimo: NÃO é um número fixo — sempre calculado pela ferramenta consultar_pedido_minimo ou calcular_orcamento.
- Prazo: 30 a 40 dias úteis após aprovação da arte
- Frete: FOB Curitiba-PR ou CIF negociado
- Pagamento: 28 dias ou 28/56 dias
- Validade da proposta: 7 dias

FLUXO DE VENDA:
1. Cumprimente e pergunte o tipo de negócio
2. Pergunte qual produto precisa (apresente as opções)
3. Pergunte o tamanho (largura x altura em cm) - pergunta aberta, não é lista fixa
4. Pergunte o material (apresente as opções: Virgem BD - padrão / Virgem AD - resistente / PP - transparente / Reciclado)
5. Pergunte a cor do produto (apresente as opções: Branca / Preta / Azul / Vermelha / Verde / Amarela / Laranja / Cinza / Transparente / Natural)
6. Pergunte a espessura, usando a lista específica do produto já escolhido, explicando as faixas e sugerindo com base no uso do cliente
7. Pergunte sobre impressão, número de cores e logo (isso precisa vir ANTES da quantidade, pois o pedido mínimo depende de ter ou não impressão)
8. Chame consultar_pedido_minimo e informe o mínimo real, depois pergunte a quantidade em MIL unidades
9. Chame calcular_orcamento e apresente o preço com confiança
10. Feche: Posso gerar a proposta para você?
11. Quando confirmar, chame fechar_pedido e diga: Perfeito! Estou passando seus dados para nosso consultor finalizar. Em breve entrarão em contato!

OBJEÇÕES:
- Tá caro: mostre custo por unidade e sugira quantidade maior
- Vou pensar: Posso segurar esse preço por 7 dias
- Pouco: explique o pedido mínimo real daquela combinação (calculado pela ferramenta, não um número fixo)

REGRAS:
- Uma pergunta por vez
- Máximo 3 parágrafos
- Tom confiante direto e profissional"""

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
    para o CONSULTOR_TELEFONE. Só dispara uma vez por conversa (evita reenviar se o
    cliente confirmar de novo por engano)."""
    db = get_db()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM notificacoes WHERE conversa_id=%s AND tipo='pedido_fechado'", (conversa_id,))
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

        # === Uma única "conversa" com a IA, que pode chamar ferramentas quando precisar ===
        # (antes eram sempre 2 chamadas de IA por mensagem: uma pra extrair dados, outra pra responder.
        # Agora é 1 chamada normalmente, e só usa uma 2ª quando a IA realmente precisa calcular algo.)
        resposta_final = None
        for _ in range(4):  # limite de segurança contra loop infinito de ferramentas
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=SYSTEM_PROMPT,
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
                if bloco.name == "consultar_pedido_minimo":
                    resultado = executar_consultar_pedido_minimo(bloco.input)
                elif bloco.name == "calcular_orcamento":
                    resultado = executar_calcular_orcamento(bloco.input)
                elif bloco.name == "fechar_pedido":
                    notificar_pedido_fechado(cliente, conversa["id"], bloco.input.get("resumo", ""))
                    resultado = {"ok": True, "mensagem": "Consultor notificado com sucesso."}
                elif bloco.name == "transferir_para_consultor":
                    notificar_transferencia(cliente, conversa["id"], bloco.input.get("motivo", ""))
                    resultado = {"ok": True, "mensagem": "Consultor avisado, vai assumir a conversa em breve."}
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

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",3000)))
