"""
Motor de preços da Plastcustom.

Fonte de verdade: lógica da Calculadora Automática enviada pelo proprietário.
A tabela abaixo mantém os fatores-base do fornecedor (C/N, 23/06/2026) e a
margem da Plastcustom é aplicada separadamente no cálculo final.

Regra comercial atual da Plastcustom: +25% sobre o preço-base calculado do
milheiro, seguindo a mesma ordem da Calculadora Automática.
"""
import math
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger, PRODUTOS_VALIDOS, MATERIAIS_VALIDOS


# ============================================================
# CONFIGURAÇÃO COMERCIAL PLASTCUSTOM
# ============================================================
MARGEM_REVENDA_PERCENTUAL = 25.0

# Tabela-base C/N da Calculadora Automática (23/06/2026).
# NÃO embutir margem nestes fatores. A margem é aplicada em calcular_preco().
TABELA = [
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 28.20, "v2": 27.70, "v3": 26.70},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 29.10, "v2": 28.70, "v3": 27.70},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 30.10, "v2": 29.60, "v3": 28.70},
    {"m": "Virgem AD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 31.50, "v2": 31.00, "v3": 30.60},

    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 30.60, "v2": 30.10, "v3": 29.20},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 31.50, "v2": 31.10, "v3": 30.10},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 32.50, "v2": 32.00, "v3": 31.10},
    {"m": "Virgem BD", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 33.90, "v2": 33.50, "v3": 33.00},

    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 25.50, "v2": 25.00, "v3": 24.00},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 26.60, "v2": 26.00, "v3": 25.00},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 27.10, "v2": 26.60, "v3": 25.50},
    {"m": "Reciclado Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 28.60, "v2": 28.10, "v3": 27.60},

    # A tabela recebida só traz a faixa 150-200 kg para Reciclado Sem Cor.
    # A Calculadora Automática replica esse valor nas faixas maiores.
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE",         "c": "até 2 cores",  "v1": 20.10, "v2": 20.10, "v3": 20.10},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE",         "c": "3 ou + cores", "v1": 21.10, "v2": 21.10, "v3": 21.10},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "até 2 cores",  "v1": 21.60, "v2": 21.60, "v3": 21.60},
    {"m": "Reciclado Sem Cor", "i": "IMPRESSÃO FRENTE / VERSO", "c": "3 ou + cores", "v1": 23.10, "v2": 23.10, "v3": 23.10},
]

# PP permanece exatamente como na Calculadora Automática enviada.
# A própria calculadora informa que esses valores são placeholders editáveis.
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
    ],
}


def recarregar_tabela_precos() -> Dict[str, Any]:
    """Compatibilidade com a rota de manutenção existente.

    A partir desta versão, preço não é mais carregado de HTML em runtime. Isso elimina
    divergência entre Python e arquivos HTML antigos. Para mudar fatores ou margem,
    altere este módulo e faça novo deploy.
    """
    logger.info(
        "Tabela de preços interna ativa",
        extra={
            "evento": "precos_tabela_interna",
            "linhas_tabela": len(TABELA),
            "margem_percentual": MARGEM_REVENDA_PERCENTUAL,
        },
    )
    return {
        "sucesso": True,
        "fonte": "calculadora_automatica_embutida",
        "linhas_tabela": len(TABELA),
        "linhas_pp_com_nf": len(PRECOS_PP["com_nf"]),
        "linhas_pp_sem_nf": len(PRECOS_PP["sem_nf"]),
        "margem_percentual": MARGEM_REVENDA_PERCENTUAL,
    }


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


def largura_camiseta_mais_proxima(largura: float) -> float:
    return min(LARGURAS_SACOLA_CAMISETA_PERMITIDAS, key=lambda v: abs(v - largura))


def disponibilidade_cilindro(medida_base: float, cores_n: int, max_rep: int, produto: str) -> List[Dict[str, bool]]:
    """Para cada repetição possível (1x, 2x, 3x...) verifica se existe cilindro compatível."""
    resultados: List[Dict[str, bool]] = []
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


def medida_cilindro_valida(produto: str, medida: float, cores_n: int, max_rep: int) -> bool:
    return any(r["disponivel"] and r["ok_cores"] for r in disponibilidade_cilindro(medida, cores_n, max_rep, produto))


def medida_cilindro_mais_proxima(produto: str, medida: float, cores_n: int, max_rep: int) -> Optional[float]:
    """Busca, entre todos os cilindros compatíveis, a medida-base mais próxima do que o cliente pediu."""
    melhor: Optional[float] = None
    melhor_dist: Optional[float] = None
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


def ajustar_tamanho(produto: str, largura: float, altura: float, cores_n: int) -> Tuple[float, float, List[str]]:
    """Ajusta largura/altura para os valores tecnicamente possíveis (com cilindro de impressão
    disponível), igual a calculadora faz automaticamente. Retorna (largura, altura, lista_de_ajustes)."""
    largura = float(largura); altura = float(altura); cores_n = int(cores_n)
    ajustes: List[str] = []

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
    "Sacola Camiseta": [0.003, 0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014],
    "Sacola Vazada": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014],
    "Saco Impresso Solda Fundo": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014],
    "Saco com Aba": [0.004, 0.005, 0.006, 0.007, 0.008, 0.009, 0.010, 0.011, 0.012, 0.013, 0.014],
}


def espessura_mais_proxima(valor: Any, produto: Optional[str] = None) -> float:
    """Ajusta qualquer valor informado para a opção oficial mais próxima DENTRO do produto escolhido."""
    opcoes = ESPESSURAS_POR_PRODUTO.get(produto) or sorted({e for lst in ESPESSURAS_POR_PRODUTO.values() for e in lst})
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return opcoes[0]
    return min(opcoes, key=lambda x: abs(x - v))


def lookup_pp(imp: str, cores_faixa: str, kg: float, tipo_nota: str) -> float:
    tabela = PRECOS_PP.get(tipo_nota, PRECOS_PP["com_nf"])
    faixa = next((f for f in tabela if kg <= f["ate"]), tabela[-1])
    frente_verso = "VERSO" in imp
    ate2 = cores_faixa != "3 ou + cores"
    if frente_verso:
        return faixa["verso2"] if ate2 else faixa["verso3"]
    return faixa["frente2"] if ate2 else faixa["frente3"]


def lookup_fator_kg(material: str, imp: str, cores_faixa: str, kg: float, tipo_nota: str = "com_nf") -> float:
    if material == "Polipropileno (PP)":
        return lookup_pp(imp, cores_faixa, kg, tipo_nota)
    row = next((r for r in TABELA if r["m"] == material and r["i"] == imp and r["c"] == cores_faixa), None)
    if not row:
        return 0
    fator_base = row["v1"] if kg <= 200 else (row["v2"] if kg <= 400 else row["v3"])
    return round(fator_base * 0.91, 2) if tipo_nota == "sem_nf" else fator_base


def calcular_pedido_minimo(largura: float, altura: float, espessura: float, cores_n: int) -> Optional[Dict[str, float]]:
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


def calcular_preco(
    produto: str,
    material: str,
    largura: float,
    altura: float,
    cores_n: int,
    imp: str,
    milheiros: float,
    espessura: float = 0.028,
    tipo_nota: str = "com_nf",
    acrescimo_percentual: float = MARGEM_REVENDA_PERCENTUAL,
    fita_extra_mil: float = 0.0,
) -> Dict[str, Any]:
    """Calcula preço seguindo a ordem da Calculadora Automática.

    Ordem reproduzida:
      1) peso do milheiro = largura * altura * espessura
      2) total kg = peso do milheiro * milheiros
      3) busca fator pela faixa de peso/material/impressão/cores
      4) sem impressão (exceto PP): -R$2,00 no fator
      5) milheiro abaixo de 1,5 kg: +R$3,00 no fator
      6) aplica +25% (ou acrescimo_percentual informado) sobre a base do milheiro
      7) soma fita_extra_mil, quando houver

    Clichê continua fora deste cálculo, como no fluxo atual do vendedor.
    """
    L = float(largura)
    A = float(altura)
    E = float(espessura)
    MILH = float(milheiros)
    cores_n = int(cores_n)
    ADD = float(acrescimo_percentual)
    fita_extra_mil = float(fita_extra_mil)

    if produto not in PRODUTOS_VALIDOS:
        raise ValueError(f"Produto inválido: {produto}")
    if material not in MATERIAIS_VALIDOS:
        raise ValueError(f"Material inválido ou não informado: {material}")
    if imp not in ("IMPRESSÃO FRENTE", "IMPRESSÃO FRENTE / VERSO"):
        raise ValueError(f"Tipo de impressão inválido ou não informado: {imp}")
    if L <= 0 or A <= 0 or E <= 0 or MILH <= 0:
        raise ValueError("Largura, altura, espessura e quantidade devem ser maiores que zero")

    area = L * A
    vol = area * E
    p_un_g = vol
    p_mil_kg = p_un_g
    total_kg = p_mil_kg * MILH

    cores_faixa = "até 2 cores" if cores_n <= 2 else "3 ou + cores"
    preco_kg_tabela = lookup_fator_kg(material, imp, cores_faixa, total_kg, tipo_nota)

    if preco_kg_tabela <= 0:
        raise ValueError(f"Combinação sem preço na tabela: {material} / {imp} / {cores_faixa}")

    # Mesma regra da calculadora: sem impressão reduz R$2,00 no fator, exceto PP.
    preco_kg_auto = preco_kg_tabela
    if cores_n == 0 and material != "Polipropileno (PP)":
        preco_kg_auto -= 2.0

    mil_base = preco_kg_auto * p_mil_kg

    # A implementação real da calculadora usa +R$3,00 quando o milheiro pesa <1,5 kg.
    adicional_fator_kg = 3.0 if 0 < p_mil_kg < 1.5 else 0.0
    adicional_mil_baixo_peso = adicional_fator_kg * p_mil_kg
    mil_base_com_baixo_peso = mil_base + adicional_mil_baixo_peso

    # Mesma ordem da calculadora: o percentual incide sobre a base antes da fita.
    multiplicador = 1.0 + (ADD / 100.0)
    milheiro_sem_fita = mil_base_com_baixo_peso * multiplicador
    milheiro = milheiro_sem_fita + fita_extra_mil

    unitario = milheiro / 1000.0
    total = milheiro * MILH
    fator_kg_final = total_kg and ((milheiro * MILH) / total_kg) or 0.0

    pedido_min_kg = 100 if cores_n == 0 else 150
    minimo = calcular_pedido_minimo(L, A, E, cores_n)

    return {
        "preco_kg_tabela": round(preco_kg_tabela, 2),
        "preco_kg": round(preco_kg_auto, 2),
        "fator_kg_final": round(fator_kg_final, 4),
        "margem_percentual": round(ADD, 2),
        "unitario": round(unitario, 4),
        "milheiro": round(milheiro, 2),
        "total": round(total, 2),
        "peso_milheiro_kg": round(p_mil_kg, 4),
        "peso_total_kg": round(total_kg, 2),
        "espessura_usada": round(E, 3),
        "adicional_fator_baixo_peso": round(adicional_fator_kg, 2),
        "fita_extra_mil": round(fita_extra_mil, 2),
        "pedido_minimo_kg": pedido_min_kg,
        "pedido_minimo_milheiros": minimo["milheiros_min"] if minimo else None,
        "atende_minimo": total_kg >= pedido_min_kg,
        "preco_especial_aplicado": False,
    }


CAMPOS_OBRIGATORIOS_ITEM = [
    "produto", "material", "largura", "altura", "espessura",
    "cores_n", "impressao", "milheiros",
]


def processar_item_pedido(item: Dict[str, Any]) -> Dict[str, Any]:
    """Valida e ajusta UM item do pedido (tamanho/espessura), calcula o que falta, e
    gera uma prévia de preço se já estiver completo. Função pura (sem banco de dados),
    o que a deixa fácil de testar isoladamente."""
    ajustes: List[str] = []
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
            calc = calcular_preco(
                produto, material, largura, altura, int(cores_n), imp_map,
                milheiros, espessura=espessura
            )
            preco_preview = {
                "preco_por_milheiro": calc["milheiro"], "preco_total": calc["total"],
                "atende_minimo": calc["atende_minimo"], "pedido_minimo_milheiros": calc["pedido_minimo_milheiros"],
            }
        except Exception as e:
            logger.warning(f"Não foi possível gerar prévia de preço para item: {e}")

    return {"item": item_normalizado, "ajustes": ajustes, "faltando": faltando, "completo": completo, "preco_preview": preco_preview}
