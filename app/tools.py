"""
As ferramentas (tool use) que a IA pode chamar durante a conversa: atualizar a memória
estruturada do pedido, consultar o mínimo, calcular o orçamento oficial, fechar o
pedido, transferir para um consultor humano, e tratar pedidos de privacidade (LGPD).
"""
from typing import Any, Dict

from app.config import logger, PRODUTOS_VALIDOS, MATERIAIS_VALIDOS, CORES_PRODUTO_VALIDAS
from app.precos import (
    ajustar_tamanho, espessura_mais_proxima, calcular_preco, calcular_pedido_minimo,
    processar_item_pedido,
)
from app.database import salvar_estado_pedido, obter_estado_pedido


def executar_atualizar_pedido(conversa_id: str, entrada: Dict[str, Any]) -> Dict[str, Any]:
    """Executa a ferramenta 'atualizar_pedido'. MESCLA com o estado anterior salvo no
    banco em vez de substituir cegamente: campos que a IA não reenviar desta vez
    (None/ausentes) são preservados do que já estava salvo, evitando perder dados se
    a IA esquecer de reenviar algum campo numa chamada futura."""
    try:
        itens_entrada = entrada.get("itens") or []
        itens_anteriores = obter_estado_pedido(conversa_id).get("itens") or []

        itens_mesclados = []
        for i, item_novo in enumerate(itens_entrada):
            item_antigo = itens_anteriores[i] if i < len(itens_anteriores) else {}
            item_completo = {**item_antigo, **{k: v for k, v in item_novo.items() if v is not None}}
            itens_mesclados.append(item_completo)

        resultados = [processar_item_pedido(it) for it in itens_mesclados]
        estado = {"itens": [r["item"] for r in resultados], "observacoes": entrada.get("observacoes", "")}
        salvar_estado_pedido(conversa_id, estado)

        itens_completos = sum(1 for r in resultados if r["completo"])
        logger.info(
            "Pedido atualizado",
            extra={
                "evento": "pedido_atualizado",
                "conversa_id": conversa_id,
                "itens": len(resultados),
                "completos": itens_completos,
            },
        )

        return {
            "itens": [
                {**r["item"], "ajustes_feitos": r["ajustes"], "faltando": r["faltando"],
                 "completo": r["completo"], "preco_preview": r["preco_preview"]}
                for r in resultados
            ],
            "observacoes": estado["observacoes"],
            "total_itens": len(resultados),
            "itens_completos": itens_completos,
        }
    except Exception as e:
        logger.error(
            "Falha ao atualizar pedido",
            extra={"evento": "erro_atualizar_pedido", "conversa_id": conversa_id, "erro": str(e)},
        )
        return {"erro": "Não foi possível atualizar o pedido agora. Continue a conversa normalmente e tente de novo em seguida."}


def executar_consultar_pedido_minimo(entrada: Dict[str, Any]) -> Dict[str, Any]:
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


CAMPOS_OBRIGATORIOS_ORCAMENTO = ["produto", "largura", "altura", "espessura", "cores_n", "milheiros"]


def executar_calcular_orcamento(entrada: Dict[str, Any]) -> Dict[str, Any]:
    campos_faltando = [c for c in CAMPOS_OBRIGATORIOS_ORCAMENTO if entrada.get(c) is None]
    if campos_faltando:
        logger.warning(
            "calcular_orcamento chamado com dados incompletos",
            extra={"evento": "orcamento_dados_incompletos", "campos_faltando": campos_faltando},
        )
        return {
            "erro": "dados_incompletos",
            "mensagem": "Ainda preciso de mais alguns dados antes de calcular o preço: " + ", ".join(campos_faltando) + ".",
            "campos_faltando": campos_faltando,
        }

    produto = entrada.get("produto")
    if produto not in PRODUTOS_VALIDOS:
        return {
            "erro": "valor_invalido",
            "mensagem": f"'{produto}' não é um produto reconhecido. Confirme com o cliente qual produto ele quer, entre as opções válidas.",
            "campos_faltando": ["produto"],
        }

    try:
        largura = float(entrada["largura"])
        altura = float(entrada["altura"])
        espessura_pedida = float(entrada["espessura"])
        cores_n = int(entrada["cores_n"])
        milheiros = float(entrada["milheiros"])
    except (TypeError, ValueError) as e:
        logger.warning(
            "calcular_orcamento recebeu valor não numérico",
            extra={"evento": "orcamento_valor_invalido", "detalhe": str(e)},
        )
        return {
            "erro": "valor_invalido",
            "mensagem": "Algum dos números do pedido (tamanho, espessura, cores ou quantidade) não ficou claro. Confirme esses valores com o cliente.",
        }

    material = entrada.get("material") or "Virgem BD"
    if material not in MATERIAIS_VALIDOS:
        material = "Virgem BD"
    cor_produto = entrada.get("cor_produto") or "Transparente"
    if cor_produto not in CORES_PRODUTO_VALIDAS:
        cor_produto = "Transparente"
    impressao = entrada.get("impressao") or "FRENTE"
    imp_map = "IMPRESSÃO FRENTE / VERSO" if impressao == "FRENTE_VERSO" else "IMPRESSÃO FRENTE"

    largura, altura, ajustes = ajustar_tamanho(produto, largura, altura, cores_n)
    espessura = espessura_mais_proxima(espessura_pedida, produto)
    if abs(espessura - espessura_pedida) > 1e-6:
        ajustes.append(f"espessura ajustada de {espessura_pedida:g}mm para {espessura:g}mm (opção disponível para este produto)")

    try:
        calc = calcular_preco(produto, material, largura, altura, cores_n, imp_map, milheiros, espessura=espessura)
    except ValueError as e:
        logger.warning(
            "Combinação sem preço na tabela",
            extra={"evento": "orcamento_combinacao_invalida", "detalhe": str(e)},
        )
        return {
            "erro": "valor_invalido",
            "mensagem": "Não encontrei preço para essa combinação de material, impressão e cores. Confirme os dados com o cliente.",
        }
    except Exception as e:
        logger.error(
            "Erro inesperado ao calcular preço",
            extra={"evento": "orcamento_erro_inesperado", "erro": str(e)},
        )
        return {
            "erro": "valor_invalido",
            "mensagem": "Não foi possível calcular o preço agora. Diga ao cliente que vai confirmar com a equipe e retornar em breve. NÃO informe nenhum valor.",
        }

    if not calc["atende_minimo"]:
        minimo_milheiros = calc["pedido_minimo_milheiros"]
        logger.info(
            "Pedido abaixo do peso mínimo",
            extra={
                "evento": "orcamento_fora_da_faixa",
                "peso_kg": calc["peso_total_kg"],
                "minimo_kg": calc["pedido_minimo_kg"],
            },
        )
        return {
            "erro": "fora_da_faixa",
            "mensagem": (
                f"Essa quantidade fica abaixo do peso mínimo de produção ({calc['pedido_minimo_kg']}kg). "
                f"Para fechar, seria preciso pelo menos {minimo_milheiros:.1f} mil unidades."
            ),
            "sugestoes": [f"aumentar a quantidade para {minimo_milheiros:.1f} mil unidades"],
            "pedido_minimo_milheiros": minimo_milheiros,
            "peso_calculado_kg": calc["peso_total_kg"],
            "peso_minimo_kg": calc["pedido_minimo_kg"],
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


TOOLS = [
    {
        "name": "atualizar_pedido",
        "description": "Chame esta ferramenta SEMPRE que o cliente informar qualquer dado novo sobre o pedido - mesmo que venha tudo numa mensagem só, ou aos poucos. Pode incluir MAIS DE UM item se o cliente mencionar vários tamanhos ou produtos na mesma mensagem (ex: '30x40, 40x50 e 50x50'). Envie a lista de itens do pedido, incluindo os novos dados desta mensagem - campos que você não souber ainda podem ficar de fora, o sistema preserva automaticamente o que já foi informado antes. O resultado te diz o que falta em cada item, então você nunca precisa perguntar de novo o que já foi informado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "description": "Lista de itens do pedido (um item = um produto+tamanho), na mesma ordem usada anteriormente na conversa. Inclua os novos dados aprendidos nesta mensagem.",
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
        "cache_control": {"type": "ephemeral"},
    },
]
