"""
Tudo relacionado a PREÇO: as tabelas oficiais (carregadas do HTML da calculadora, com
uma versão de reserva embutida), as regras de tamanho/espessura por produto (validação
contra os cilindros de impressão disponíveis), e a fórmula de cálculo do orçamento.

Este módulo é "puro" no sentido de que não acessa banco de dados nem rede - só recebe
números e devolve números. Isso o deixa fácil de testar isoladamente (veja test_pricing.py).
"""
import re
import math

from app.config import (
    logger, CAMINHO_CALCULADORA,
    PRODUTOS_VALIDOS, MATERIAIS_VALIDOS,
)

# ============================================================
# TABELA DE PREÇOS OFICIAL — portada da calculadora HTML da Plastcustom
# Faixas: v1 = 150-200kg | v2 = 210-400kg | v3 = 410kg ou +
# ============================================================
# TABELA_PADRAO / PRECOS_PP_PADRAO servem de rede de segurança: são usadas SOMENTE se o
# arquivo Plastcustom_Orcamento.html não for encontrado ou não puder ser lido. Na operação
# normal, os valores de verdade vêm direto do arquivo HTML - assim, pra atualizar preços,
# basta substituir esse arquivo no GitHub e fazer o deploy, sem editar nenhum código Python.
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
    HTML da calculadora oficial. Se o arquivo não existir ou não puder ser lido, devolve
    (None, None) e quem chamou usa a tabela padrão como rede de segurança."""
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


_tabela_carregada, _precos_pp_carregados = carregar_tabela_precos_do_html(CAMINHO_CALCULADORA)
if _tabela_carregada:
    TABELA = _tabela_carregada
    PRECOS_PP = _precos_pp_carregados
    logger.info(f"Tabela de preços carregada de {CAMINHO_CALCULADORA} ({len(TABELA)} linhas)")
else:
    TABELA = TABELA_PADRAO
    PRECOS_PP = PRECOS_PP_PADRAO
    logger.warning(f"Não encontrou/não conseguiu ler {CAMINHO_CALCULADORA} - usando tabela de preços padrão embutida no código")


# ============================================================
# TABELA DE CILINDROS DE IMPRESSÃO — determina quais larguras/alturas são
# tecnicamente possíveis de imprimir para cada produto
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
    """Calcula o preço EXATO seguindo a mesma lógica da calculadora oficial da Plastcustom.
    Não inclui clichê (cobrado à parte, conforme já informado pelo robô ao cliente)."""
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
    if cor_produto is not None and cor_produto not in ("Branca", "Preta", "Azul", "Vermelha", "Verde", "Amarela", "Laranja", "Cinza", "Transparente", "Natural"):
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
